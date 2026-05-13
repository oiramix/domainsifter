# DomainSifter — Master Plan

**Project:** DomainSifter — daily-curated list of expired domains worth registering
**URL:** https://domainsifter.com
**Repo:** https://github.com/oiramix/domainsifter
**Stack:** Astro 4 + Tailwind static site, Cloudflare Pages, GitHub Actions for data, Python 3.11
**Owner:** Mario-Martin, Estonia (independent project)
**Started:** April 25, 2026

---

## Mission

Help users find legitimately-available, valuable expired domains by filtering out the 95%+ of daily drops that are spam, malware, adult, abusive, or otherwise junk. Users see a tight curated daily list (~500 high-quality candidates), each linkable to ICANN-accredited registrars via affiliate links.

## Non-goals

We are NOT building:

- A general domain marketplace
- A WHOIS lookup service
- An auction platform
- A bulk domain export tool
- A backorder/drop-catching service

If a contributor proposes a feature outside the daily-curated-list scope, the answer is no for v1–v3.

---

## Core architecture (stable across all versions)

### Data flow (unchanging)

CZDS zone files → diff → enrichment → filter → score → daily JSON → static site rebuild

### Tech stack (locked)

- Frontend: Astro 4 + Tailwind, Cloudflare Pages
- Pipeline: Python 3.11, GitHub Actions runner (free tier, public repo)
- Storage: None on owner's laptop. Yesterday's domain lists committed to repo under scripts/state/ for v1; migrate to Cloudflare R2 in v2.
- Email: Cloudflare Email Routing inbound + Brevo SMTP outbound (Gmail Send-as)
- Domain/DNS: Namecheap registrar, Cloudflare DNS

### Cost target

- v1: 0 EUR per month operating cost
- v2+: ~30 EUR per month accepted only when monthly affiliate revenue exceeds 100 EUR

---

## Phase roadmap (full vision)

### Phase 1 — V1 Launch Foundation (Weeks 1 to 4)

Goal: Daily auto-updating list goes live with real data. Site monetizes via affiliate clicks.

Scope:

1. Daily pipeline: GitHub Actions cron at 06:00 UTC.
2. TLD coverage: All 11 already-approved TLDs (.app, .dev, .live, .studio, .tech, .online, .site, .store, .xyz, .info, .org); add .com, .net, .shop, .biz when approved.
3. Enrichment sources (free):
   - Wayback Machine CDX API (snapshot count, last snapshot date)
   - OpenPageRank (authority score)
   - Google Safe Browsing API (malware/phishing/social engineering check)
   - SURBL DNS lookup (spam blocklist)
   - Spamhaus DBL DNS lookup (domain blocklist)
   - crt.sh (certificate transparency — has the domain ever had legit SSL?)
   - RDAP (previous registrar info for display)
4. Filtering rules:
   - REJECT: Safe Browsing flag, SURBL match, Spamhaus DBL match, zero Wayback snapshots, adult/gambling/pharma keyword pattern in domain
   - REJECT: Punycode/IDN domains (v1 only — too noisy)
   - REJECT: Single-character or all-numeric domains
5. Scoring (composite signal):
   - Wayback snapshot count (more = more historical legitimacy)
   - OpenPageRank score (>2 = strong, 0 = weak)
   - Cert history depth (multiple cert renewals = legitimate use)
   - Domain length and readability
6. Output: src/data/daily-domains.json with top 500 candidates per day, replacing sample-domains.json.
7. Affiliate links: Namecheap placeholder for now (https://www.namecheap.com/domains/registration/results/?domain={name}); replace with real affiliate IDs as approvals land.
8. Newsletter capture: Buttondown integration on existing email signup form — SHIPPED 2026-04-30 (commit `97fbdca`). Public embed endpoint `https://buttondown.com/api/emails/embed-subscribe/domainsifter`, no API key in client. Account created and validated end-to-end 2026-05-13 with first organic subscriber.

v1 deliverables:

- .github/workflows/daily-diff.yml — cron workflow
- scripts/pipeline.py — orchestrator
- scripts/czds_client.py — CZDS API auth + download
- scripts/zone_parser.py — streaming zone file parser, domain extraction
- scripts/diff.py — yesterday vs today set diff
- scripts/enrichment/ — one module per source (wayback.py, open_page_rank.py, spam_check.py, surbl.py, spamhaus.py, crtsh.py, rdap.py)
- scripts/filter.py — reject rules
- scripts/score.py — composite scoring
- scripts/output.py — write JSON
- scripts/state/ — persistent storage of yesterday's domain lists between runs (committed to repo via Actions)
- scripts/config.json — TLD list, thresholds, feature flags
- tests/ — pytest unit tests for each module

v1 success criteria:

- Daily JSON updates automatically, never manually
- Site shows fresh real domains every day
- Pipeline runs in under 30 minutes total
- Zero local processing on owner's laptop
- Pipeline survives a single registry being down (continues with others)

v1 timeline: 2 weeks of build + 2 weeks of stabilization = launch by mid-May 2026.

---

### Phase 2 — Authority and Coverage (Weeks 5 to 10)

Goal: Better data quality and broader TLD coverage. First paid signal added.

Scope additions:

1. Migrate spam check: Google Safe Browsing → Google Web Risk API (paid, ~30 USD per month). Same module interface, just internal swap. Module is named generically (spam_check.py) so calling code never changes.
2. Common Crawl integration: Pull backlink data from latest Common Crawl monthly index. Use BigQuery free tier or download specific WARC slices. Adds the strongest authority signal we can get for free at scale.
3. Historical data: Move daily snapshots from repo state to Cloudflare R2 (free tier ~10GB). Enables "this domain was first listed N days ago" features.
4. Search and filter UI on the website:
   - Search by keyword
   - Filter by TLD, OPR score range, length, has-cert
   - Sort by score, length, registration date
5. More TLDs: Apply for next batch via CZDS once v1 stable (.club, .world, .email, .solutions, .agency, .guru, .tools, etc.)
6. Newsletter as core product: Daily/weekly email digest of top 50 domains, sent via Brevo to Buttondown subscribers.

v2 deliverables:

- scripts/enrichment/spam_check.py — internal swap from Safe Browsing → Web Risk (no caller changes)
- scripts/enrichment/common_crawl.py — backlink graph queries
- scripts/historical.py — snapshot persistence to Cloudflare R2
- Frontend: filter/search UI components in Astro
- scripts/newsletter.py — daily/weekly digest generation, Buttondown API push
- Buttondown account upgrade if subscriber count crosses free tier (1k subs)

v2 success criteria:

- 50+ TLDs covered
- Newsletter sent daily without manual intervention
- Site has searchable archive
- Revenue: minimum 100 EUR per month from affiliates (this unblocks paid Web Risk)

v2 timeline: 6 weeks. Target completion: end of June 2026.

---

### Phase 3 — Premium Tier and API (Months 4 to 6)

Goal: Paid SaaS layer. 19 EUR per month custom alerts and API access for power users.

Scope additions:

1. User accounts: Email + password auth via Clerk or similar (or build minimal own auth — TBD based on v2 learnings)
2. Custom filter alerts: User defines criteria (e.g., "alert me when a .com domain with OPR >= 5 and length < 10 drops"); daily check, email if matches.
3. Public API: REST endpoints for paid users to query the filtered drops programmatically
4. Saved searches and watchlists
5. Affiliate revenue sharing: Direct integrations with more registrars (Porkbun, Dynadot, Sav), better commission rates than impact.com networks

v3 deliverables:

- Backend service (likely Cloudflare Workers + D1 SQLite, or Hetzner VPS if Workers proves limiting)
- Auth system
- Subscription billing (Stripe or LemonSqueezy)
- Customer dashboard
- API documentation site
- Email alert engine

v3 success criteria:

- 50 paying customers (~950 EUR per month MRR)
- API uptime > 99.5%
- Estonian OÜ registered (revenue justifies the entity)

v3 timeline: 3 months. Target completion: end of September 2026.

---

### Phase 4 — Scale and Specialization (Months 7+)

Goal: Become the reference tool for serious domain researchers.

Possible directions (not committed):

- Auction integration: Show GoDaddy/NameJet/Sedo auction data alongside drops
- AI-generated domain analysis: "This domain was used 2018-2023 as a fitness blog, has 47 backlinks, expired due to non-renewal" — Claude API summary per domain
- Browser extension: Right-click any domain on the web → see DomainSifter analysis
- Bulk analysis tool: Upload list of 1000 domains, get filtered/scored output
- Industry partnerships: White-label feed for hosting companies, registrar partners

Decision criteria for Phase 4: only after Phase 3 reaches 5k EUR per month MRR.

---

## Architectural principles (Claude Code, follow these always)

### Principle 1: Pluggable enrichment

Every data source is its own module under scripts/enrichment/ with a uniform interface:

```python
def enrich(domain: str, config: dict) -> dict:
"""Return a dict of fields this source provides for the domain.
   Returns empty dict if source is unavailable or domain not found.
   Never raises — log errors and return empty.
"""
```

Why: We're swapping Safe Browsing → Web Risk in v2, adding Common Crawl in v2, adding more in v3. Plugin architecture means adding/swapping is one file, not a rewrite.

### Principle 2: Each source is independently failable

If Wayback is down today, the pipeline runs with Wayback fields empty for today's batch. It does NOT crash. Log the failure, continue with other sources.

Why: Single-point-of-failure pipelines are how indie projects die. Resilience > completeness for daily updates.

### Principle 3: All thresholds in config, never hardcoded

scripts/config.json holds all magic numbers. Example:

```json
{
"filter_thresholds": {
"min_wayback_snapshots": 1,
"max_domain_length": 30,
"min_open_page_rank": 0
},
"scoring_weights": {
"wayback": 0.3,
"open_page_rank": 0.4,
"cert_history": 0.2,
"length": 0.1
},
"rejected_keywords": ["porn", "casino", "viagra", "..."],
"tlds": ["com", "net", "org", "..."],
"max_candidates_per_day": 500
}
```

Why: Threshold tuning happens weekly in early phases. We don't want to redeploy pipeline code to change "min OPR from 0 to 1."

### Principle 4: State lives in the repo (for now)

Yesterday's domain lists are committed to the repo under scripts/state/. Yes, this means daily commits. Yes, the repo grows. We accept this for v1 because it's free and simple.

In v2, when historical data starts mattering, we move to Cloudflare R2.

### Principle 5: Output is the contract

The Astro site reads src/data/daily-domains.json. The pipeline writes that file. Format is locked:
```json
{
"generated_at": "2026-04-27T06:00:00Z",
"domain_count": 487,
"domains": [
{
"name": "example.com",
"tld": "com",
"dropped_date": "2026-04-26",
"wayback_snapshots": 142,
"wayback_last_snapshot": "2024-08-15",
"open_page_rank": 3.7,
"cert_history": true,
"previous_registrar": "GoDaddy",
"score": 78,
"affiliate_link": "https://..."
}
]
}
```

Pipeline produces this. Site consumes this. Neither side surprises the other.

### Principle 6: Spam check is named generically

Module is scripts/enrichment/spam_check.py, not safe_browsing.py. The module currently calls Google Safe Browsing in v1 and Google Web Risk in v2, but the calling code never knows or cares.

Why: Migration in v2 = one file change, not a sprawl.

### Principle 7: Observability built in from day one

Every pipeline run logs to GitHub Actions output. Key metrics emitted:

- Total drops detected per TLD
- Drops surviving each filter stage
- API calls made per source, with success/failure counts
- Total runtime
- Final candidate count

Why: When something breaks in week 8, we want to read logs, not guess.

---

## Timeline summary

| Phase | Weeks | Target completion | Key milestone |
|---|---|---|---|
| V1 Foundation | 1 to 4 | Mid-May 2026 | Daily auto-updating list live |
| V2 Authority and Coverage | 5 to 10 | End of June 2026 | Newsletter + 50 TLDs + searchable archive |
| V3 Premium Tier | 11 to 22 | End of September 2026 | First 1k EUR MRR, OÜ registered |
| V4 Scale | 23+ | TBD | Decision point at 5k EUR MRR |

---

## Next milestone — Common Crawl backlink integration (Phase 2 first deliverable)

Phase 2's bullet list above includes Common Crawl integration as item 2. This section makes that bullet concrete: it's the next planned deliverable after V1 Foundation finishes, scoped at 4–5 hours of focused work, **planned, not started**.

**Description.** Add an independent quality signal to the enrichment chain by querying Common Crawl's host-graph edges file for the count of inbound hosts that link to each candidate apex. Quarterly refresh of the host-graph into Cloudflare R2 (~50 GB Parquet); per-domain point-lookups via DuckDB over R2 range reads. New `cc_inbound_hosts` field flows through the existing plugin-contract enrichment pipeline; a new term in the scoring function (with a tunable weight in `scripts/config.json`) combines it with the existing Wayback signal.

**Rationale.** Wayback answers *"did this site exist over time?"* (temporal evidence). Common Crawl answers *"did other sites think this site mattered?"* (link evidence). The two are independent — a name with high Wayback AND non-trivial inbound-host count is much harder to fake than either signal alone. The 2026-05-08 published cohort surfaced the first OpenPageRank-positive Promising candidates; Common Crawl would corroborate that authority signal independently and at higher resolution than OpenPageRank's coarse 0–10 bucketing.

**Status.** Planned, scoped, not started. Architecture matches Principle 1 (plugin enrichment) and Principle 3 (thresholds in config); no infrastructure additions beyond a `pip install duckdb`.

**Trigger to start.** This week's pipeline stability (week ending 2026-05-08) holding through at least one more clean cron run. See [STATE.md Wave 2 section](STATE.md) for the empirical state.

**Anti-trigger (do NOT start before this).** If Verisign / `.com` `.net` land in CZDS during the trigger window, finish CC integration FIRST so the 10× volume jump benefits from the additional signal. If a regression appears in the existing pipeline, fix that first.