# Strategic notes

Long-horizon decisions that don't fit neatly into PLAN.md (which scopes the current phase) or STATE.md (which logs current state and recent changes). Use this file when the decision being captured will outlast multiple phase transitions, or when the *reasoning* behind a choice matters more than its current implementation status.

Index of decisions:

- [Common Crawl integration — accumulation strategy (2026-05-13)](#common-crawl-integration--accumulation-strategy-2026-05-13)
- [Multi-release CC query strategy (2026-05-13 — Strategy A active 2026-05-14)](#multi-release-cc-query-strategy-2026-05-13-pending)
- [Free vs paid tier model (2026-05-13, pending)](#free-vs-paid-tier-model-2026-05-13-pending)
- [Daily publication count cap (2026-05-13, pending)](#daily-publication-count-cap-2026-05-13-pending)
- [Common Crawl refresh cadence — manual vs automated (2026-05-13, pending)](#common-crawl-refresh-cadence--manual-vs-automated-2026-05-13-pending)

---

## Common Crawl integration — accumulation strategy (2026-05-13)

### The decision

Common Crawl publishes a new domain-webgraph release every month (rolling 3-month crawl window). Each release lands on R2 as ~21 GiB raw artifacts + ~1.5 GiB derived SQLite. **We never delete old releases.**

The cumulative R2 storage cost reaches ~$14/month at the 5-year mark (~$420 total over 5 years). Trivial for what we get in return: a multi-year, monthly-granularity backlink dataset that almost nobody else publishes freely, queryable in SQLite, ready for retrospective scoring or trend analysis whenever future product directions need it.

### Why this matters more than the immediate use case

The immediate use case is "use the latest release's `source_domain_count` as one input to the daily scoring of dropped domains." That alone is worth building — current scoring already has Wayback signal, OpenPageRank, cert history, and lexical features; backlink-graph data adds an independent quality dimension. But that's the obvious application and not the strategic part.

The strategic part is **decay velocity**. When a domain is freshly dropped on day D, its CC release on D−30 still contains the link graph as the rest of the web saw it 1–4 months ago (CC crawl window). If a domain had 800 inbound source domains in Feb 2026 and only 12 in May 2026, that's a steep decay signal — far more informative than the single point. **We can't measure that without historical retention.**

This is the kind of capability that's expensive to acquire retroactively (you can't go back in time and download the Feb 2026 release after they're stale) and cheap to acquire prospectively (just don't delete). The asymmetry decides it.

### What this enables (eventually, not now)

In rough order of how soon we might use them:

1. **Latest-release scoring** — `cc_source_domain_count` as a scoring input. The next commit after this one, conditional on validation. (Phase 2 wave 1.)

2. **Multi-release decay scoring** — compare a candidate's count across the last 3–6 monthly releases; score higher when the decay curve is flatter (sticky links from authoritative sources tend to persist; cheap link-farm backlinks evaporate fast). (Phase 2 wave 2 or 3.)

3. **Long-term backlink-history product feature** — once we have 12+ months of accumulated data, the per-domain detail page (currently a 404; see STATUS.md) could surface "this domain had N inbound source domains a year ago; M today." That's a unique signal nobody else has packaged for the drop-catcher audience. (Phase 3 or later.)

4. **Aggregate / research outputs** — once we have years, the corpus becomes interesting for "what does post-drop link decay look like across the .com vs .org universe?" type questions. Could become a blog post, a Reddit r/dataisbeautiful submission, an industry-report basis, etc. Not a primary goal but a free option. (Far future.)

### Why we resisted simpler alternatives

A few framings we explicitly rejected:

- **Rolling-window retention (e.g., keep last 12 releases, delete older)**: would cost ~$3/month flat instead of growing. Saves us ~$10/month at year 5. Costs us every long-horizon scoring application above. Bad trade.

- **Don't store raw; only derived**: would save 21 GiB × $0.010 = ~$0.21/release/month on IA, but loses the option to rebuild derived with a different schema later. Useful when (not if) we add `cc_outbound_count` to the schema. The full raw also enables host-level webgraph experiments without a re-download. Marginal cost, big optionality value.

- **Periodically compact old releases**: e.g. delete raw older than 24 months. Same logic — cheap to keep, expensive to re-acquire. Not now. Maybe at year 10 if costs balloon for some reason.

- **External cold storage (Backblaze B2, AWS Glacier Deep Archive)**: cheaper per GB ($0.005/GB-mo for B2; $0.00099/GB-mo for Glacier Deep) but adds operational complexity (separate credential set, separate egress fees, multi-hour rehydrate latency for Glacier). Not worth the savings until cumulative R2 cost crosses ~$30/month, which is year 10+ at current trajectory.

### Validation gate before scaling reliance

Before adding `cc_backlinks` to the scoring weights or surfacing backlink counts on the homepage, we want at least one full validation cycle:

1. `cc_refresh.py` completes end-to-end on OVH against real CC data (~25-35 min wall-clock, ~22 GiB transferred). The agent's smoke tests covered the build path against fixture data; the real run exercises file sizes, DuckDB spill behavior on real 5.4B-edge data, and R2 upload throughput.
2. CLI sanity-check on known anchors (`google.com` should have a large N; invented names should return "not in graph").
3. SQLite shape verification: row count near the ~134M expected, file size near 1.5 GiB, meta table populated.
4. Once those pass, a follow-up commit registers `cc_backlinks` in `ENRICHMENT_MODULES` and adds a scoring weight.

If any of those steps reveals a wrong assumption, we have a working standalone capability to debug against without contaminating the live pipeline.

### Operational notes (carry forward)

- **Refresh cadence**: monthly, to match CC's release cadence. NOT automated as a cron yet. When we wire in, add a monthly systemd timer on OVH — first of each month, retry-with-backoff if CC hasn't published yet.
- **Release-name source of truth**: `config.json[cc_backlinks].latest_release`. Bumping this string is part of the post-refresh commit each month (so the enricher queries the latest data).
- **R2 cleanup discipline**: there is no cleanup. Manually checking `cc/raw/` and `cc/derived/` prefixes in the R2 dashboard should show monotonic growth. Any deletion is a bug; raise alarm.
- **Wayback substitute via CC URL-columnar-index**: separate future task. Not part of this strategic accumulation. Will be its own scoped decision when we pick it up.

---

## Multi-release CC query strategy (2026-05-13 — Strategy A active 2026-05-14)

### Status update — 2026-05-14

**Strategy A is now active in production.** The 2026-05-14 wire-in commit registered `cc_backlinks` in `ENRICHMENT_MODULES` with Strategy A — the enricher queries the single latest release configured in `config["cc_backlinks"]["latest_release"]`. Strategies B (union/max across last N) and C (full historical aggregate) remain deferred. The re-evaluation criteria below stand: revisit when ~6 releases have accumulated (late 2026), or when product positioning shifts toward decay-curve as a feature.

### The question

The enricher `scripts/enrichment/cc_backlinks.py` opens ONE derived SQLite per process. Today that's the latest release. As more monthly releases accumulate in R2 (year 1: 12, year 5: 60), what shape should the per-candidate query take?

### Three strategies considered

**Strategy A — latest release only.** Query the most recent release's `cc_apex`; return whatever it says (count N, dangler 0, or row-absent → `{}`). Simple, fast (~ms per lookup), no joins. Today's behaviour.

**Strategy B — union/max across the last N releases.** For a candidate apex, query the last N SQLites and return `MAX(source_domain_count)` across them. Catches "domain had 800 inbound 3 months ago but the latest crawl only saw 12" — protects against transient crawl noise. ATTACH multiple SQLite files in `sqlite3` and run a single UNION query; cost is N × point-lookup ≈ still sub-ms for N ≤ 6.

**Strategy C — full historical aggregate.** Query EVERY accumulated release; return both the max and a decay-curve signal (e.g. `[count_m12, count_m6, count_m3, count_m1]`). Strongest moat — nobody else has this data — but slowest lookup as N grows. At 60 releases, opening 60 SQLite files in a single process and ATTACHing them might hit OS file-handle limits; needs a different shape (e.g. a precomputed cross-release index).

### Provisional answer

**Strategy A for the wire-in commit.** Two reasons:

1. We have exactly one release today. Strategies B and C don't have data to act on yet.
2. The pipeline's enrichment time budget is tight (~5-50 candidates per run today; 3000s budget). Strategy A's sub-ms point-lookup is irrelevant to that budget; Strategy B/C with 6+ ATTACHed files start mattering at scale.

### When to re-evaluate

When we have ~6 accumulated releases (i.e., late 2026): re-evaluate B vs C. Decision criteria:

- **Does the latest-release count vary materially from the 6-month max?** Run an offline analysis of, say, 1,000 sampled apex names from yesterday's `daily-domains.json`. Compare `latest_count` vs `MAX(last_6)`. If the median delta is >2×, Strategy B is worth the engineering cost. If it's <1.2×, Strategy A is fine and B is over-engineering.
- **Has product positioning shifted toward decay-curve as a feature?** If the homepage UI ever surfaces "this domain's backlink trajectory" or paid-tier offers "12-month historical view", Strategy C lands automatically because that's the strategy paid-tier feature actually needs.

Strategy C is the long-horizon target IFF historical backlink decay becomes a paid-tier feature; otherwise it's premature optimisation.

### Implementation note (for whoever does the wire-in)

The enricher's current `_get_connection(release, config)` already opens ONE connection cached by release name. Strategy B is additive: extend the cache key to a tuple of release names, ATTACH each, and rewrite the SQL to a UNION. Strategy A → B is a ~30-line change. B → C is the harder leap because the SQLite-per-release pattern starts groaning past ~10 attached files.

---

## Free vs paid tier model (2026-05-13, pending)

### The proposition

Articulated during tonight's discussion. The product naturally splits along two dimensions: **data freshness/depth** and **access mode**.

**Free tier** (today's default):
- Top 30–50 daily candidates surfaced on the homepage
- 14-day rolling window (carryover already implements this)
- **Static** CC backlink count from the latest release (latency: refreshed monthly)
- No API, browse-only

**Paid tier** (Phase 2, not yet built):
- **Live** backlink verification — at request time, fetch a sample of source URLs from the CC graph and HTTP-check they still link to the candidate. Catches "CC saw 50k inbound 3 months ago but most are now dead pages". Strongest single signal for a serious drop-catcher.
- **Historical decay** — the 12-month backlink trajectory (Strategy C from the multi-release query decision above)
- **API access** — programmatic queries against today's list, historical lists, single-apex backlink lookups
- **More domains/day** — 200-500 candidates vs free's 30-50
- **Longer archive** — 60- or 90-day window vs free's 14
- **CSV / NDJSON export** — for users who want to feed our list into their own tools

### Why this shape

The free tier needs to be genuinely useful — a publication-quality list of vetted drops with enough signal that someone could act on it. The paid tier should add capabilities that **require ongoing compute** (live verification) or **require accumulated infrastructure** (historical archive) — not just unlock data the free tier hides. That asymmetry justifies the price gap without making the free tier feel crippled.

### Pricing/UX is deferred

We don't know:
- What the live-verification compute cost actually is per query
- What the conversion funnel looks like (newsletter sub → free user → paid user)
- What competitors charge (Ahrefs/Majestic price per domain query, not per-month-with-API; ExpiredDomains.net is free with ad noise)

These all become legible only after Phase 2 ships and we have weeks of free-tier traffic data. Pricing tomorrow would be guesswork.

### What we DON'T defer

The data architecture choices made now affect what's possible later. Specifically:
- **Accumulation strategy** (decided): never delete old releases — this is the *precondition* for the paid historical-decay tier
- **Schema forward-compatibility** (decided): cc_apex schema is column-additive, multi-release-joinable — keeps Strategy C viable
- **JSON contract stability** (already PLAN.md Principle 5): the public JSON shape is locked — paid-tier fields land via NEW keys, not by mutating existing ones

So we're already paying the small cost of "build for paid-tier optionality" without paying any of the cost of "actually run a paid tier." Right balance for the current phase.

### When to re-engage

Trigger 1 — Newsletter subscriber count crosses ~100. Implies non-trivial audience interest; pricing experiments become possible.

Trigger 2 — Live verification capability is built (Phase 2 milestone). Without it, the paid-tier proposition has no unique value.

Trigger 3 — Direct user request for any of the paid-tier features (we won't proactively ask). If a user emails asking "can I get this as CSV for $X/mo", that's a pricing signal worth honoring.

---

## Daily publication count cap (2026-05-13, pending)

### Current state

`config.json` has `max_candidates_for_publication: 300`. This is a CEILING applied at publication time (in `output.build_payload`), NOT a quota — `output.py` never pads up to 300, just clips down from whatever survived scoring.

**Today's run published 52 domains.** The cap is irrelevant at current quality density. Tomorrow's CC-enabled run might lift density meaningfully but won't reach 100; the cap stays inactive.

### When the cap becomes real

The cap matters once:
- CC backlinks scoring lifts the typical day's publication count past ~150
- `.com` re-enablement (planned 2026-05-17) multiplies candidate volume by ~10×

Either alone might push us past 300/day. Both together almost certainly will. At that point the current "hard cap at 300" behaviour starts dropping legitimate candidates with no graceful UX.

### Three options to consider then

**Option H — Hard cap.** Keep `max_candidates_for_publication: N` as-is, raise N to whatever feels right (500? 1000?). Simple. Frontend gets a single static list. Domains beyond rank N silently lost on a given day.

**Option F — Score-floor only.** Drop the cap entirely; publish every candidate that scores above `publish_min_score` (currently 30). List grows or shrinks naturally with quality density. Frontend needs lazy load or pagination for long lists. Domains beyond the rank that the user scrolls to: still in the JSON, just not visible without UI action.

**Option Y — Hybrid: hard floor + pagination beyond.** Surface top N (say, 100) on the homepage card; remaining survivors accessible via a "more candidates" link or paginated archive page. Best UX, most engineering. Requires frontend work + URL routing for the archive page (currently no such page exists per STATUS.md).

### Provisional lean (not decided)

Option F (score-floor only) is the cleanest "data product" stance: we publish what passes our quality bar, the UI is the UI's problem. Option Y is the better PRODUCT but requires a Phase 2 frontend change we haven't scoped. Option H is the cop-out.

### What to do before deciding

- Wait until we have at least 3 days of CC-enabled runs to see the actual publication-count distribution
- Look at the day-3 score histogram: bimodal would suggest Option Y (top tier vs long tail), unimodal would suggest Option F (no natural cut point)
- Talk to the first ~3 paid users (when Phase 2 ships) about what they want — do they want curated top-50, or do they want raw "every domain that passed filters"?

### What NOT to do

Don't pre-emptively change the cap before CC scoring is wired and observed. The current 300 ceiling is fine for the next 4-6 days; we'll have real data to decide on by then.

---

## Common Crawl refresh cadence — manual vs automated (2026-05-13, pending)

### Current state

`cc_refresh.py` is invoked manually:

```bash
python -m scripts.cc_refresh --release cc-main-2026-feb-mar-apr
```

CC publishes a new domain-webgraph release roughly monthly. Next expected: `cc-main-2026-mar-apr-may`, late May / early June 2026. After today's first successful run, manual cadence is fine for the next refresh; automation is now eligible to land but not urgent.

### Three options

**Option Manual (today).** Operator triggers each refresh by hand. After completion, operator commits a config bump:

```diff
- "latest_release": "cc-main-2026-feb-mar-apr"
+ "latest_release": "cc-main-2026-mar-apr-may"
```

Pros: zero infrastructure; impossible to silently break; the config bump is a deliberate human acknowledgment that the new data is good.

Cons: requires a human in the loop monthly; if Mario is on vacation when CC publishes, we miss a release.

**Option Cron.** A systemd timer on OVH fires `cc_refresh` on the 5th of each month (later than CC's typical publication date of "early in the month"). On success, the script optionally bumps the config and commits to main.

Pros: hands-off; we never miss a release.

Cons: more moving parts (CC's actual publish date varies; the cron has to either hard-code a release name pattern or scrape CC's index for the latest); a silent failure (cron didn't run, CC's URL pattern changed, OVH disk full) goes unnoticed unless the email reporter is wired to catch refresh failures too.

**Option Hybrid.** Cron checks if a new release is available on CC; if yes, run `cc_refresh` and send an email asking Mario to confirm + bump config. Best of both — no missed releases, but operator stays in the loop.

### Provisional lean

**Stay manual for the next 2-3 releases.** Reasons:

1. The first real run JUST completed today; we don't yet know what variance to expect in CC's publication timing, file sizes, or schema. Manual cadence gives us a chance to observe before automating.
2. Automation surface to maintain is non-trivial (release-name resolution, failure detection + alerting, config-bump-and-commit logic) and we're in a phase where every commit matters.
3. The marginal cost of "human types one command monthly" is genuinely small compared to the cost of automation-bug fire-drills.

### When to revisit

After 2-3 manual refreshes (so call it July-August 2026), revisit. By then we'll have:
- Empirical CC publication-date distribution
- Empirical size/schema stability (or surprises)
- Empirical refresh wall-clock variance
- Confidence that the script handles the year's variants

Then either: build Option Cron with retry/backoff/alerting, or build Option Hybrid (more conservative, keeps human in the loop), depending on whether the manual runs have revealed any surprises.

### Operational discipline meanwhile

- Next refresh due: ~early June 2026 (release `cc-main-2026-mar-apr-may`, expected)
- Trigger: Mario notices the new release via CC's `https://commoncrawl.org/web-graphs` index, or watches the index manually
- Action: `python -m scripts.cc_refresh --release cc-main-2026-mar-apr-may`, then a small commit bumping `config.json[cc_backlinks].latest_release`
- Failure mode if missed: pipeline keeps querying the previous month's release; signal degrades from "latest month's view" to "previous month's view" — graceful, no crash
