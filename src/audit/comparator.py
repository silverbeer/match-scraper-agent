"""Pure match comparison logic — no I/O, easily unit-tested."""

from __future__ import annotations

from typing import Any

from audit.models import AuditFinding


def compare_matches(
    scraped: list[dict[str, Any]],
    mt_matches: list[dict[str, Any]],
    team: str,
) -> list[AuditFinding]:
    """Compare scraped mlssoccer.com matches against MT records and return findings.

    Match key strategy:
    1. Primary: external_match_id (if both records have it)
    2. Fallback: (home_team, away_team, match_date) tuple

    Args:
        scraped: Match dicts from MLSScraper (normalized team names).
        mt_matches: Match dicts from MT /api/agent/matches.
        team: MT canonical team name being audited (for logging context).

    Returns:
        List of AuditFinding describing discrepancies found.
    """
    findings: list[AuditFinding] = []

    # Build indexed lookup maps for MT matches
    mt_by_id: dict[str, int] = {}  # external_match_id -> list index
    mt_by_key: dict[tuple[str, str, str], int] = {}  # (home, away, date) -> list index

    for i, m in enumerate(mt_matches):
        ext_id = m.get("external_match_id")
        if ext_id:
            mt_by_id[ext_id] = i
        key = (m.get("home_team", ""), m.get("away_team", ""), m.get("match_date", ""))
        mt_by_key[key] = i

    # Track which MT indices were matched (to detect extras)
    matched_mt_indices: set[int] = set()

    for scraped_m in scraped:
        ext_id = scraped_m.get("external_match_id")
        key = (
            scraped_m.get("home_team", ""),
            scraped_m.get("away_team", ""),
            scraped_m.get("match_date", ""),
        )

        # Locate corresponding MT match
        mt_idx: int | None = None
        if ext_id and ext_id in mt_by_id:
            mt_idx = mt_by_id[ext_id]
        elif key in mt_by_key:
            mt_idx = mt_by_key[key]

        if mt_idx is None:
            findings.append(
                AuditFinding(
                    finding_type="missing_in_mt",
                    home_team=scraped_m.get("home_team", ""),
                    away_team=scraped_m.get("away_team", ""),
                    match_date=scraped_m.get("match_date", ""),
                    external_match_id=ext_id,
                    scraped_match=scraped_m,
                )
            )
            continue

        matched_mt_indices.add(mt_idx)
        mt_m = mt_matches[mt_idx]

        # Score mismatch — only flag when scraped has a score
        scraped_hs = scraped_m.get("home_score")
        scraped_as = scraped_m.get("away_score")
        mt_hs = mt_m.get("home_score")
        mt_as = mt_m.get("away_score")
        if scraped_hs is not None and (scraped_hs != mt_hs or scraped_as != mt_as):
            findings.append(
                AuditFinding(
                    finding_type="score_mismatch",
                    home_team=scraped_m.get("home_team", ""),
                    away_team=scraped_m.get("away_team", ""),
                    match_date=scraped_m.get("match_date", ""),
                    external_match_id=ext_id,
                    field="score",
                    scraped_value=f"{scraped_hs}-{scraped_as}",
                    mt_value=f"{mt_hs}-{mt_as}",
                    scraped_match=scraped_m,
                )
            )

        # Status mismatch
        scraped_status = scraped_m.get("match_status")
        mt_status = mt_m.get("match_status")
        if scraped_status and scraped_status != mt_status:
            findings.append(
                AuditFinding(
                    finding_type="status_mismatch",
                    home_team=scraped_m.get("home_team", ""),
                    away_team=scraped_m.get("away_team", ""),
                    match_date=scraped_m.get("match_date", ""),
                    external_match_id=ext_id,
                    field="match_status",
                    scraped_value=scraped_status,
                    mt_value=mt_status,
                    scraped_match=scraped_m,
                )
            )

        # Time mismatch — only flag when scraped has a time
        scraped_time = scraped_m.get("match_time")
        mt_time = mt_m.get("match_time")
        if scraped_time is not None and scraped_time != mt_time:
            findings.append(
                AuditFinding(
                    finding_type="time_mismatch",
                    home_team=scraped_m.get("home_team", ""),
                    away_team=scraped_m.get("away_team", ""),
                    match_date=scraped_m.get("match_date", ""),
                    external_match_id=ext_id,
                    field="match_time",
                    scraped_value=scraped_time,
                    mt_value=mt_time,
                    scraped_match=scraped_m,
                )
            )

    # Build a set of scraped keys for extra_in_mt detection
    scraped_keys: set[tuple[str, str, str]] = {
        (m.get("home_team", ""), m.get("away_team", ""), m.get("match_date", "")) for m in scraped
    }
    scraped_ids: set[str] = {m["external_match_id"] for m in scraped if m.get("external_match_id")}

    # Extra in MT: MT matches with no corresponding scraped match
    for i, mt_m in enumerate(mt_matches):
        if i in matched_mt_indices:
            continue
        mt_ext_id = mt_m.get("external_match_id")
        mt_key = (mt_m.get("home_team", ""), mt_m.get("away_team", ""), mt_m.get("match_date", ""))

        # Double-check via both id and key to avoid false positives
        if mt_ext_id and mt_ext_id in scraped_ids:
            continue
        if mt_key in scraped_keys:
            continue

        findings.append(
            AuditFinding(
                finding_type="extra_in_mt",
                home_team=mt_m.get("home_team", ""),
                away_team=mt_m.get("away_team", ""),
                match_date=mt_m.get("match_date", ""),
                external_match_id=mt_ext_id,
            )
        )

    return findings
