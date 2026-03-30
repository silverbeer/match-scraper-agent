"""Pydantic v2 models for the audit module."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class AuditFinding(BaseModel):
    finding_type: Literal[
        "missing_in_mt",
        "extra_in_mt",
        "score_mismatch",
        "status_mismatch",
        "time_mismatch",
        "home_away_mismatch",
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
    created_at: str | None = None  # ISO timestamp set by MT on insert


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


class AuditTeamStatus(BaseModel):
    """Per-team audit status record returned by /api/agent/audit/teams."""

    team: str
    age_group: str
    league: str
    division: str
    season: str
    last_audited_at: str | None = None
    last_audit_status: str | None = None
    findings_count: int = 0


class ExtraInMtMatch(BaseModel):
    home_team: str
    away_team: str
    match_date: str
    team: str
    age_group: str


class ProcessResult(BaseModel):
    events_processed: int
    matches_resubmitted: int
    corrections_by_type: dict[str, int] = {}  # finding_type -> count of resubmitted matches
    extra_in_mt_skipped: int  # queued for human review, not auto-cancelled
    extra_in_mt_findings: list[ExtraInMtMatch] = []
    errors: int
