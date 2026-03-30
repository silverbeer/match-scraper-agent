"""Async httpx client wrapping all MT audit API calls."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from audit.models import AuditEvent, AuditTeamStatus, NextTeamResponse

logger = structlog.get_logger()


def _headers(api_key: str) -> dict[str, str]:
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


async def fetch_next_team(
    api_url: str,
    api_key: str,
    season: str,
    division: str,
    league: str,
) -> NextTeamResponse | None:
    """Return the next team to audit, or None if all teams are up-to-date this week."""
    url = f"{api_url}/api/agent/audit/next-team"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            url,
            params={"season": season, "division": division, "league": league},
            headers=_headers(api_key),
        )
        if resp.status_code == 204:
            logger.info("audit.client.next_team", status="all_current")
            return None
        resp.raise_for_status()
        data = resp.json()
    logger.info("audit.client.next_team", team=data.get("team"), age_group=data.get("age_group"))
    return NextTeamResponse.model_validate(data)


async def fetch_mt_matches(
    api_url: str,
    api_key: str,
    age_group: str,
    league: str,
    division: str,
    team: str,
    season: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict]:
    """Fetch individual match records for a team from MT.

    start_date/end_date scope the results to the same window as the scrape
    to avoid false extra_in_mt findings from other season segments (e.g. fall).
    """
    url = f"{api_url}/api/agent/matches"
    params: dict[str, str] = {
        "age_group": age_group,
        "league": league,
        "division": division,
        "team": team,
        "season": season,
    }
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, params=params, headers=_headers(api_key))
        resp.raise_for_status()
        data = resp.json()
    matches = data.get("matches", [])
    logger.info("audit.client.fetch_mt_matches", team=team, count=len(matches))
    return matches


async def submit_audit_event(
    api_url: str,
    api_key: str,
    event: AuditEvent,
) -> None:
    """Submit audit findings (or clean result) for a completed team audit."""
    url = f"{api_url}/api/agent/audit/events"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=event.model_dump(), headers=_headers(api_key))
        resp.raise_for_status()
    logger.info(
        "audit.client.submit_event",
        event_id=event.event_id,
        findings=len(event.findings),
    )


async def fetch_pending_events(
    api_url: str,
    api_key: str,
    season: str,
) -> list[AuditEvent]:
    """Fetch all pending audit events for processing."""
    url = f"{api_url}/api/agent/audit/events"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            url,
            params={"status": "pending", "season": season},
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        data = resp.json()
    events = [AuditEvent.model_validate(e) for e in data.get("events", [])]
    logger.info("audit.client.fetch_pending_events", count=len(events))
    return events



async def fetch_audit_team_status(
    api_url: str,
    api_key: str,
    season: str,
    league: str,
    division: str,
) -> list[AuditTeamStatus]:
    """Fetch all teams with their audit status for a given league/division/season."""
    url = f"{api_url}/api/agent/audit/teams"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            url,
            params={"season": season, "league": league, "division": division},
            headers=_headers(api_key),
        )
        resp.raise_for_status()
        data = resp.json()
    teams = [AuditTeamStatus.model_validate(t) for t in data.get("teams", [])]
    logger.info("audit.client.fetch_audit_team_status", count=len(teams))
    return teams


async def mark_event_processed(
    api_url: str,
    api_key: str,
    event_id: str,
) -> None:
    """Mark an audit event as processed after corrections have been submitted."""
    url = f"{api_url}/api/agent/audit/events/{event_id}"
    payload = {
        "status": "processed",
        "processed_at": datetime.now(tz=UTC).isoformat(),
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.patch(url, json=payload, headers=_headers(api_key))
        resp.raise_for_status()
    logger.info("audit.client.mark_processed", event_id=event_id)
