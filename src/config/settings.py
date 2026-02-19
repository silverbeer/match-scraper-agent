"""Environment-based configuration for match-scraper-agent."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

ENVS_DIR = Path(__file__).resolve().parents[2] / "envs"


def env_file_path(env: str) -> Path:
    """Resolve the dotenv file for a given environment name.

    Args:
        env: Environment name (e.g. "local", "prod").

    Returns:
        Path to envs/.env.<env>.

    Raises:
        FileNotFoundError: If the env file does not exist.
    """
    path = ENVS_DIR / f".env.{env}"
    if not path.is_file():
        available = [p.name for p in ENVS_DIR.glob(".env.*")] if ENVS_DIR.is_dir() else []
        msg = f"Env file not found: {path}"
        if available:
            msg += f" (available: {', '.join(sorted(available))})"
        raise FileNotFoundError(msg)
    return path


class AgentSettings(BaseSettings):
    """Agent configuration loaded from a dotenv file + environment variables.

    Precedence: env vars > dotenv file > defaults.
    Pass _env_file to the constructor to select which env file to load.
    """

    model_config = {"env_prefix": "AGENT_"}

    proxy_base_url: str = "http://localhost:8100"
    model_name: str = "claude-haiku-4-5-20251001"
    anthropic_api_key: str = "agent-via-proxy"
    rabbitmq_url: str = "amqp://guest:guest@localhost:5672/"
    exchange_name: str = "matches-fanout"
    league: str = "Homegrown"
    age_group: str = "U14"
    division: str = "Northeast"
    missing_table_api_url: str = "http://localhost:8000"
    missing_table_api_key: str = ""
    dry_run: bool = False
    json_logs: bool = False
    log_level: str = "info"

    # Database (missing-table Supabase — used by trigger.sh post-run verification)
    db_host: str = "127.0.0.1"
    db_port: int = 54332
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_name: str = "postgres"
