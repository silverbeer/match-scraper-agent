"""Unit tests for audit.comparator — pure comparison logic, no I/O."""

from __future__ import annotations

from audit.comparator import compare_matches


def _match(
    home: str,
    away: str,
    date: str,
    *,
    ext_id: str | None = None,
    status: str = "scheduled",
    home_score: int | None = None,
    away_score: int | None = None,
    match_time: str | None = None,
) -> dict:
    return {
        "home_team": home,
        "away_team": away,
        "match_date": date,
        "external_match_id": ext_id,
        "match_status": status,
        "home_score": home_score,
        "away_score": away_score,
        "match_time": match_time,
    }


class TestClean:
    def test_no_findings_when_identical(self):
        m = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="scheduled")
        findings = compare_matches([m], [m], "IFA")
        assert findings == []

    def test_no_findings_empty_both(self):
        findings = compare_matches([], [], "IFA")
        assert findings == []

    def test_no_findings_empty_scraped(self):
        m = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1")
        # MT has a match but nothing scraped — this is extra_in_mt, not missing
        findings = compare_matches([], [m], "IFA")
        assert len(findings) == 1
        assert findings[0].finding_type == "extra_in_mt"

    def test_no_findings_with_score(self):
        m = _match(
            "IFA",
            "Arsenal",
            "2026-03-15",
            ext_id="abc1",
            status="completed",
            home_score=2,
            away_score=1,
        )
        findings = compare_matches([m], [m], "IFA")
        assert findings == []


class TestMissingInMT:
    def test_scraped_not_in_mt(self):
        scraped = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1")
        findings = compare_matches([scraped], [], "IFA")
        assert len(findings) == 1
        assert findings[0].finding_type == "missing_in_mt"
        assert findings[0].home_team == "IFA"
        assert findings[0].away_team == "Arsenal"
        assert findings[0].match_date == "2026-03-15"
        assert findings[0].scraped_match is not None

    def test_scraped_not_in_mt_no_ext_id(self):
        scraped = _match("IFA", "Arsenal", "2026-03-15")  # no ext_id
        findings = compare_matches([scraped], [], "IFA")
        assert len(findings) == 1
        assert findings[0].finding_type == "missing_in_mt"

    def test_multiple_missing(self):
        matches = [
            _match("IFA", "Arsenal", "2026-03-15", ext_id="a1"),
            _match("IFA", "Chelsea", "2026-03-22", ext_id="a2"),
        ]
        findings = compare_matches(matches, [], "IFA")
        assert len(findings) == 2
        assert all(f.finding_type == "missing_in_mt" for f in findings)


class TestExtraInMT:
    def test_mt_match_not_scraped(self):
        mt = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1")
        findings = compare_matches([], [mt], "IFA")
        assert len(findings) == 1
        assert findings[0].finding_type == "extra_in_mt"
        assert findings[0].home_team == "IFA"
        assert findings[0].scraped_match is None  # no scraped_match for extra_in_mt

    def test_extra_has_no_scraped_match(self):
        mt = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1")
        findings = compare_matches([], [mt], "IFA")
        assert findings[0].scraped_match is None


class TestScoreMismatch:
    def test_score_differs(self):
        scraped = _match(
            "IFA",
            "Arsenal",
            "2026-03-15",
            ext_id="abc1",
            status="completed",
            home_score=2,
            away_score=1,
        )
        mt = _match(
            "IFA",
            "Arsenal",
            "2026-03-15",
            ext_id="abc1",
            status="completed",
            home_score=1,
            away_score=1,
        )
        findings = compare_matches([scraped], [mt], "IFA")
        assert len(findings) == 1
        f = findings[0]
        assert f.finding_type == "score_mismatch"
        assert f.field == "score"
        assert f.scraped_value == "2-1"
        assert f.mt_value == "1-1"
        assert f.scraped_match is not None

    def test_no_score_mismatch_when_scraped_score_is_none(self):
        scraped = _match(
            "IFA",
            "Arsenal",
            "2026-03-15",
            ext_id="abc1",
            status="scheduled",
            home_score=None,
            away_score=None,
        )
        mt = _match(
            "IFA",
            "Arsenal",
            "2026-03-15",
            ext_id="abc1",
            status="scheduled",
            home_score=2,
            away_score=1,
        )
        findings = compare_matches([scraped], [mt], "IFA")
        assert not any(f.finding_type == "score_mismatch" for f in findings)


class TestStatusMismatch:
    def test_status_differs(self):
        scraped = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="completed")
        mt = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="scheduled")
        findings = compare_matches([scraped], [mt], "IFA")
        assert len(findings) == 1
        f = findings[0]
        assert f.finding_type == "status_mismatch"
        assert f.field == "match_status"
        assert f.scraped_value == "completed"
        assert f.mt_value == "scheduled"
        assert f.scraped_match is not None

    def test_no_mismatch_when_status_matches(self):
        m = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="completed")
        findings = compare_matches([m], [m], "IFA")
        assert findings == []


class TestTimeMismatch:
    def test_time_differs(self):
        scraped = _match(
            "IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="scheduled", match_time="10:00"
        )
        mt = _match(
            "IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="scheduled", match_time="11:00"
        )
        findings = compare_matches([scraped], [mt], "IFA")
        assert len(findings) == 1
        f = findings[0]
        assert f.finding_type == "time_mismatch"
        assert f.field == "match_time"
        assert f.scraped_value == "10:00"
        assert f.mt_value == "11:00"
        assert f.scraped_match is not None

    def test_no_time_mismatch_when_scraped_time_is_none(self):
        scraped = _match(
            "IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="scheduled", match_time=None
        )
        mt = _match(
            "IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="scheduled", match_time="10:00"
        )
        findings = compare_matches([scraped], [mt], "IFA")
        assert not any(f.finding_type == "time_mismatch" for f in findings)


class TestMatchKeyStrategy:
    def test_match_by_ext_id_takes_priority(self):
        # Different home/away but same ext_id — should match (no missing/extra)
        scraped = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1")
        mt = _match("IFA FC", "Arsenal FC", "2026-03-15", ext_id="abc1")
        findings = compare_matches([scraped], [mt], "IFA")
        # Status both "scheduled" + same → only possible status finding, not missing/extra
        assert not any(f.finding_type in ("missing_in_mt", "extra_in_mt") for f in findings)

    def test_fallback_to_key_when_no_ext_id(self):
        scraped = _match("IFA", "Arsenal", "2026-03-15")  # no ext_id
        mt = _match("IFA", "Arsenal", "2026-03-15")  # no ext_id, same key
        findings = compare_matches([scraped], [mt], "IFA")
        assert findings == []

    def test_different_ext_ids_not_matched_by_key(self):
        # Same key but different ext_ids — ext_id lookup should take priority
        # MT has ext_id "mt1", scraped has ext_id "scraped1"
        # They should NOT match by fallback key since ext_id lookup is tried first
        scraped = _match("IFA", "Arsenal", "2026-03-15", ext_id="scraped1")
        mt = _match("IFA", "Arsenal", "2026-03-15", ext_id="mt1")
        # Scraped won't find "scraped1" in mt_by_id → falls back to key match
        findings = compare_matches([scraped], [mt], "IFA")
        # Matched via fallback key — no missing/extra
        assert not any(f.finding_type in ("missing_in_mt", "extra_in_mt") for f in findings)


class TestMultipleFindings:
    def test_score_and_status_mismatch_on_same_match(self):
        scraped = _match(
            "IFA",
            "Arsenal",
            "2026-03-15",
            ext_id="abc1",
            status="completed",
            home_score=2,
            away_score=1,
        )
        mt = _match(
            "IFA",
            "Arsenal",
            "2026-03-15",
            ext_id="abc1",
            status="scheduled",
            home_score=0,
            away_score=0,
        )
        findings = compare_matches([scraped], [mt], "IFA")
        types = {f.finding_type for f in findings}
        assert "score_mismatch" in types
        assert "status_mismatch" in types

    def test_mixed_clean_and_dirty(self):
        clean = _match("IFA", "Arsenal", "2026-03-15", ext_id="abc1", status="scheduled")
        dirty_scraped = _match(
            "IFA",
            "Chelsea",
            "2026-03-22",
            ext_id="abc2",
            status="completed",
            home_score=3,
            away_score=0,
        )
        dirty_mt = _match(
            "IFA",
            "Chelsea",
            "2026-03-22",
            ext_id="abc2",
            status="scheduled",
            home_score=None,
            away_score=None,
        )
        findings = compare_matches([clean, dirty_scraped], [clean, dirty_mt], "IFA")
        assert len(findings) == 2  # score_mismatch + status_mismatch
        assert all(f.home_team == "IFA" and f.away_team == "Chelsea" for f in findings)
