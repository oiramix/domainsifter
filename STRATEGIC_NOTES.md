# Strategic notes

Long-horizon decisions that don't fit neatly into PLAN.md (which scopes the current phase) or STATE.md (which logs current state and recent changes). Use this file when the decision being captured will outlast multiple phase transitions, or when the *reasoning* behind a choice matters more than its current implementation status.

Index of decisions:

- [Common Crawl integration — accumulation strategy (2026-05-13)](#common-crawl-integration--accumulation-strategy-2026-05-13)

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
