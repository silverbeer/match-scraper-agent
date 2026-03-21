"""Async httpx client wrapping all MT audit API calls."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

from audit.models import AuditEvent, NextTeamResponse

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
) -> list[dict]:
    """Fetch individual match records for a team from MT."""
    url = f"{api_url}/api/agent/matches"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            url,
            params={
                "age_group": age_group,
                "league": league,
                "division": division,
                "team": team,
                "season": season,
            },
            headers=_headers(api_key),
        )
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


async def cancel_match(
    api_url: str,
    api_key: str,
    home_team: str,
    away_team: str,
    match_date: str,
    age_group: str,
    league: str,
    division: str,
    season: str,
) -> bool:
    """Mark a match as cancelled in MT. Returns True if found and cancelled."""
    url = f"{api_url}/api/agent/matches/cancel"
    payload = {
        "home_team": home_team,
        "away_team": away_team,
        "match_date": match_date,
        "age_group": age_group,
        "league": league,
        "division": division,
        "season": season,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.patch(url, json=payload, headers=_headers(api_key))
        if resp.status_code == 404:
            logger.warning(
                "audit.client.cancel_match.not_found",
                home_team=home_team,
                away_team=away_team,
                match_date=match_date,
            )
            return False
        resp.raise_for_status()
    logger.info(
        "audit.client.cancel_match",
        home_team=home_team,
        away_team=away_team,
        match_date=match_date,
    )
    return True


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
