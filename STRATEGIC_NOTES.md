# Strategic notes

Long-horizon decisions that don't fit neatly into PLAN.md (which scopes the current phase) or STATE.md (which logs current state and recent changes). Use this file when the decision being captured will outlast multiple phase transitions, or when the *reasoning* behind a choice matters more than its current implementation status.

Index of decisions:

- [Common Crawl integration — accumulation strategy (2026-05-13 — raw pruning added 2026-09-20)](#common-crawl-integration--accumulation-strategy-2026-05-13)
- [Multi-release CC query strategy (2026-05-13 — Strategy A active 2026-05-14)](#multi-release-cc-query-strategy-2026-05-13-pending)
- [Free vs paid tier model (2026-05-13, pending)](#free-vs-paid-tier-model-2026-05-13-pending)
- [Daily publication count cap (2026-05-13, pending)](#daily-publication-count-cap-2026-05-13-pending)
- [Common Crawl refresh cadence — manual vs automated (2026-05-13 — RESOLVED 2026-09-20: automated, weekly)](#common-crawl-refresh-cadence--manual-vs-automated-2026-05-13-pending)
- [Common Crawl backlink history — how to actually get it (2026-09-20, planned not built)](#common-crawl-backlink-history--how-to-actually-get-it-2026-09-20-planned-not-built)

---

## Common Crawl integration — accumulation strategy (2026-05-13)

### The decision

Common Crawl publishes a new domain-webgraph release every month (rolling 3-month crawl window). Each release lands on R2 as ~21 GiB raw artifacts + ~1.5 GiB derived SQLite. **We never delete old releases.**

The cumulative R2 storage cost reaches ~$14/month at the 5-year mark (~$420 total over 5 years). Trivial for what we get in return: a multi-year, monthly-granularity backlink dataset that almost nobody else publishes freely, queryable in SQLite, ready for retrospective scoring or trend analysis whenever future product directions need it.

### Why this matters more than the immediate use case

The immediate use case is "use the latest release's `source_domain_count` as one input to the daily scoring of dropped domains." That alone is worth building — current scoring already has Wayback signal, OpenPageRank, cert history, and lexical features; backlink-graph data adds an independent quality dimension. But that's the obvious application and not the strategic part.

The strategic part is **decay velocity**. When a domain is freshly dropped on day D, its CC release on D−30 still contains the link graph as the rest of the web saw it 1–4 months ago (CC crawl window). If a domain had 800 inbound source domains in Feb 2026 and only 12 in May 2026, that's a steep decay signal — far more informative than the single point. **We can't measure that without historical retention.**

This is the kind of capability that's expensive to acquire retroactively (you can't go back in time and download the Feb 2026 release after they're stale) and cheap to acquire prospectively (just don't delete). The asymmetry decides it.

### Revision (2026-09-20): prune raw, keep derived forever

"We never delete old releases" is refined, not reversed. The distinction the 2026-05-13 note didn't draw is between the two artifact classes:

- **Derived SQLite** (~6.6 GB/release, `cc/derived/{release}.sqlite`) — `apex_domain -> source_domain_count`. **Never deleted.** This is the artifact the decay-velocity thesis above actually needs: every one of the four enabled capabilities is a query over counts across releases, and none of them reads raw. It is also the only artifact the pipeline ever touches.
- **Raw vertices + edges** (~10-18 GB/release, `cc/raw/{release}/*.txt.gz`) — **pruned to the newest 2 releases.** Raw exists for exactly one purpose: to build the derived SQLite. Once derived is built and verified, raw is a build input we already consumed. The newest 2 are kept so a discovered build bug can be re-derived without a re-download.

The asymmetry argument that decided the original policy does not apply to raw, because raw is **not** expensive to acquire retroactively: data.commoncrawl.org keeps its hyperlinkgraph releases indefinitely and serves them free. Verified 2026-09-20 — every 2026 release plus the 2025 back-catalogue still returns 200. So the "you can't go back in time" premise is true of derived (we build it, nobody else hosts it) and false of raw (CC hosts it forever).

Cost effect: raw pinned at ~21 GB IA (~$0.21/mo) instead of growing 10-18 GB every month; derived still accumulates ~$0.10/mo per release. Five-year projection drops from ~$14/mo to ~$6/mo with **zero** loss of strategic capability. That matters more than it did in May: there is still no revenue, and the EUR 120/mo Max subscription is the whole budget.

Implemented as `cc_backlinks.refresh.prune_raw_after_releases` (default 2; set 0 to disable). The pruning code refuses to delete anything under `cc/derived/` at any config value, refuses to touch the active release, and skips any release name it cannot parse — three independent guards, because deleting the live derived SQLite would silently zero out a 0.30-weight scoring input.

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

**Status update — 2026-05-14**: All four validation steps below passed. Wire-in commit `bae0bde` registered `cc_backlinks` in `ENRICHMENT_MODULES`, added the scoring weight (0.30, log-scaled /4.0), and surfaced the Backlinks column on the homepage plus the Step 5 "Check backlinks" methodology card. Tomorrow's 06:30 UTC autonomous run is the first production exercise of the wired stage. The validation gate paragraph is preserved below for historical reference.

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

### RESOLVED 2026-09-20 — Option Cron, weekly, with automated verification

The revisit date set below ("call it July-August 2026") passed unobserved, and the failure mode this note predicted is exactly what happened: **the refresh never ran again.** One manual run, 2026-05-13. Four months stale by 2026-09-20, through releases mar-apr-may, apr-may-jun, may-jun-jul and jun-jul-aug — while `cc_source_domain_count` carried scoring weight 0.30. Nobody noticed, because stale data produces plausible numbers.

That settles the argument the "provisional lean" was making. Its three reasons were all about deferring automation cost; none of them priced the cost of the manual path simply not being walked. The empirical answer to "what variance should we expect in CC's publication timing and file sizes" also arrived, and it is undramatic: monthly like clockwork, vertices stable at 0.88-0.92 GB, schema unchanged. The thing that actually varied was human attention.

**Option Hybrid is rejected**, despite being the conservative choice. Hybrid's "email Mario to confirm + bump config" step is the manual step that already failed — it moves the single point of failure from "notice the release" to "act on the email" without removing it. Automation that still needs a human to finish is the worst of the three, because it also lets everyone believe the problem is solved.

**Option Cron as built**, addressing each of its listed cons:

| 2026-05-13 objection | How it is answered |
|---|---|
| "CC's actual publish date varies" | Weekly ticks, not a day-of-month. A release is picked up within 7 days of appearing, against a 3-month-wide data window — irrelevant staleness. |
| "hard-code a release name pattern or scrape CC's index" | Neither. The release name is *computed* from the rolling 3-month window and then **HEAD-probed** on data.commoncrawl.org, walking back up to 6 windows. Authoritative (it checks the artifact, not a web page), no HTML parsing to break. |
| "a silent failure goes unnoticed unless the email reporter is wired to catch refresh failures too" | So the email reporter was wired. The daily operational email now carries the installed release and its age, and escalates the subject when it exceeds `staleness_warn_days`. This objection was correct and is the single most important part of the change — same lesson as the 2026-07-23 to 2026-09-17 LLM outage. |
| "the config bump is a deliberate human acknowledgment that the new data is good" (the Manual pro) | Replaced by a machine check that is *stricter* than the human one ever was: the derived SQLite is re-downloaded **from R2** and probed for row count and known-apex canaries before `latest_release` moves. The manual bumps never verified anything. |

Two safety properties worth stating plainly, because a half-applied refresh is the one genuinely dangerous outcome:

1. **The config swap happens only after verification passes.** A verification failure leaves `latest_release` pointing at the previous release, so the pipeline keeps scoring on known-good data. Degraded, never broken, never half-swapped mid-run.
2. **The job cannot still be running at 09:00 UTC.** Not by being scheduled early enough, but by a systemd runtime cap that has PID 1 kill it with hours of clearance. Every step is idempotent and the download resumes by HTTP Range, so a kill costs one week, not one release.

Weekly rather than monthly is mostly about that last point: a monthly timer that gets killed or fails leaves the 0.30-weight input stale for a month. Weekly gives every failure an automatic retry, and a no-op tick costs two HEAD requests.

### Provisional lean (2026-05-13, superseded — kept for the reasoning)

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
---

## Common Crawl backlink history — how to actually get it (2026-09-20, planned not built)

### The question this answers

With the refresh automated (weekly tick, installs monthly), R2 now accumulates one derived SQLite per Common Crawl release, forever. The natural expectation is that this is "growing our own database" of backlink history. **It is not, yet** — and the gap is worth writing down before someone builds the expensive version of it.

What we have is an **archive of monthly snapshots**. What a history capability needs is the ability to answer *"what did THIS domain's inbound source-domain count look like over the last N releases?"* Strategy A queries only `latest_release`, so nothing reads across snapshots today.

### Why the obvious implementation is the wrong one

Each derived SQLite holds **all ~120M apexes** (119,722,885 in `cc-main-2026-jun-jul-aug`) and weighs **6.4 GB**. Keeping them forever and querying across them means:

- **Storage**: 12 releases/year x 6.4 GB = ~77 GB/year, ~384 GB and ~$5.80/mo at the 5-year mark.
- **Query cost**: answering the decay question for one domain across 12 releases means touching 12 separate 6.4 GB files. Even as indexed point-lookups that is 77 GB of cold object storage to pull or keep resident.

And it is almost entirely waste, because **we only ever evaluate a few thousand domains a day**. The domains we care about are a vanishing fraction of 120M. Building history over the full graph to serve a few thousand lookups is the wrong shape.

### The two capabilities are different, and one of them is nearly free

Separating them is the whole insight:

**(a) Forward history — cheap, compounding, start-anytime.**
`cc_backlinks.enrich()` already fetches `cc_source_domain_count` for every candidate it evaluates, every single day. Recording `(apex_domain, release, source_domain_count, observed_date)` at that moment costs **one extra append per candidate** and no extra reads. At ~2,500 candidates/day that is a few hundred KB/day — megabytes per year, not hundreds of gigabytes.

The idiom already exists in this repo: `scripts/domain_archive.py` appends event-shaped records to private R2 at `state/domain_archive/YYYY-MM.jsonl`, append-only, monthly partitions, never aged out, chained as a non-fatal step so it cannot break a run. A backlink-history writer should be the same shape and should reuse that pattern rather than invent one.

The catch worth being honest about: forward history only accrues from the day it is switched on. It compounds, so the value of starting is monotonically decreasing in how long we wait — which is the argument for doing it sooner rather than when a product need appears.

**(b) Backward history — expensive, batch-only, genuinely deferred.**
"This domain dropped today; what did it look like a year ago" requires reading historical derived SQLites, which is the 6.4-GB-per-release cost above. But it has a property that makes it tractable: it is **batchable**. One pass over one historical SQLite can answer thousands of domains at once. So the right shape is an offline batch job over a candidate set, never a per-domain lookup on the daily hot path — and certainly never inside `enrich()`, which must stay a sub-ms point lookup.

This is what keeping the derived SQLites buys us, and it is why the 2026-09-20 retention decision keeps derived forever while pruning raw.

### Coverage we actually hold, and the overlap trap

As of 2026-09-20, after backfilling the three releases missed during the stale period, R2 holds derived SQLites for `feb-mar-apr`, `mar-apr-may`, `apr-may-jun`, `may-jun-jul` and `jun-jul-aug` 2026 — unbroken monthly coverage Feb-Aug 2026.

**But monthly releases are not independent observations.** Each is a rolling 3-month window, so consecutive releases share **two of their three months**. `feb-mar-apr` and `jun-jul-aug` share none; `feb-mar-apr` and `mar-apr-may` share two thirds. Any decay calculation must account for this or it will read autocorrelation as signal. Practically: for a decay slope, prefer samples **three or more releases apart** (non-overlapping windows); use adjacent releases only for smoothing, never as independent points.

This is also why the four-month stale gap cost less information than it appeared to — we were left with two *fully non-overlapping* snapshots, which is the useful configuration anyway.

### What it would take to score on it

Out of current phase scope, and noting the blast radius so nobody underestimates it: a decay signal is a **new scoring input**, which means `scoring_weights` changes, `score.py` changes, and the output JSON contract in PLAN.md Principle 5 changes on **both** the pipeline and site sides. It also needs a null-handling story at least as careful as the existing one (`cc_source_domain_count=null` is excluded from the average and deliberately not counted toward `publish_min_enrichment_completeness`), because "no history yet" will be the common case for a long time after forward history is switched on.

### Recommendation

1. **Do (a) when there is appetite for one small commit**: an append-only forward-history writer modelled on `domain_archive.py`, disabled-by-default config flag, non-fatal, no scoring change. It is cheap, it cannot break a run, and every day it is not on is a day of history not accumulated.
2. **Leave (b) deferred.** It is an offline batch capability whose inputs we are already preserving. Nothing is lost by waiting, because the derived SQLites are kept.
3. **Do not merge snapshots into one big table.** Keep per-release files as the durable artifact and derive whatever compact view is needed; a merged 120M-row-by-N-release table is the expensive version of a problem we do not have.
4. **If storage ever becomes the binding constraint**, the first lever is compacting *old* derived SQLites to drop the `source_domain_count = 0` rows (14.9M of 119.7M in `jun-jul-aug`, ~12%) — but note that loses the three-state "in graph with zero inbound" vs "not in graph" distinction the schema was deliberately built to preserve, so it is a real trade, not free housekeeping.
