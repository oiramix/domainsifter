# CLAUDE.md — DomainSifter Build Conventions

Read this file at the start of every session. It contains the hard rules and conventions for working on this project.

For project context, also read:

- PLAN.md — master plan, phases, timeline
- STATE.md — current state of the project, what's built, what's next

---

## Project mission (one sentence)

DomainSifter publishes a daily-curated list of recently-dropped domains, filtered aggressively for spam, malware, and abuse, monetized via affiliate links to ICANN-accredited registrars.

## Current phase

Phase 1 — V1 Foundation. See PLAN.md for full scope and STATE.md for what's built so far.

---

## Hard rules — never violate

### Data integrity rules

1. NEVER use real registered domains in sample data, tests, or fixtures. Only invented combinations like marketglow.com, tideblock.io, coppernest.org. Real domains in test data create legal risk and confuse production logic.

2. NEVER invent launch dates, user counts, testimonials, fake "as seen on" logos, or any other social proof. The product launches with real numbers or none at all.

3. NEVER hardcode credentials. All API keys, passwords, and tokens must come from GitHub Secrets via os.environ. If you find yourself typing a key into a file, stop.

4. NEVER commit zone files, decompressed or otherwise. They're huge and licensed. Pipeline must stream-process and discard. The only zone-derived data that gets committed is the deduplicated domain list under scripts/state/ (text, one domain per line) and the filtered daily-domains.json.

### Scope rules

5. NEVER add features not requested. No chatbot. No AI widgets. No popup interrupters. No "let me also build X while I'm here." Stick to the explicit scope of the current task.

6. NEVER expand beyond the current phase scope. If a Phase 2 idea appears tempting during Phase 1 work, write it down in STATE.md under "Future ideas" and continue with the Phase 1 task.

7. The tech stack is locked. Astro static + Tailwind + vanilla JS for frontend. Python 3.11 + standard library + requests for pipeline. No React/Vue/Svelte. No fancy ML libraries. No databases for v1.

### Architectural rules

8. All enrichment sources follow the plugin interface (see PLAN.md Principle 1). One module per source under scripts/enrichment/. Uniform enrich(domain, config) -> dict signature. Returns empty dict on failure, never raises.

9. All thresholds and magic numbers live in scripts/config.json. Never hardcode "if score > 50" in Python. Read from config.

10. The output JSON contract is locked (see PLAN.md Principle 5). Pipeline produces it, site consumes it. If you need to change the schema, update both sides plus document the breaking change in STATE.md.

11. Each enrichment source must be independently failable. A failed Wayback API call must not crash the pipeline — log the error, return empty fields for that source, continue.

12. The spam check module is named spam_check.py, not safe_browsing.py. Internal implementation may change (Safe Browsing in v1, Web Risk in v2), calling code never knows.

### Code quality rules

13. Every pipeline module must have a corresponding test file in tests/. Tests use pytest. Tests use mocked HTTP responses (via responses library or unittest.mock), never hit live APIs.

14. Logging, not print. Use Python's logging module configured to write to GitHub Actions output. Each module gets a logger named after itself.

15. Type hints required for all function signatures. Use dict, list, str | None style (Python 3.11+ syntax).

16. No global state in pipeline modules. Functions take config as a parameter, return values, never mutate module-level variables.

17. Errors that should crash the pipeline: authentication failures, missing config, invalid JSON output. Errors that should NOT crash: single API source down, single TLD failing, individual domain enrichment failing. Use try/except defensively at the source level, not the pipeline level.

### Frontend rules (for completeness — pipeline work won't touch these usually)

18. All affiliate links use the configurable Namecheap placeholder pattern with ?domain={name} until real affiliate IDs land. The link template lives in config, not hardcoded in components.

19. Do not modify the existing site components (Header.astro, Footer.astro, DomainTable.astro, etc.) when working on the pipeline. The integration point is src/data/daily-domains.json. Pipeline writes that file, site consumes it. No other coupling.

### Operational rules

20. NEVER move the daily cron trigger earlier than 06:30 UTC without verifying that no recent registry RDAP ban events would still be active at the new time. Registry RDAP servers can return Retry-After bans up to 24 hours (e.g., identitydigital returned 86397s = 24h on 2026-05-05). The 06:30 UTC schedule provides 1+ hour buffer past any 24h cooldown started during the previous day's run. Earlier triggers risk hitting registries before their cooldown expires, which can extend bans or escalate to permanent blocks. Cron is controlled by the Cloudflare Worker domainsifter-cron-trigger, NOT by GitHub Actions schedule:.

---

## Coding conventions

### Python style

- Imports: Standard library first, then third-party, then local. One per line. Sorted alphabetically within each group.
- Functions: Type-hinted. Docstrings for public functions. Private helpers prefixed with underscore.
- Async only when justified. v1 pipeline can be sync. If we need parallelism for enrichment, use concurrent.futures.ThreadPoolExecutor (simpler than asyncio for HTTP-bound work).
- HTTP requests: Use requests library with explicit timeouts (timeout=10). Always handle ConnectionError, Timeout, HTTPError separately.

### File and directory structure

```
domainsifter/
  PLAN.md
  STATE.md
  CLAUDE.md
  README.md
  astro.config.mjs
  package.json
  src/
    pages/
    components/
    layouts/
    data/
      sample-domains.json
      daily-domains.json
  scripts/
    config.json
    pipeline.py
    czds_client.py
    zone_parser.py
    diff.py
    filter.py
    score.py
    output.py
    enrichment/
      __init__.py
      wayback.py
      open_page_rank.py
      spam_check.py
      surbl.py
      spamhaus.py
      crtsh.py
      rdap.py
    state/
      .gitkeep
  tests/
    __init__.py
    test_czds_client.py
    test_zone_parser.py
    test_diff.py
    test_filter.py
    test_score.py
    test_output.py
    enrichment/
      test_wayback.py
      test_open_page_rank.py
  .github/
    workflows/
      daily-diff.yml
```

Notes on the structure:

- PLAN.md, STATE.md, CLAUDE.md, README.md sit at repo root
- Frontend files (astro.config.mjs, package.json, src/) are existing — DO NOT MODIFY from pipeline work
- src/data/daily-domains.json is the integration point — pipeline writes here
- src/data/sample-domains.json is existing fake data — keep as fallback
- All Python pipeline code lives under scripts/
- Yesterday's domain lists go in scripts/state/ (created at runtime by the pipeline)
- Tests mirror the structure of scripts/

### Naming

- Modules and files: snake_case
- Classes: PascalCase
- Functions and variables: snake_case
- Constants: SCREAMING_SNAKE_CASE
- Test files: test_<module_name>.py

### Configuration shape

scripts/config.json example structure (non-exhaustive — extend as needed):

```json
{
"version": "1.0",
"tlds": {
"approved": ["app", "dev", "live", "studio", "tech", "online", "site", "store", "xyz", "info", "org"],
"pending": ["com", "net", "shop", "biz"]
},
"filter_thresholds": {
"min_wayback_snapshots": 1,
"max_domain_length": 30,
"min_open_page_rank": 0
},
"scoring_weights": {
"wayback_snapshots": 0.3,
"open_page_rank": 0.4,
"cert_history": 0.2,
"domain_length": 0.1
},
"rejected_keywords": ["porn", "casino", "viagra", "pharma", "xxx", "adult", "gambling", "betting"],
"max_candidates_per_day": 500,
"affiliate_link_template": "https://www.namecheap.com/domains/registration/results/?domain={name}",
"api_endpoints": {
"czds_base": "https://czds-api.icann.org",
"wayback_cdx": "https://web.archive.org/cdx/search/cdx",
"open_page_rank": "https://openpagerank.com/api/v1.0/getPageRank",
"safe_browsing": "https://safebrowsing.googleapis.com/v4/threatMatches:find",
"crtsh": "https://crt.sh",
"rdap_bootstrap": "https://data.iana.org/rdap/dns.json"
},
"request_timeout_seconds": 10,
"max_concurrent_enrichments": 10
}

```

## How to run things

### Local development (manual testing)

```bash
Set environment variables (use a .env file, gitignored)
export CZDS_USERNAME="..."
export CZDS_PASSWORD="..."
export OPENPAGERANK_KEY="..."
export SAFE_BROWSING_KEY="..."
Run the pipeline
cd scripts
python pipeline.py
Run tests
cd ..
pytest tests/
```

### GitHub Actions

The workflow daily-diff.yml runs at 06:30 UTC daily via Cloudflare Worker domainsifter-cron-trigger (changed from 05:17 UTC on 2026-05-05 after registry ban events; see Hard rule 20) and on manual trigger. Secrets are read from repo settings.

To manually trigger: GitHub repo → Actions tab → "Daily Domain Diff" workflow → "Run workflow" button.

---

## How to update this file

This file should remain stable. If a convention needs to change:

1. Discuss in a session, agree on the new rule
2. Update this file with the new rule + a one-line change note at the top of the relevant section
3. Update STATE.md to reflect the new convention is active
4. Apply the new convention to all new code; only retrofit existing code if it materially affects current work

---

## Final note for Claude Code

You're building a real production system that will run unattended every day, indefinitely. Quality matters. Resilience matters. Boring, predictable code matters more than clever code.

When in doubt, pick the simpler option. When tempted to abstract early, don't. When tempted to add a feature that "would be nice," check PLAN.md and STATE.md first.

The owner is a solo founder in Estonia who values narrow scope and gets fatigued by long sessions. Build in a way that future sessions (yours or another Claude's) can quickly resume. That means: small files, clear function names, comments where intent is non-obvious, tests that document behavior.

Begin with STATE.md step "What Claude Code should do next." Build in the order listed there. After each major milestone, update STATE.md to reflect new state.

Good building.