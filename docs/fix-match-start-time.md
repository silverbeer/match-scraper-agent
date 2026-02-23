# Fix: Flow match start time through the pipeline

## Problem

The MLS Next scraper extracts full datetime (`match_datetime`) including kick-off
time, but only the date portion reaches the database. The time is discarded in
`match-scraper-agent/src/agent/tools.py`:

```python
"match_date": m.match_datetime.date().isoformat(),  # time thrown away
```

The missing-table `matches` table already has a `match_time TIME` column, but
nothing populates it.

## Root cause

The match message contract (`match-message-schema.json` v1.1.0) has no
`match_time` field and sets `additionalProperties: false`. Even if the agent
sent it, the consumer would reject it.

## Changes required (3 repos, in order)

### 1. missing-table (consumer — do first)

**Schema** — `docs/08-integrations/match-message-schema.json`

Add `match_time` to `properties`:

```json
"match_time": {
  "type": ["string", "null"],
  "pattern": "^\\d{2}:\\d{2}$",
  "description": "Match kick-off time in HH:MM format (24-hour, null if TBD)",
  "example": "14:00"
}
```

Bump version to `1.2.0`.

**Celery task** — find the task that processes match messages and write
`match_time` to the DB when present. The column already exists; it just
needs to be included in the INSERT/upsert.

**Contract test** — update any contract tests to cover the new field.

### 2. match-scraper (producer model)

**Model** — `src/models/match_data.py`

Add optional field to `MatchData`:

```python
match_time: str | None = Field(
    None,
    pattern=r"^\d{2}:\d{2}$",
    description="Match kick-off time HH:MM (24h)",
)
```

**Contract test** — validate the updated model matches the v1.2.0 schema.

### 3. match-scraper-agent (bridge)

**Tool** — `src/agent/tools.py` in the `built` list comprehension

Add one line after `match_date`:

```python
"match_time": m.match_datetime.strftime("%H:%M") if m.match_datetime.hour or m.match_datetime.minute else None,
```

The `hour or minute` check avoids sending `"00:00"` for matches where the
scraper defaulted to midnight (meaning time was not available).

## Verification

1. missing-table: contract tests pass, Celery task writes match_time to DB
2. match-scraper: `uv run pytest` — MatchData validates with match_time
3. match-scraper-agent: `cd tests && uv run pytest` — tools tests pass
4. E2E: trigger a local dry run, inspect queue message, confirm match_time present

## Notes

- Changes must be deployed in order: consumer first, then producer, then agent.
  The consumer must accept the new field before anyone sends it.
- `match_time` is nullable — old messages without it are still valid.
- The scraper already has the data; this is purely a plumbing fix.
