"""Pydantic v2 models for the audit module."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class AuditFinding(BaseModel):
    finding_type: Literal[
        "missing_in_mt", "extra_in_mt", "score_mismatch", "status_mismatch", "time_mismatch"
    ]
    home_team: str
    away_team: str
    match_date: str
    external_match_id: str | None = None
    field: str | None = None  # "score" | "match_status" | "match_time"
    scraped_value: str | None = None
    mt_value: str | None = None
    scraped_match: dict[str, Any] | None = None  # full dict for re-submission by processor


class AuditEvent(BaseModel):
    event_id: str
    audit_run_id: str
    team: str
    age_group: str
    league: str
    division: str
    season: str
    findings: list[AuditFinding]
    status: Literal["pending", "processed", "ignored"] = "pending"


class NextTeamResponse(BaseModel):
    team: str
    age_group: str
    league: str
    division: str
    season: str
    last_audited_at: str | None = None


class AuditRunResult(BaseModel):
    team: str
    age_group: str
    scraped_count: int
    mt_count: int
    findings: list[AuditFinding]
    dry_run: bool = False


class ProcessResult(BaseModel):
    events_processed: int
    matches_resubmitted: int
    extra_in_mt_flagged: int
    errors: int
