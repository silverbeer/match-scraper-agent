"""Typer CLI for match-scraper-agent."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated

import structlog
import typer

if TYPE_CHECKING:
    from config.settings import AgentSettings

app = typer.Typer(name="match-scraper-agent", no_args_is_help=True)
logger = structlog.get_logger()


def _classify_error(exc: Exception, proxy_url: str) -> tuple[str, bool]:
    """Return a one-line diagnostic and whether the error is known.

    Returns:
        (message, known) — known=True means the diagnostic is sufficient,
        no traceback needed.
    """
    import httpx
    from anthropic import APIConnectionError, APIStatusError, AuthenticationError

    # Walk the full cause chain once
    chain: BaseException | None = exc
    while chain is not None:
        if isinstance(chain, (httpx.ConnectError, APIConnectionError)):
            return (
                f"Cannot reach proxy at {proxy_url} — is the iron-claw proxy running?",
                True,
            )
        if isinstance(chain, AuthenticationError):
            return (
                "Authentication failed — check AGENT_ANTHROPIC_API_KEY or proxy auth config",
                True,
            )
        if isinstance(chain, APIStatusError):
            return f"API error {chain.status_code}: {chain.message}", True
        chain = getattr(chain, "__cause__", None)

    # Truly unexpected — caller should log the traceback
    return str(exc), False


_TARGET_PROMPTS: dict[str, str] = {
    "u14-hg": ("Only scrape U14 Homegrown Northeast today. Do not scrape other targets."),
    "u14-hg-ifa": (
        "Only scrape U14 Homegrown Northeast today. "
        "Only IFA matches will be submitted. Do not scrape other targets."
    ),
    "u13-hg": ("Only scrape U13 Homegrown Northeast today. Do not scrape other targets."),
    "u13-hg-ifa": (
        "Only scrape U13 Homegrown Northeast today. "
        "Only IFA matches will be submitted. Do not scrape other targets."
    ),
    "u14-academy": (
        "Only scrape U14 Academy New England (conference='New England') today. "
        "Do not scrape other targets."
    ),
    "u14-academy-ifa": (
        "Only scrape U14 Academy New England (conference='New England') today. "
        "Only IFA Academy matches will be submitted. Do not scrape other targets."
    ),
}

# Targets that include a team filter — value is the DB team name used for filtering
_TARGET_TEAM_FILTER: dict[str, str] = {
    "u14-hg-ifa": "IFA",
    "u13-hg-ifa": "IFA",
    "u14-academy-ifa": "IFA Academy",
}


def _proxy_preflight(settings: AgentSettings) -> str:
    """Check iron-claw proxy status and return the model to use.

    Hits GET /status on the proxy. If RADIUS is active, validates the token
    budget and returns the model allowed by the RADIUS session. If bare mode
    (no RADIUS), returns the configured model. Exits on unreachable proxy or
    exhausted budget.

    Args:
        settings: Agent configuration with proxy URL and min token budget.

    Returns:
        The model name to use for this run.

    Raises:
        typer.Exit: If the proxy is unreachable or budget is exhausted.
    """
    import httpx

    base = settings.proxy_base_url.rstrip("/")
    status_url = base.replace("/v1", "") + "/status"

    try:
        resp = httpx.get(status_url, timeout=5)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.error("preflight.proxy_unreachable", url=status_url, error=str(exc))
        raise typer.Exit(code=1) from None
    except httpx.HTTPStatusError as exc:
        logger.error("preflight.proxy_error", url=status_url, status=exc.response.status_code)
        raise typer.Exit(code=1) from None

    # Bare mode — proxy is up but no RADIUS session
    if data.get("no_radius_session"):
        logger.info("preflight.bare_mode", proxy=status_url)
        return settings.model_name

    # RADIUS active — check budget
    tokens_remaining = data.get("tokens_remaining", 0)
    model_allowed = data.get("model_allowed", settings.model_name)
    policy_mode = data.get("policy_mode", "enforce")

    logger.info(
        "preflight.radius_active",
        model_allowed=model_allowed,
        tokens_remaining=tokens_remaining,
        budget_pct=data.get("budget_pct"),
        policy_mode=policy_mode,
    )

    if tokens_remaining < settings.min_token_budget:
        if policy_mode == "monitor":
            logger.warning(
                "preflight.budget_low_monitor",
                tokens_remaining=tokens_remaining,
                min_token_budget=settings.min_token_budget,
            )
        else:
            logger.error(
                "preflight.budget_exhausted",
                tokens_remaining=tokens_remaining,
                min_token_budget=settings.min_token_budget,
            )
            raise typer.Exit(code=1)

    return model_allowed


@app.command()
def run(
    env: Annotated[str, typer.Option("--env", help="Environment name (local, prod)")] = "local",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Skip mutating operations")] = False,
    json_logs: Annotated[bool, typer.Option("--json-logs", help="Output JSON log lines")] = False,
    model: Annotated[str | None, typer.Option("--model", help="Override model name")] = None,
    proxy_url: Annotated[
        str | None, typer.Option("--proxy-url", help="Override proxy base URL")
    ] = None,
    target: Annotated[
        str | None,
        typer.Option("--target", help="Scrape only this target (u14-hg, u13-hg, u14-academy)"),
    ] = None,
    no_proxy: Annotated[
        bool, typer.Option("--no-proxy", help="Bypass iron-claw proxy, go direct to Anthropic")
    ] = False,
) -> None:
    """Run the match-scraper agent."""
    from src.celery.queue_client import MatchQueueClient

    from agent.core import create_agent
    from agent.deps import AgentDeps
    from config.settings import AgentSettings, env_file_path
    from utils.logger import configure_logging

    settings = AgentSettings(_env_file=env_file_path(env))
    if model:
        settings.model_name = model
    if proxy_url:
        settings.proxy_base_url = proxy_url
    if no_proxy:
        settings.proxy_enabled = False
    if dry_run:
        settings.dry_run = True

    configure_logging(json_output=json_logs or settings.json_logs, log_level=settings.log_level)

    # Bind run_id and env to all log lines for this invocation
    run_id = uuid.uuid4().hex[:12]
    structlog.contextvars.bind_contextvars(run_id=run_id, env=env)

    # Proxy preflight — validate budget and resolve model from RADIUS
    if settings.proxy_enabled:
        preflight_model = _proxy_preflight(settings)
        if preflight_model != settings.model_name:
            logger.info(
                "preflight.model_override",
                configured=settings.model_name,
                using=preflight_model,
            )
            settings.model_name = preflight_model

    logger.info(
        "agent.starting",
        model=settings.model_name,
        proxy=settings.proxy_base_url,
        proxy_enabled=settings.proxy_enabled,
        dry_run=settings.dry_run,
    )

    try:
        agent = create_agent(settings)
        queue_client = MatchQueueClient(
            broker_url=settings.rabbitmq_url,
            exchange_name=settings.exchange_name,
        )
        if target and target not in _TARGET_PROMPTS:
            valid = ", ".join(sorted(_TARGET_PROMPTS))
            typer.echo(f"Unknown target '{target}'. Valid targets: {valid}", err=True)
            raise typer.Exit(code=1)

        team_filter = _TARGET_TEAM_FILTER.get(target or "", "")
        deps = AgentDeps(
            queue_client=queue_client,
            settings=settings,
            dry_run=settings.dry_run,
            team_filter=team_filter,
        )

        if target:
            user_prompt = _TARGET_PROMPTS[target]
            logger.info("agent.target_filter", target=target, team_filter=team_filter or None)
        else:
            user_prompt = "Review today's matches and take appropriate actions."

        result = agent.run_sync(user_prompt, deps=deps)
    except Exception as exc:
        message, known = _classify_error(exc, settings.proxy_base_url)
        logger.error("agent.failed", error=message)
        if not known:
            logger.error("agent.failed.trace", exc_info=exc)
        raise typer.Exit(code=1) from None

    logger.info(
        "agent.completed",
        summary=result.output.summary,
        actions=len(result.output.actions),
        matches_found=result.output.matches_found,
        matches_submitted=result.output.matches_submitted,
        requests=result.usage().requests,
        tokens=result.usage().total_tokens,
    )

    if json_logs or settings.json_logs:
        print(result.output.model_dump_json(indent=2))
    else:
        typer.echo(f"\n{result.output.summary}")
        for action in result.output.actions:
            prefix = "[DRY RUN] " if action.dry_run else ""
            typer.echo(f"  {prefix}{action.action}: {action.detail}")

    structlog.contextvars.unbind_contextvars("run_id", "env")


@app.command()
def check(
    env: Annotated[str, typer.Option("--env", help="Environment name (local, prod)")] = "local",
    proxy_url: Annotated[
        str | None, typer.Option("--proxy-url", help="Override proxy base URL")
    ] = None,
) -> None:
    """Check proxy health and RabbitMQ connectivity."""
    import httpx

    from config.settings import AgentSettings, env_file_path
    from utils.logger import configure_logging

    configure_logging(json_output=False)

    settings = AgentSettings(_env_file=env_file_path(env))
    if proxy_url:
        settings.proxy_base_url = proxy_url

    typer.echo(f"environment: {env}")

    # Check proxy
    base = settings.proxy_base_url.rstrip("/")
    status_url = base.replace("/v1", "") + "/status"
    typer.echo(f"proxy: checking {status_url}")
    try:
        resp = httpx.get(status_url, timeout=5)
        typer.echo(f"  status: {resp.status_code}")
        if resp.status_code == 200:
            typer.echo(f"  response: {resp.text[:200]}")
    except httpx.ConnectError:
        typer.echo("  status: UNREACHABLE")
    except httpx.TimeoutException:
        typer.echo("  status: TIMEOUT")

    # Check RabbitMQ
    typer.echo(f"rabbitmq: checking {settings.rabbitmq_url}")
    try:
        from src.celery.queue_client import MatchQueueClient

        client = MatchQueueClient(
            broker_url=settings.rabbitmq_url,
            exchange_name=settings.exchange_name,
        )
        if client.check_connection():
            typer.echo("  status: connected")
        else:
            typer.echo("  status: UNREACHABLE")
    except Exception as exc:
        typer.echo(f"  status: ERROR ({exc})")
