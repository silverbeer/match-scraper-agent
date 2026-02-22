# Open Issues

## 1. Duplicate matches from mls-match-scraper pagination

**Discovered:** 2026-02-21
**Repo:** `mls-match-scraper` (not this repo)
**Severity:** High — causes duplicate submissions to RabbitMQ/missing-table

### Problem

Running `match-scraper-agent scrape --target u14-hg-ifa` returns 404 total matches but many are duplicates. Example: **IFA vs Ironbound Soccer Club (2026-05-03, match_id 99972)** appears 16 times in the output.

The scraper appears to be paginating through MLS Next results and re-scraping the same matches on each page without deduplication.

### How to reproduce

```bash
uv run match-scraper-agent scrape --target u14-hg-ifa
# Output shows 404 matches, 25 after IFA filter — but 16 of those are the same game
```

### Expected behavior

Each unique `match_id` should appear exactly once. The scraper should deduplicate by `match_id` before returning results.

### Fix options

1. **In mls-match-scraper:** Deduplicate matches by `match_id` in `MLSScraper.scrape_matches()` before returning
2. **In match-scraper-agent (workaround):** Deduplicate in the `scrape` CLI command or in `tools.py` after scraping

Option 1 is the right fix — the library should not return duplicates.

### Relevant log lines

```
SCHEDULED #332: Intercontinental Football Academy of New England vs Ironbound Soccer Club
  match_id: 99972, date: 2026-05-03, time: 04:00 PM
  venue: SBLI Fields at Progin Park - Field 10

# Same match_id 99972 repeats as #351, #370, #389 ... (16 total times)
```

---

## 2. New `scrape` CLI command added (2026-02-21)

**Status:** Working

Added `match-scraper-agent scrape --target <target>` command that runs the Playwright scraper directly — no LLM, no API key, no proxy, no RabbitMQ needed. Useful for testing scraping logic locally.

```bash
uv run match-scraper-agent scrape --target u14-hg-ifa        # human-readable
uv run match-scraper-agent scrape --target u14-hg-ifa --json  # raw match dicts
```

### Other changes in this session

- `src/config/settings.py`: Added `"extra": "ignore"` to `model_config` so pydantic-settings ignores unknown `AGENT_*` env vars (e.g. `AGENT_KUBE_CONTEXT` in `.env.local`)
- `src/cli/main.py`: Added `_TARGET_SCRAPER_CONFIG` dict and `scrape` command

### Not yet committed
