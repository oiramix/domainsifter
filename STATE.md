# DomainSifter — Current State

Last updated: 2026-05-09 (Verisign approval landed in CZDS overnight — `.com` and `.net` now wired into the pipeline at a defensive 4.0s RDAP throttle. Same day: per-registry throttle audit completed (5 of 8 registries publish no numeric limit, only CentralNic has a documented ceiling); concurrency investigation ruled out the leak hypothesis for the 2026-05-05 identitydigital ban (volume-driven, not concurrency-driven); User-Agent fingerprint finding surfaced and fixed (named UA replaces the heavily-flagged `python-requests/2.32.3` default on all RDAP calls). GMO bumped 4.0→5.0 defensively after 8 soft warnings today. Common Crawl integration deferred one week so Verisign's earlier-than-expected approval can ship first. Today's cron itself was a Wayback-outage day (HTTP 111 throughout enrichment) — pipeline handled the external degradation correctly, only 1 fresh publish (`promo-pro.online` Caution) + 18 carryover. 336/336 tests passing.)

This document captures the current snapshot of the project. Update it whenever a meaningful milestone is reached. Read this file FIRST in any new session to understand where we are.

---

## What is built and live

### Domain and DNS

- Registered: domainsifter.com (Namecheap, 2-year registration, ~26 EUR)
- Privacy: WhoisGuard enabled
- DNS: Migrated to Cloudflare nameservers (rex.ns.cloudflare.com, tessa.ns.cloudflare.com)

### Website (live in production)

- URL: https://domainsifter.com (and https://www.domainsifter.com)
- Stack: Astro 4 + Tailwind, single-page static site
- Hosting: Cloudflare Pages, auto-deploys from main branch
- Deployment method: Wrangler CLI from local
- Lighthouse scores (production): Performance 92, Accessibility 100, Best Practices 100, SEO 100
- SEO: Title, meta description, canonical, OG tags, Twitter cards, JSON-LD (WebSite + Organization + SearchAction), robots.txt, sitemap-index.xml, 404 page, favicon (DS monogram in teal)
- Sample data: 20 hardcoded fake-but-realistic domain entries in src/data/sample-domains.json (NEVER use real registered domains here)
- Sitemap quirk: @astrojs/sitemap pinned to 3.2.1 (3.7.2+ crashes with Astro 4). Revisit ~3 months from now.

### GitHub repository

- URL: https://github.com/oiramix/domainsifter
- Visibility: Public, MIT license
- Commit author: oiramix with privacy email 99090280+oiramix@users.noreply.github.com (repo-only git config)
- Topics: expired-domains, domain-research, seo-tools, domain-discovery, drop-catching, astro, cloudflare-pages

### Email infrastructure (fully working)

- Inbound: Cloudflare Email Routing — hello@domainsifter.com and catch-all forward to oiramix3@gmail.com
- Outbound: Brevo SMTP relay (free tier, 300 emails/day) → Gmail Send-as
- Authentication: SPF, DKIM, DMARC all passing
- DNS records:
  - 3x MX → route1/2/3.mx.cloudflare.net
  - TXT (SPF): v=spf1 include:_spf.mx.cloudflare.net ~all
  - TXT (Cloudflare DKIM): cf2024-1._domainkey
  - 2x CNAME (Brevo DKIM): brevo1._domainkey → b1.domainsifter-com.dkim.brevo.com, brevo2._domainkey → b2.domainsifter-com.dkim.brevo.com
  - TXT (Brevo verification): brevo-code:bcd9e20e9d2d885b9b108b891fa9dd39
  - TXT (DMARC): _dmarc → v=DMARC1; p=none; rua=mailto:rua@dmarc.brevo.com
- Brevo SMTP credentials: server smtp-relay.brevo.com, port 587, login a95103001@smtp-brevo.com, key labeled "Gmail Send-as" (saved separately by owner)
- Tested and verified: Reply from Gmail uses hello@domainsifter.com correctly. Headers show mailed-by domainsifter.com, signed by domainsifter.com, TLS encrypted. Gmail flags as Important on first send.

### ICANN CZDS access

- Account: Mario-Martin (individual applicant, Estonia, unaffiliated with ICANN bodies)
- Contact: hello@domainsifter.com
- Submitted: April 26, 2026 — 15 TLDs requested
- Approved (11): .app, .dev, .live, .studio, .tech, .online, .site, .store, .xyz, .info, .org
- Pending (4): .com, .net, .shop, .biz
- Expiration dates: 2036-2037 (auto-renew enabled for all)
- Dashboard: https://czds.icann.org/zone-requests/all
- Purpose statement used (under 300 chars):
  "DomainSifter (https://domainsifter.com) publishes a daily-curated list of recently-dropped domains, filtered for spam, malware, and abuse signals via Wayback Machine and Safe Browsing. We do not redistribute zone files. Estonia-based independent project. Contact: hello@domainsifter.com"

---

## What is NOT yet built

### Pipeline (Phase 1 — CODE COMPLETE, awaiting first run)

- scripts/config.json: BUILT — TLDs (11 approved + 4 pending), filter thresholds, scoring weights, rejected keywords, both CZDS base URLs (auth + api), all enrichment endpoints, paths
- scripts/czds_client.py: BUILT — `authenticate`, `list_zone_links`, `download_zone` (streaming). Two exception types (`CzdsAuthError`, `CzdsApiError`). Auth/links/download all raise on failure; pipeline orchestrator decides per-zone tolerance.
- tests/test_czds_client.py: BUILT — 12 tests, all passing locally (Python 3.10.7 + pytest 8.3.3 + responses 0.25.3). Covers happy paths, 401/403/404, malformed JSON, missing token, connection errors, bearer-header propagation.
- scripts/zone_parser.py: BUILT — `iter_apex_names` (streaming, may emit dups) + `parse_zone` (dedup set). Streams gz via `gzip.open(..., "rt")`, splits on whitespace (handles spaces and tabs), lowercases, strips trailing dot. Skips blank/`;`/`$` lines.
- tests/test_zone_parser.py: BUILT — 9 tests, all passing. Covers dedup across record types, lowercase normalization, trailing-dot strip, comment/directive skipping, tab-separated records, empty zone, missing file.
- scripts/diff.py: BUILT — `load_yesterday`, `compute_drops`, `commit_today`, `diff_and_commit`. State lives in Cloudflare R2 (S3-compatible) at `s3://$R2_BUCKET_NAME/state/{tld}_yesterday.txt`, sorted, one per line. Cold start (no R2 object) returns empty drops and writes today's snapshot for tomorrow. **Migrated from on-disk state during v1 first-run** because per-TLD files exceeded GitHub's 100 MB limit (.org=228 MB, .xyz=136 MB, .info=100.88 MB). PLAN.md Principle 4 originally targeted v2; brought forward to v1.
- tests/test_diff.py: BUILT — covers cold start, warm run, sorted output, blank-line tolerance, TLD-case lowercase, non-404 ClientError propagation, raw '404' code handling, R2 endpoint construction. R2 client mocked via `unittest.mock.MagicMock` and injected through `client=` kwarg (CLAUDE.md rule #13: tests never hit live APIs).
- scripts/enrichment/__init__.py: BUILT — documents the plugin contract (`enrich(domain, config) -> dict`, never raises, empty dict on failure)
- scripts/enrichment/wayback.py: BUILT — Wayback CDX API. Returns `wayback_snapshots` (int) and `wayback_last_snapshot` ("YYYY-MM-DD" | None). Empty dict on 5xx, malformed JSON, connection errors.
- tests/enrichment/test_wayback.py: BUILT — 7 tests, all passing. Covers happy path, no snapshots, header-only response, 5xx, invalid JSON, connection error, missing-config defaults.
- scripts/enrichment/open_page_rank.py: BUILT — Reads `OPENPAGERANK_KEY` from env. Returns `{"open_page_rank": float}` or empty dict. Skips silently when key unset.
- tests/enrichment/test_open_page_rank.py: BUILT — 8 tests, all passing. Covers happy path, missing key, header propagation, zero rank, 5xx, malformed JSON, connection error, non-numeric decimal.
- scripts/enrichment/spam_check.py: BUILT — Generic name (CLAUDE.md rule #12). v1 calls Google Safe Browsing v4. Reads `SAFE_BROWSING_KEY`. Returns `spam_flagged` + `spam_threat_types`. Empty dict when key unset, on 5xx, malformed JSON, or connection error.
- tests/enrichment/test_spam_check.py: BUILT — 7 tests, all passing. Covers no-match, multi-match dedupe, missing key, key-in-querystring + URL-in-body, 5xx, invalid JSON, connection error.
- scripts/enrichment/_dnsbl.py: BUILT — Shared DNSBL helper using `socket.gethostbyname_ex` (no dnspython dep, per CLAUDE.md rule #7). Returns True/False/None.
- scripts/enrichment/surbl.py: BUILT — Queries `multi.surbl.org`. Returns `{"surbl_listed": bool}` or empty.
- scripts/enrichment/spamhaus.py: BUILT — Queries `dbl.spamhaus.org`. Returns `{"spamhaus_listed": bool}` or empty.
- tests/enrichment/test_surbl.py + test_spamhaus.py: BUILT — 9 tests total, all passing. Cover 127.x → listed, NXDOMAIN → not listed, transient failure → empty, OS error → empty, configurable zone.
- scripts/enrichment/crtsh.py: BUILT — Queries `crt.sh/?q=%.{domain}&output=json`. Returns `cert_history` (bool) + `cert_count` (int, deduped by cert id). Empty dict on transport failure or HTML/non-list response.
- tests/enrichment/test_crtsh.py: BUILT — 7 tests, all passing. Covers happy path with dedup, empty list, wildcard query encoding, 5xx, HTML response, non-list payload, connection error.
- scripts/enrichment/rdap.py: BUILT — Loads IANA bootstrap once via module-level `@lru_cache(maxsize=8)` on `_fetch_bootstrap(url, timeout)`; cache cleared on failure so retries can succeed. Looks up TLD's RDAP server, fetches `/domain/{name}`, extracts registrar from `entities[].vcardArray.fn`. Returns `previous_registrar` + `rdap_status`. Empty dict on bootstrap fail or unknown TLD; null fields on 404.
- tests/enrichment/test_rdap.py: BUILT — 9 tests, all passing. Autouse fixture clears the lru_cache between tests. Covers happy path, lru_cache reuse across calls, bootstrap 503, unknown TLD, 404 → null fields, 5xx → empty, no registrar entity, missing status field, connection error.
- scripts/env_check.py: BUILT — `validate_env()` raises `MissingEnvVarsError` listing every missing required var (CZDS_USERNAME, CZDS_PASSWORD, SAFE_BROWSING_KEY, R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME). OPENPAGERANK_KEY is optional → warning. Empty strings count as missing. Pipeline.py calls this first thing.
- tests/test_env_check.py: BUILT — covers all-required, partial-missing (multi), empty-string-as-missing, optional warning, fallback to os.environ.
- scripts/enrichment/spam_check.py (UPDATED): missing `SAFE_BROWSING_KEY` now raises `SpamCheckConfigError` (was: silent empty dict). Per-domain network/5xx still returns empty dict — those are transient. Reasoning: spam_check is a CORE filter rule; degraded malware filtering is worse than no daily run. env_check is the first line of defence; the raise here is defence-in-depth.
- scripts/filter.py: BUILT — `keep(candidate, config, *, strict_spam_check=True)` returns `(bool, reason|None)`. Rules: punycode, length, all-numeric, keyword, spam_flagged, surbl_listed, spamhaus_listed, min wayback (only enforced when field present). `strict_spam_check=True` rejects when spam_flagged field is missing. `filter_candidates(...)` logs per-reason rejection counts.
- tests/test_filter.py: BUILT — 16 tests, all passing.
- scripts/score.py: BUILT — Composite weighted score in [0, 100]. wayback log-scaled, OPR linear /10, cert boolean, length inverted (shorter = better). Missing fields = 0 contribution (not a penalty). `score_candidates(...)` mutates in-place adds `score` field, sorts desc by score then asc by name for determinism.
- tests/test_score.py: BUILT — 11 tests, all passing.
- scripts/output.py: BUILT — `build_payload(...)` projects to the locked PLAN.md Principle 5 contract (no internal fields leak), caps at `max_candidates_per_day`, applies `affiliate_link_template`. `write_output(...)` writes atomically via tempfile + os.replace so Cloudflare Pages never serves a partial file. Cleans up temp on error.
- tests/test_output.py: BUILT — 12 tests, all passing.
- scripts/pipeline.py: BUILT — Orchestrator. Order: `env_check.validate_env()` → CZDS auth → list links → per-TLD download/parse/diff/commit (tempdir + per-zone failure tolerance) → `ThreadPoolExecutor` enrichment with `max_workers = config.max_concurrent_enrichments` (each candidate runs all 7 sources sequentially within one worker; candidates run in parallel across the pool) → filter (strict_spam_check=True) → score → atomic write. Per-enricher exceptions logged & continued; `SpamCheckConfigError` re-raised to abort run. CLI: `python scripts/pipeline.py [--config path] [--output path]`.
- tests/test_pipeline.py: BUILT — 9 tests, all passing.
- .github/workflows/daily-diff.yml: BUILT — Triggers `cron: "0 6 * * *"` + `workflow_dispatch`. `permissions: contents: write`. Concurrency group prevents overlap. Steps: checkout → setup-python 3.11 (pip cache) → install requirements.txt → run pipeline (CZDS + Safe Browsing + OpenPageRank + R2 secrets passed as env) → commit ONLY `src/data/daily-domains.json` as `github-actions[bot]` with message `data: daily refresh YYYY-MM-DD`. State files (yesterday snapshots) are written to Cloudflare R2 by the pipeline itself, not committed. Skips commit when nothing changed.
- scripts/state/.gitkeep: BUILT (directory now ignored except for .gitkeep — state lives in R2).
- Full suite: passing locally (test counts updated for new R2 tests).
- requirements.txt + requirements-dev.txt: BUILT — `requests==2.32.3`, `boto3==1.35.71` (R2 client). Dev: `pytest==8.3.3`, `responses==0.25.3`. R2 mocked via `unittest.mock` (no `moto` dep needed).
- .gitignore: BUILT — blocks *.zone, *.zone.gz, .env, __pycache__, .venv, AND `scripts/state/*` (except `.gitkeep`).

### External API keys needed

- OpenPageRank API key (free, 10k/day): NOT YET SIGNED UP — sign up at https://www.domcop.com/openpagerank/
- Google Safe Browsing API key (free, 10k/day): NOT YET SIGNED UP — Google Cloud Console → enable Safe Browsing API
- Both keys must be added to GitHub Secrets as OPENPAGERANK_KEY and SAFE_BROWSING_KEY
- CZDS credentials must be added to GitHub Secrets as CZDS_USERNAME and CZDS_PASSWORD
- Cloudflare R2 (free tier, 10 GB storage + 1M Class A ops/month): bucket `domainsifter-state`, API token with R2 read/write scope. Four secrets: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME.

### Affiliate programs

- Porkbun direct (5 min signup): NOT DONE
- Dynadot direct (5 min signup): NOT DONE
- Namesilo direct (5 min signup): NOT DONE
- Sav direct (5 min signup): NOT DONE
- Skipped: Namecheap via impact.com (heavy tax/VAT forms — defer)

### Newsletter capture

- Buttondown account: NOT CREATED
- Wire existing email signup form to Buttondown API endpoint https://api.buttondown.email/v1/subscribers: NOT DONE

### Social presence

- Claim @domainsifter on X/Twitter: NOT DONE
- Claim u/domainsifter on Reddit: NOT DONE

---

## Active accounts and where credentials live

| Service | Account | Where credentials are stored |
|---|---|---|
| Namecheap | mario-martin | Owner password manager |
| Cloudflare | oiramix3@gmail.com | Owner password manager |
| GitHub | oiramix | Owner password manager |
| Brevo | (account owner email) | Owner password manager + saved SMTP key |
| ICANN/CZDS | (Mario-Martin individual) | Owner password manager |
| Gmail (forwarding target) | oiramix3@gmail.com | Owner password manager |

GitHub Secrets to be added (currently empty):

- CZDS_USERNAME
- CZDS_PASSWORD
- OPENPAGERANK_KEY
- SAFE_BROWSING_KEY
- R2_ACCOUNT_ID         (Cloudflare account ID, visible in any R2 bucket page sidebar)
- R2_ACCESS_KEY_ID      (from "Manage R2 API Tokens" → create token with read+write on the bucket)
- R2_SECRET_ACCESS_KEY  (shown once at token creation — save it before closing the dialog)
- R2_BUCKET_NAME        (set to `domainsifter-state`)

---

## V1 PIPELINE COMPLETE — operational runbook for first manual run

All v1 code is in place and 135/135 tests pass locally. The remaining steps
are operational. Future Claude sessions: read this section first if the
owner asks "where are we?".

### ⚠️ READ THIS BEFORE THE FIRST RUN

**Day 1 produces an empty domain list. THIS IS NOT A BUG.**

The pipeline computes drops as `yesterday - today`. On the first run there
is no `scripts/state/{tld}_yesterday.txt` for any TLD, so `compute_drops`
returns an empty set for every TLD. The pipeline still:

- Downloads all 11 approved zones
- Parses them
- Writes `scripts/state/{tld}_yesterday.txt` for each TLD (this seeds the
  baseline for tomorrow's diff)
- Writes `src/data/daily-domains.json` with `domain_count: 0` and an empty
  `domains` array

**Day 2 (cron at 06:00 UTC the next morning) produces the first real list.**

If the day-1 bot commit shows 11 new state files plus a daily-domains.json
with `"domain_count": 0`, the run was successful. Do not "fix" the empty
output.

### Step 1 — Sign up for the two free API keys

| Service | Where | Free quota | Env var |
|---|---|---|---|
| Google Safe Browsing v4 | console.cloud.google.com → create project → APIs & Services → enable "Safe Browsing API" → Credentials → Create API key | 10k/day | `SAFE_BROWSING_KEY` |
| OpenPageRank | https://www.domcop.com/openpagerank/ → sign up → API key shown in dashboard | 1k/day per key | `OPENPAGERANK_KEY` |

CZDS credentials already exist (Mario-Martin individual account).

### Step 1b — Set up Cloudflare R2 for state storage

Yesterday's per-TLD zone snapshots live in Cloudflare R2 (S3-compatible
object storage), not in the repo. They were originally planned for the
repo (PLAN.md Principle 4) but the .org snapshot alone is 228 MB and
GitHub rejects files over 100 MB.

1. Cloudflare dashboard → **R2 Object Storage** → "Create bucket"
   - Name: **`domainsifter-state`**
   - Location hint: leave "Automatic" (free tier, no charge)
   - Click Create.
2. Stay on the R2 landing page → click **"Manage R2 API Tokens"** (top right) → **Create API Token**.
   - Token name: `domainsifter-pipeline`
   - Permissions: **Object Read & Write**
   - Specify buckets: select **only** `domainsifter-state`
   - TTL: leave "Forever" (or set a reminder; rotate annually)
   - Click Create API Token. Copy:
     - **Access Key ID** → goes into `R2_ACCESS_KEY_ID`
     - **Secret Access Key** → goes into `R2_SECRET_ACCESS_KEY` (shown ONCE — copy now)
3. R2_ACCOUNT_ID is visible in any R2 bucket detail page sidebar
   ("Account ID"). Copy that into `R2_ACCOUNT_ID`.
4. `R2_BUCKET_NAME` is the literal string `domainsifter-state`.

Free tier limits (well under our usage):
- Storage: 10 GB/month — we'll use ~500 MB total across 11 TLDs
- Class A ops (writes): 1M/month — we do 11/day = 330/month
- Class B ops (reads): 10M/month — same scale

### Step 2 — Add eight GitHub Actions secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add these eight (names are case-sensitive):

- `CZDS_USERNAME`
- `CZDS_PASSWORD`
- `SAFE_BROWSING_KEY`
- `OPENPAGERANK_KEY`
- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`

### Step 3 — Commit and push the pipeline code

From the local working directory (`c:/Users/Y/Documents/Github/domainsifter pipeline`):

```bash
git add scripts/ tests/ .github/ requirements.txt requirements-dev.txt .gitignore PLAN.md STATE.md CLAUDE.md
git status                                  # verify nothing under scripts/state/ except .gitkeep, no .zone files
git commit -m "feat: v1 pipeline (CZDS → enrich → filter → score → JSON)"
git push origin main
```

If the local clone doesn't already have a remote, set it first:

```bash
git remote add origin https://github.com/oiramix/domainsifter.git
git push -u origin main
```

If working on a branch instead of main, push the branch then open a PR and
merge in the GitHub UI.

### Step 4 — Trigger the workflow manually

1. Open https://github.com/oiramix/domainsifter
2. Click the **Actions** tab (top nav, between "Pull requests" and "Projects")
3. In the left sidebar workflow list, click **Daily Domain Diff**
4. On the right, click the **Run workflow** dropdown button
5. Leave branch as `main`, click the green **Run workflow** button
6. Refresh the page — a new run appears at the top with a yellow spinning icon
7. Click into the run, then click the `run-pipeline` job to stream logs live

### Step 5 — Success markers to watch for in the run log

The log lines below come from the modules' `logger.info(...)` calls. Look
for these in order — if any are missing, the corresponding step failed.

```
INFO scripts.env_check All required environment variables are present.
INFO scripts.czds_client CZDS authentication succeeded
INFO scripts.czds_client CZDS returned NN zone links
INFO scripts.pipeline CZDS approved NN zones; 11 match our TLD list
INFO scripts.czds_client Downloaded NNNNN bytes from .../app.zone to ...   (×11)
INFO scripts.zone_parser Parsed NNNNNN unique apex names from ...           (×11)
INFO scripts.diff No prior R2 snapshot for .app — cold start                (×11 on day 1; absent on day 2+)
INFO scripts.diff Wrote NNNNNN names to r2://domainsifter-state/state/app_yesterday.txt (×11)
INFO scripts.pipeline .app: NNNNNN in zone today, 0 dropped since yesterday (×11 — "0 dropped" is correct on day 1)
INFO scripts.pipeline Collected 0 total drops across 11 TLDs                (day 1 only — day 2+ will be hundreds to thousands)
INFO scripts.pipeline Enriching 0 candidates with 10 concurrent workers     (day 1 only)
INFO scripts.filter Filter kept 0 / 0 candidates                            (day 1 only)
INFO scripts.output Wrote 0 domains to src/data/daily-domains.json (generated_at=2026-MM-DDTHH:MM:SSZ)
INFO scripts.pipeline Pipeline complete: 0 domains in output                (day 1 only)
```

Final workflow steps:

```
Commit refreshed daily output → "data: daily refresh YYYY-MM-DD"
[main XXXXXXX] data: daily refresh 2026-MM-DD
 1 file changed, ...
```

The bot commit now touches a single file: `src/data/daily-domains.json`.
State files no longer hit the repo (they go to R2). If you see "No
changes to commit" on day 1, that's expected when the file content
hasn't actually changed (e.g., the previous run already wrote a
0-domain payload with the same `generated_at` timestamp). Verify R2
contents instead — see "Step 6 alt".

### Step 6 — Verify the bot commit landed

Refresh the repo's main page on github.com. You should see:

- Latest commit: `data: daily refresh YYYY-MM-DD` by `github-actions[bot]`
- `src/data/daily-domains.json` exists (0 domains on day 1, hundreds on day 2+)
- `scripts/state/` is still empty except for `.gitkeep` (state lives in R2 now)

### Step 6 alt — Verify R2 received the snapshots

In the Cloudflare dashboard → R2 → `domainsifter-state` you should see
a `state/` prefix containing 11 objects on day 1 (one per approved TLD):

```
state/app_yesterday.txt
state/dev_yesterday.txt
state/info_yesterday.txt
state/live_yesterday.txt
state/online_yesterday.txt
state/org_yesterday.txt
state/site_yesterday.txt
state/store_yesterday.txt
state/studio_yesterday.txt
state/tech_yesterday.txt
state/xyz_yesterday.txt
```

Click any one — file size should match the TLD's apex count
(.org ≈ 228 MB, .xyz ≈ 136 MB, etc.).

### Step 7 — Verify Cloudflare Pages auto-rebuilds

The bot commit on main should trigger Cloudflare Pages. Within ~1–2 min:

1. Cloudflare dashboard → Pages → domainsifter → Deployments shows a new build
2. https://domainsifter.com loads with the fresh JSON (0 domains shown on day 1)

### Step 8 — Wait for the cron

Cron is already set to `0 6 * * *` (06:00 UTC daily) in
`.github/workflows/daily-diff.yml`. No further action needed. The next
06:00 UTC tick produces the first real list using day 1's seeded snapshots
as the "yesterday" baseline.

### Fixes applied after first manual run

- **Import path (commit applied):** the workflow originally invoked the pipeline as `python scripts/pipeline.py`, which sets `sys.path[0]` to `scripts/` and breaks the absolute `from scripts import …` imports inside `pipeline.py` (line 41). Changed `.github/workflows/daily-diff.yml` to run `python -m scripts.pipeline --config scripts/config.json`. Verified locally: imports succeed, run gets as far as `env_check.validate_env()` and exits cleanly with `MissingEnvVarsError` for the unset CZDS/Safe Browsing secrets — exactly as expected outside CI. `scripts/__init__.py` and `scripts/enrichment/__init__.py` already exist (both empty/docstring-only) and were already tracked, so no new files needed.
- **State storage migrated to Cloudflare R2 (commit applied):** the second manual run completed the pipeline successfully (38.9M domain entries processed across 11 TLDs) but the commit step was rejected by GitHub:

      remote: error: File scripts/state/info_yesterday.txt is 100.88 MB
      remote: error: File scripts/state/org_yesterday.txt is 228.30 MB
      remote: error: File scripts/state/xyz_yesterday.txt is 136.08 MB
      remote: error: GH001: Large files detected.

  GitHub's hard 100 MB per-file limit broke the repo-storage plan
  immediately. R2 was already planned for v2 (PLAN.md Phase 2,
  scripts/historical.py) — pulled forward to v1. Changes:
  - `scripts/diff.py` now reads/writes via `boto3` against R2 endpoint `https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com`. Cold-start = `NoSuchKey` ClientError → empty set, then writes today's snapshot for tomorrow. Same set-difference semantics as before.
  - `scripts/env_check.py` validates four new R2 vars (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME).
  - `scripts/pipeline.py.collect_drops` constructs the R2 client once and reuses it across TLDs (one auth per run, not 11).
  - `.github/workflows/daily-diff.yml` no longer commits `scripts/state/` — only `src/data/daily-domains.json`. R2 secrets are passed to the pipeline run step.
  - `.gitignore` now excludes `scripts/state/*` (except `.gitkeep`) so a local manual run can't accidentally commit state files.
  - `requirements.txt` adds `boto3==1.35.71`.
  - Tests for `diff.py` and `pipeline.py.collect_drops` use `unittest.mock.MagicMock` for the S3 client; `ClientError` with `Code: "NoSuchKey"` simulates cold start.

### Common failure modes

| Symptom in log | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'scripts'` | Pipeline launched as a script (`python scripts/pipeline.py`) instead of a module | Run as a module: `python -m scripts.pipeline --config scripts/config.json`. Already fixed in `daily-diff.yml`; only resurfaces if someone reverts that line. |
| `MissingEnvVarsError: ...` | A repo secret is missing or empty | Re-check Settings → Secrets, names are case-sensitive |
| `CzdsAuthError: HTTP 401` | Wrong CZDS username/password | Verify in https://czds.icann.org login, update secret |
| `CzdsAuthError: HTTP 403 ... must accept terms` | One or more zones have ToS pending | Log into czds.icann.org, accept any pending T&Cs |
| `SpamCheckConfigError` mid-run | `SAFE_BROWSING_KEY` set but invalid | Re-generate key in Google Cloud Console; whitelist the API |
| Workflow fails at "Commit refreshed daily output" with `Permission denied` | `permissions: contents: write` not honored | Repo Settings → Actions → General → Workflow permissions: select "Read and write permissions" |
| `remote: error: File scripts/state/*.txt is NNN MB ... GH001: Large files detected` | RESOLVED — repo-storage plan replaced by Cloudflare R2 in this commit. State files no longer hit the repo. If this resurfaces, the workflow is staging state files again — re-check `git add` line in `daily-diff.yml`. |
| `botocore.exceptions.EndpointConnectionError` or `SignatureDoesNotMatch` from R2 | Wrong account ID or token, or token lacks the bucket | Verify R2_ACCOUNT_ID matches the dashboard "Account ID" exactly. Re-issue the R2 API token with **Object Read & Write** scoped to `domainsifter-state`. |
| `botocore.exceptions.ClientError ... NoSuchBucket` | Bucket doesn't exist or `R2_BUCKET_NAME` typo | Create the `domainsifter-state` bucket in R2 dashboard; verify the secret literal matches. |

---

## Total cost to date

26 EUR (domain registration only). Everything else: free.

---

## Day-2 ops check (2026-04-27)

Run #4 — scheduled cron — **cancelled (timeout)**.

- Triggered: 2026-04-27 08:28 UTC (cron is `0 6 * * *`, queued ~2.5h late by GitHub free-tier scheduler)
- Duration: 45m 19s — hit the workflow's `timeout-minutes: 45` cap
- Cancellation reason: "The job has exceeded the maximum execution time of 45m0s"
- Run URL: https://github.com/oiramix/domainsifter/actions/runs/24984601314
- No `data: daily refresh 2026-04-27` commit landed on `main` (origin still tipped at `e91becb`, the front-end wiring fix from 2026-04-26).
- `src/data/daily-domains.json` still contains the day-1 cold-start payload: `generated_at=2026-04-26T18:00:04Z`, `domain_count=0`.
- No score-distribution data to report (no domains).
- Other warning observed on the run page: "Node.js 20 actions are deprecated" — `actions/checkout@v4`, `actions/setup-python@v5` need updating before 2026-06-02. Not the cause of the timeout.

Cause not yet diagnosed from this check alone. Next step is reading the run logs to find which phase consumed the 45 minutes (zone download? enrichment? R2 writes?) — but not doing that here per instructions.

---

## Day 2 Incident and Architectural Response (2026-04-27)

### Incident

Cron run #4 (https://github.com/oiramix/domainsifter/actions/runs/24984601314) was cancelled at the 45-minute timeout. From log review after the fact:

- Pipeline collected **53,125 drops** across the 11 approved TLDs — that's the correct order of magnitude for a single day's churn, not a runaway count.
- Enrichment submitted all 53,125 candidates to a `ThreadPoolExecutor(max_workers=10)` and started hitting all 7 sources sequentially per candidate.
- **crt.sh** and **Wayback** rate-limited within ~14 seconds of the run start. Every subsequent request from those sources hung for the full 10s timeout.
- With 53,125 × 7 sources × 10s timeout / 10 workers ≈ 10.3 hours of degraded work scheduled, the 45m budget shredded.
- No partial output — the workflow's commit step never ran, so the site stayed on day-1 cold-start.

Root cause: the v1 architecture had no admission control. Every drop was treated as worth enriching, every API call was retried at full timeout, and there was no clock cap on enrichment. Once one source went bad, the budget was already lost.

### Architectural response (this commit)

Five interlocked changes — all required, all shipped together:

1. **Lexical pre-enrichment filter** (`scripts/lexical_filter.py`): two passes between structural reject and enrichment. Garbage detection (digit/vowel ratios, Shannon entropy, repeat runs, consonant runs) and pronounceability (overlapping trigrams matched against a ~700-entry English trigram set, derived at module load from ~200 seed words). Defaults are deliberately permissive — better to enrich a borderline real domain than reject a real one. Tunable in `config.json` under `lexical_thresholds`.

2. **Per-source circuit breaker** (`scripts/enrichment/_circuit_breaker.py`): each enrichment source instantiates one `CircuitBreaker` at module level. After 5 consecutive failures, the circuit opens for 15 minutes — `enrich()` returns `{}` immediately without making the network call. Half-open behavior on timeout: one trial allowed; failure re-opens. Thread-safe (pipeline runs candidates concurrently). 429 responses get exponential backoff (1s, 2s, then count as failure). Wired into all 7 sources without changing the public `enrich(domain, config) -> dict` contract.

3. **Wall-clock budget for enrichment** (`pipeline.enrich_all`): config gets `enrichment_time_budget_seconds: 2100` (35 min). The submission loop checks elapsed time before submitting each candidate. When budget exhausts, no new submissions; in-flight workers get up to 60s grace. Whatever finished gets filtered+scored+published. **Partial output is the design**, not a failure mode — 200 enriched survivors > 0 enriched survivors because of timeout.

4. **Two-stage caps**: `max_candidates_per_day: 500` is gone. Replaced by `max_candidates_for_enrichment: 1000` (safety net after lexical filter — sort by length asc and trim if exceeded; logs a warning) and `max_candidates_for_publication: 300` (CEILING after scoring; if fewer survive, publish all of them; **never pad**).

5. **Methodology copy on the site** (`src/components/Methodology.astro`): the inflated "12+ spam signals, 95% rejected" claim is replaced with the literal description of what the pipeline actually does. No invented statistics (CLAUDE.md rule #2).

### Filter ordering in the new pipeline

```
collect_drops              (~53,000 expected on a normal day)
  ↓
filter_candidates_structural   (R1-R5: punycode/length/numeric/keyword)
  ↓
lexical_filter.filter_candidates   (Pass 2A garbage + Pass 2B pronounceability)
  ↓
trim_for_enrichment    (cap to max_candidates_for_enrichment by length asc)
  ↓
enrich_all   (ThreadPoolExecutor; wall-clock budget; per-source breakers)
  ↓
filter_candidates_post_enrichment   (R6-R10: blocklists, wayback floor, spam_check_missing)
  ↓
score_candidates
  ↓
write_output   (cap to max_candidates_for_publication; cap is a CEILING)
```

### Decisions on ambiguous points (surfaced for review)

- **`max_consonant_run` set to 6, not the spec's 5.** "quartzbloom" — one of our sample-data brand names — has 5 consecutive consonants ("rtzbl"). Spec literal `5+` would reject it; spec narrative says "lean PERMISSIVE — better to let borderline cases through than reject real domains like 'lumenpath'". Resolved in favor of the narrative. Easily tightened in Wave 1 via `config.lexical_thresholds.max_consonant_run`.
- **Trigram seed list is ~200 words rather than a hand-curated trigram table.** Generation gives ~700 unique trigrams covering common English syllable structure. Tradeoff: less precise tuning, but easier to extend (add a word, get its trigrams for free). All 19 sample-domain roots pass; "78win012", "kvk434k1ha62", and unpronounceable noise reject.
- **Circuit breaker is module-level singleton per source, lifetime = process lifetime (one pipeline run).** Considered passing breakers via `config` but it needlessly complicated the plugin contract. Trade-off accepted; tests reset breakers via an autouse fixture in `tests/enrichment/conftest.py`.
- **bootstrap fetch in `rdap.py` is NOT routed through the 429 helper.** It's `lru_cached` and fetched once per process, so 429 retry/backoff has marginal value there. Per-domain RDAP queries DO use the helper.

### Day 3 expectation

- Cron triggers at 06:00 UTC, runs as a module (`python -m scripts.pipeline ...`).
- Structural filter takes ~53,000 → ~50,000 (rejects punycode, all-numeric, short, banned keywords).
- Lexical filter takes ~50,000 → ~3,000-15,000 (rejects digit-heavy, low-vowel, high-entropy, unpronounceable). This is a guess — actual ratio is what Wave 1 measures.
- If still over the 1,000 enrichment cap, sort-by-length-asc trim down. Logged as a warning.
- Enrichment with breakers + 35-minute budget should comfortably finish ≤30 min. Worst case (every source rate-limits) the breakers open within 5 × 10s = 50 seconds and the rest of the run skips them.
- Post-enrichment + score + cap to 300 → publish.
- Total runtime expectation: 5-15 minutes, well under the 45-min Actions cap.

### Failure modes added to the matrix

- Enrichment time budget exhausted → log warns, partial output published (NOT a failure mode any more).
- Source circuit opens → that source's fields are absent from candidates; `filter_candidates_post_enrichment` tolerates absent fields (R9 wayback floor only fires when the field is present; R10 spam_check still rejects under strict mode if `spam_flagged` is missing).

---

## Multi-registrar architecture and website honesty pass (2026-04-27 evening)

### JSON output schema migration

Per-domain shape changed in this commit. The single `affiliate_link` string is gone; replaced by an ordered `registrars[]` array:

```jsonc
// before
{ "name": "...", "...": "...", "affiliate_link": "https://..." }

// after
{
  "name": "...",
  "...": "...",
  "registrars": [
    { "name": "Namecheap", "url": "https://namecheap.pxf.io/..." },
    { "name": "NameSilo",  "url": "https://www.namesilo.com/..." }
  ]
}
```

Schema is still locked (PLAN.md Principle 5) — this is a coordinated migration touching pipeline + site + sample data in one commit.

### Reasoning

- Gives users registrar choice. Clicking "Register →" now opens a small popover; users see both Namecheap and NameSilo and pick whichever has the best price or whichever they already have an account with.
- Adding a new affiliate is a config-only change: append a new entry to `config.registrars` and the JSON / popover update on the next pipeline run. No code changes, no site redeploy beyond the daily refresh.
- Order in config = order in the popover. Highest-converting affiliate first.

### Active and pending affiliate programs

- **Active in this commit:** Namecheap (via impact.com network, link includes `WO655J` partner ID), NameSilo (direct, auto-approved on signup, `rid=36a0644du`).
- **Pending:** Dynadot Ambassador application is in review. When it lands, append `{"name":"Dynadot","link_template":"..."}` to `config.registrars` — no other code changes.
- **Removed from copy:** Porkbun (program closed) and the specific "Namecheap/Dynadot/Porkbun" list in About were replaced with generic "affiliate registrars" phrasing so the marketing copy doesn't drift every time the affiliate roster changes.

### Files changed

- `scripts/config.json` — `affiliate_link_template` removed; `registrars[]` added with the two live affiliate templates. The `{name}` placeholder is plain string substitution (NOT `str.format`) because the URL contains literal `%3D` etc.
- `scripts/output.py` — `_build_registrars()` substitutes `{name}` per entry, preserves config order, silently skips malformed entries; `CONTRACT_FIELDS` swapped `affiliate_link` for `registrars`.
- `tests/test_output.py` — schema updated; new tests cover order preservation, two-registrar substitution for both templates, malformed-entry skip, empty-config graceful default, no URL-encoding of dots.
- `tests/test_pipeline.py` — happy-path now asserts the registrars array shape, not a single string.
- `src/data/sample-domains.json` — fully regenerated. 20 invented domains across all 11 approved TLDs, distribution: org×3, xyz×3, info×3, online×2, store×2, site×2, app×1, dev×1, tech×1, live×1, studio×1. Each entry has both registrars pre-populated using the same templates the pipeline will use.
- `src/components/DomainTable.astro` — `Register →` link replaced with a click-triggered popover (vanilla JS, no framework). Behavior: tap-to-open, tap-outside-to-close, Escape-to-close, only one open at a time across the whole page. Anchored `right-4 top-full` on desktop, `left-1/2 -translate-x-1/2 top-full` on mobile cards. Each link uses `target="_blank" rel="noopener sponsored"` (sponsored per Google's affiliate-link guidance).
- `src/components/Hero.astro` — "thousands of fresh domain drops every day" → "every fresh domain drop across 11 TLDs". The first wording was a present-tense overclaim; we have one successful run, not a track record.
- `src/components/About.astro` — "independent operators" → "an independent operator" (solo founder); specific affiliate list ("Namecheap, Dynadot, Porkbun") → "affiliate registrars".
- `src/components/Footer.astro` — "Built in Estonia 🇪🇪" line removed entirely. Footer now: copyright, nav links, ICANN disclaimer.

### Frontend popover decisions

- **Width 44 (w-44 = 11rem):** narrow enough to feel like a contextual menu, wide enough for "Namecheap" + "NameSilo" labels with comfortable click targets.
- **Mobile centers via `left-1/2 -translate-x-1/2`** rather than right-anchored, because the mobile Register button spans full card width.
- **`hidden` attribute, not `class="hidden"`:** the Tailwind `hidden` class would mean the inline `class={...}` toggling, which is messier. The HTML `hidden` attribute is read by the JS as `el.hidden = true/false` and respects `display: none` natively.
- **No `nofollow` on registrar links:** Google's recent guidance says `rel="sponsored"` is the correct signal for paid/affiliate links; `nofollow` is the older fallback. We use `sponsored` alone.
- **State lives in the DOM, not in JS variables:** the `aria-expanded` attribute on each trigger is the source of truth. Cheap, accessible, and survives event-handler re-attachment.

### Sample data domain-name plausibility

The 20 invented names are creative compounds (color/material + nature noun) chosen to be plausibly unregistered. Examples: amberkite, frostledge, paperhalo, brassflint, silverbrook, jadeloom, lichenpath, thistlecove, opalstride, vellumstone, duskforge. CLAUDE.md rule #1 was the constraint: never use real registered domains in sample/test data. Quick spot-checks during selection — these aren't household names and don't show up in obvious commercial contexts.

---

## Critical bug fix: zone-diff "drops" weren't actually available (2026-04-29)

### Bug

The pipeline computed candidates as `zone(yesterday) − zone(today)` and treated absence-from-zone as proof of availability. That assumption is wrong. RDAP audit of 21 currently-published candidates (10 top-scored, 11 random) found the real distribution:

| Bucket | Count | Pct |
|---|---|---|
| Truly available (HTTP 404) | 1 | **5%** |
| Redemption period (lapsed but recoverable) | 16 | 76% |
| Owned with future expiration | 4 | 19% |

`gimid.dev` was on the published list with future expiration `2027-04-25`, recently auto-renewed `2026-04-29` (one day before the audit). It was never available. So were 19 of the other 20 sampled.

Domains drop out of zone files for many reasons that don't make them registerable: clientHold/serverHold, redemption period (the most common — old owner has 30 days to recover at premium), DNSSEC re-signing, registrar transfer churn. None of these mean "this domain is now free to register."

### Fix

Authoritative RDAP availability check added as the FINAL pipeline stage. Only HTTP 404 from the registry counts as "available" — everything else (HTTP 200 with any status, transport failures, breaker-open) is rejected.

**Pipeline order (new):**
```
zone diff → structural → lexical → cap → enrich → post-filter → score → VALIDATE_AVAILABILITY → write
```

**Files changed:**
- `scripts/enrichment/rdap.py` — added `check_availability(domain, config) -> dict` returning `{is_available: True|False|None, rdap_status, rdap_expiration, previous_registrar, rdap_http}`. Reuses the existing IANA bootstrap cache, per-host throttle, and circuit breaker. HTTP 404 → True; HTTP 200 → False; anything else → None.
- `scripts/pipeline.py` — removed `rdap` from `ENRICHMENT_MODULES` (it's now run as a dedicated post-score stage instead). Added `validate_availability(candidates, config)` that walks scored candidates in order, calls `check_availability`, and keeps only `is_available=True`. Honors `availability_budget_seconds` (default 600s) — untouched candidates default to None=REJECT, fail-closed.
- `scripts/output.py` — added `rdap_status`, `rdap_expiration`, `availability_verified_at` to `CONTRACT_FIELDS` and `_project()`. These are optional for the frontend (will render as null/empty when absent).
- `scripts/config.json` — added `availability_budget_seconds: 600`.
- `tests/enrichment/test_rdap.py` — 10 new tests for `check_availability` covering the full HTTP code matrix (404/200-owned/200-redemption/5xx/429-persistent/connection-error/bootstrap-fail/unknown-tld/breaker-open) plus `registrar expiration` fallback.
- `tests/test_pipeline.py` — 3 new tests for `validate_availability` (keep True / reject False+None, budget=0 short-circuits, empty input). Existing `main()` integration tests stub `rdap.check_availability` to return True so survivors reach the output.

### Why HTTP 404 is the only acceptable signal

The RDAP audit showed status flags overlap heavily — `pending delete` is paired with `redemption period` in real responses, so the "pending delete = drops in 5 days" rule from the spec doesn't apply (those domains are still in redemption). The only unambiguous registry signal is "I have no record of this name," which is HTTP 404. Any HTTP 200 means the registry is asserting that *someone* owns this name — even if the name is in a transitional state.

### Why None defaults to REJECT

Transport failures, persistent 429s, an open circuit breaker, or an unknown-TLD bootstrap miss all return `is_available=None`. Treating unknown as REJECT (not ACCEPT) means we under-publish during infrastructure trouble rather than over-publish stale-state guesses. The False-vs-None distinction stays in the logs as diagnostic signal: many None values means our RDAP path is degraded; many False means the zone-diff signal itself is just noisy.

### Expected published count after fix

The audit yielded 1/21 (~5%) actually available. With ~300 candidates reaching the score stage today, expect 5–25 in the post-fix published list — possibly fewer. **A list of 5 actually-available domains is correct; a list of 300 owned ones isn't.** Site copy may need tuning later but is out of scope for this fix.

### Phase 3 verification

Ran `check_availability()` (the new code path) against the existing published list:
- 10/10 currently-published domains correctly identified as `is_available=False` (would be rejected).
- 2/2 known-available controls (`naccd.site` from the audit + a synthetic non-existent domain) correctly identified as `is_available=True`.
- All HTTP codes, expiration dates, and status flags extracted correctly.

The user has paused the GitHub Actions cron; manual workflow run + new daily-domains.json verification still pending the operator's re-enable.

---

## Pipeline reorder + politeness pass (2026-04-30)

### Problems observed in the 2026-04-29 cron run

- **Wayback returned 503, crt.sh returned 502** within ~3 seconds of enrichment start; circuit breakers tripped repeatedly with consecutive_failures climbing past 12. End result: 95%+ of enriched candidates had `wayback_snapshots=null` and `cert_history=null`. Per-host throttling at 1.0s wasn't enough — 10 workers × 1 req/s/host produced a perfectly-clockwork pattern that rate-limited services treat as suspicious.
- **Wasted enrichment**: only 10 of 952 enriched candidates were actually available per the new RDAP check. We burned ~7000 API calls (1000 candidates × 7 sources) on 942 owned/redemption domains that nobody could register anyway.

### Architectural fix

**Pipeline reordered so availability runs BEFORE enrichment.** The two problems shared a root cause: enrichment ran in the wrong position in the pipeline, on the wrong scale, with the wrong pacing.

Before:
```
zone diff → structural → lexical → enrich (1000) → filter → score → availability → publish
```

After:
```
zone diff → structural → lexical → cap → AVAILABILITY (1000) → ENRICH (5-50) → filter → score → publish
```

With enrichment running on confirmed-available domains only (typically 5-50, not 1000), we have time to be polite. The pipeline now runs enrichment SEQUENTIALLY (1 worker, not 10) with much more generous per-host pacing and irregular timing.

### Files changed

- `scripts/pipeline.py` — main() reorder. `validate_availability` moved up to run after the eval-cap and before `enrich_all`. `total_evaluated` now reflects "submitted to availability check" (the dominant filter), not "submitted to enrichment". Added a CRITICAL log line when >50% of availability checks return unknown — a tripwire that says RDAP infrastructure itself is degraded and today's published list will be unusually small.
- `scripts/enrichment/_circuit_breaker.py` — `HostThrottle.acquire()` now uses **multiplicative jitter**: each effective interval is `min_interval × random.uniform(0.75, 1.25)` so back-to-back requests don't form a deterministic clockwork pattern. Also rewrote the loop as a single-shot **reserve-then-sleep**: the previous polling loop suffered from floating-point cancellation (`(now + wait) - now ≠ wait`) and could stall in microsleeps. The new shape is simpler, race-free under the lock, and naturally serialises concurrent callers.
- `scripts/config.json` — `max_concurrent_enrichments: 10 → 1` (sequential). Per-host intervals: Wayback `1.0 → 3.0`, crt.sh `1.0 → 3.0`, OPR `0.4 → 1.0`, RDAP `0.2 → 0.4`. `availability_budget_seconds: 600 → 1500` (now budgeting for ~1000 RDAP queries pre-enrichment, not ~300 post-enrichment).
- `scripts/score.py` — null-aware normalization. A field with `None` is now excluded from BOTH the numerator and denominator: `score = sum(value × weight for populated) / sum(weight for populated)`. Previously, missing fields coerced to 0 and stayed in the denominator, which artificially capped the score for any domain with a flaky enrichment. A candidate with null Wayback but populated OPR+cert+length now scores on what's actually known. If ALL components are None (degenerate input), `score_candidate` returns None and `score_candidates` drops the row.
- `tests/enrichment/test_circuit_breaker.py` — old exact-sleep-amount test now passes `jitter_factor_range=(1.0, 1.0)` for determinism. New `test_throttle_jitter_varies_actual_interval` asserts the 75-125% spread.
- `tests/test_score.py` — replaced `test_score_treats_missing_fields_as_zero_signal` (enshrined the old wrong behaviour) with three tests per the spec: `test_full_data_score`, `test_partial_data_score`, `test_no_data_returns_none`. Updated tie-break test to use same-length apex labels (otherwise length-only signal differs and there's no tie).
- `tests/test_pipeline.py` — added `test_main_skips_enrichment_for_unavailable_domains` asserting the new pipeline order architecturally (an unavailable domain must NOT reach `enrich_all`). Updated `test_main_propagates_spam_check_config_error` to mock `check_availability=True` so the test candidate flows through to the spam-check failure under test.

### Expected behaviour with the new settings

- ~1000 RDAP availability checks at 0.4s/host with jitter → ~400-600s (under the 1500s budget)
- ~5-50 enrichments at sequential pace: each candidate needs ~3s Wayback + ~3s crt.sh + ~1s OPR + RDAP-already-cached + ~0s for blocklist DNS lookups ≈ 7-10s/candidate. 50 candidates × 10s = 500s = 8 minutes (well under the 35-minute enrichment budget)
- Wayback and crt.sh circuit breakers should not trip in normal operation. Multiplicative jitter + much slower nominal rate + sequential dispatch removes the perfect-clockwork signal.
- Score distribution should be meaningful again — top >40, median >20 — because partial enrichment no longer mathematically caps the score.

286/286 tests passing. Cron is paused; manual workflow trigger needed to verify the new flow on real data.

---

## Pacing diagnostic — empirical verification (2026-04-30)

### Question

After commit `045d002` shipped the multiplicative-jitter HostThrottle and 1-worker sequential enrichment, the next pipeline run still saw Wayback 503 / crt.sh 502 storms. Two possibilities: (A) external services are degraded, (B) our throttle isn't actually enforcing the configured interval in production. We had no proof either way.

### Method

Added DEBUG-level instrumentation gated on `config.diagnostic_logging`:

- `scripts/enrichment/_circuit_breaker.py` — `HostThrottle.acquire()` now logs `host`, `configured_interval`, sampled `factor`, `effective` interval, `delay_applied`, `since_last`, `next_allowed`.
- `scripts/enrichment/wayback.py` and `scripts/enrichment/crtsh.py` — log request initiate / response with elapsed_ms.
- `scripts/pipeline.py` — when `diagnostic_logging: true` in config, flips those three loggers to DEBUG for the run.

A standalone harness (`diag_pacing.py`, not committed) drove 20 sequential calls through each of `wayback.enrich` and `crtsh.enrich` with synthetic candidate names. No CZDS/R2 needed — this isolates the throttle's behaviour from the rest of the pipeline.

### Findings

Computed **send-to-send gap** (`since_last + delay_applied`) from the throttle's own log lines, since the diagnostic harness's "fn-invocation gap" understates the real spacing (the throttle wait happens *inside* `enrich()`, after the harness records the start time).

**Wayback (web.archive.org):** 19 non-first throttle events.
- send-to-send gap: min 2.74s, median 6.91s, mean 7.04s, max 10.38s
- compliance with effective interval (`gap >= effective`): **19/19 (100%)**
- bursts (`gap < 1.0s`): **0/19**
- 4 events triggered an actual `delay_applied > 0` (the previous request finished fast); on those, the throttle injected exactly the right amount of sleep to reach `effective`. Example: `since_last=1.83s + delay=1.16s = 2.99s` matched `effective=2.99s` to three decimal places.

**crt.sh:** 7 non-first throttle events before the breaker tripped.
- send-to-send gap: min 2.37s, median 3.52s, mean 5.39s, max 10.17s
- compliance: **7/7 (100%)**
- bursts: **0/7**
- 4 events with `delay_applied > 0`, all reaching `effective` exactly.
- The breaker correctly opened after 5 consecutive failures (mix of 10s timeouts and HTTP 404s — crt.sh's query-format handling is degraded today). The 12 subsequent calls returned 0 ms because the breaker short-circuited them before reaching the throttle, exactly as designed.

### Conclusion

**Pacing is correct.** The throttle enforces the configured interval (with multiplicative 0.75–1.25× jitter) on every back-to-back call. No code change required. The Wayback 503s / crt.sh 502s + 404s observed in the morning's run are external-service degradation, not pacing failure.

A nuance worth noting for future debugging: when *all* requests are slow (5–11s end-to-end including timeouts), the inter-send spacing is naturally `>= request_latency`, so `delay_applied=0` for most calls — the server is pacing us, not us pacing ourselves. The throttle still kicks in correctly when a request finishes quickly (e.g. crt.sh's fast 404s did trigger 2.0–3.0s injected delays).

### Operational note

`diagnostic_logging` defaults to `false`. Flip to `true` in `scripts/config.json` and run a single `python -m scripts.pipeline` (or one cron tick) to capture the throttle/enricher DEBUG stream when investigating future pacing issues. Don't leave it on in production — the log volume is ~3-4 lines per request.

---

## Wave 1.5 ship (2026-04-30) — persistence, two-card frontend, grid layout, ops hardening

Detailed commit-by-commit log lives in `WORK_LOG_2026-04-30.md`. Cross-cutting summary of what changed at architectural level:

### 14-day persistent rolling list

- New `scripts/carryover.py` with pure functions: `load_existing`, `filter_by_age`, `validate_against_zone`, `annotate_today_drops`, `annotate_carryover_days_listed`, `merge`.
- `pipeline.collect_drops` now takes `carryover_candidates=...` and returns `(drops, retained_carryover)`. Validates each TLD's carryover INSIDE the per-TLD loop while `today_set` is still in scope; this avoids holding all 13 zones (10–50M apex names each) in memory simultaneously on the GHA runner.
- TLD whose zone download/parse fails → carryover for that TLD passes through with `last_validated_date` UNCHANGED. Fail-open. Ages out naturally if the failure persists 14 days.
- New schema fields per domain: `first_seen_date` (immutable after capture), `last_validated_date` (refreshed each successful zone check), `days_listed` (derived at write time). Top-level adds: `today_count`, `carryover_count`.
- Migration: pre-persistence entries (no `first_seen_date`) get treated as `first_seen_date = today` so they survive the migration day cleanly and age naturally going forward.

### Frontend: two-card layout

- DomainTable.astro split: Card 1 "Today's drops" (`days_listed === 0`, 7 columns) + Card 2 "Still available" (`days_listed > 0`, 8 columns including "Listed").
- Shared search + TLD filter at top applies to both cards. Per-card sort state.
- Differentiated empty states: real-data zero ("No new drops met our quality bar today" / "rolling list builds up over time") vs filter-reduced zero (per-card "No domains in {Today's drops|Still available} match your filters").
- `sample-domains.json` regenerated with 5 today + 15 carryover so the cold-start fallback exercises both cards.

### Frontend: table → CSS Grid

- Replaced `<table>` markup with semantic `<div role="table"/row/cell"/columnheader">`. Each row is its own grid container with an identical `grid-cols-[200px_80px_…_1fr]` template; columns align across rows because templates match.
- Card 1: `grid-cols-[200px_80px_110px_110px_180px_120px_1fr]` (Domain, TLD, Dropped, Wayback, OPR, Verdict, Register).
- Card 2: `grid-cols-[200px_80px_110px_110px_110px_180px_120px_1fr]` (Domain, TLD, Dropped, **Listed**, Wayback, OPR, Verdict, Register — Listed reordered to position 4 next to Dropped, both temporal).
- Domain through Verdict are content-sized fixed pixels; Register is `1fr` and absorbs all card slack.
- **Inline popover, not absolute:** Register cell is a flex container `[button | popover-icons]`. Popover starts `hidden`; JS toggles `flex` class on open so it lays out inline next to the button using the 1fr slack space the cell already owns. Killed three classes of bug: `<td>`-as-containing-block inconsistency, overflow-clip from the scrolling wrapper, overlap with neighbouring columns.

### Methodology section: 3 → 6 cards

- Walks the actual pipeline order: Catch → Filter → Verify → Enrich → Score → Publish.
- 1×6 mobile, 2×3 tablet, 3×2 desktop.
- New SVG icons (Lucide-style stroke geometry, no runtime icon library) for shield-check / database / bar-chart / file-check.

### Enrichment hardening

- `config.api_request_timeout_seconds.{wayback,crtsh}: 60` (per-enricher override; other enrichers still 10 s default).
- New `retry_on_timeout` helper in `_circuit_breaker.py` — 3 attempts, 5 s + 15 s backoff. Retries ONLY on Connect/Read/Timeout; other failures (HTTPError, ConnectionError, JSON) propagate immediately. One breaker failure recorded per FINAL failure, not per attempt.
- Wired into `wayback.enrich` and `crtsh.enrich`. Each retry re-acquires the host throttle slot so retries respect per-host pacing identically to first attempts.

### Diagnostic instrumentation

- `config.diagnostic_logging: false` flag added. When `true`, the pipeline flips throttle + wayback + crtsh loggers to DEBUG: per-request initiate/response timing in the enrichers + per-acquire throttle trace (`configured`, `factor`, `effective`, `delay_applied`, `since_last`).
- Empirically verified pacing is 100% compliant via 20-call probe on 2026-04-30. Today's external 503/502 storms were upstream-service degradation, not us hammering.

### Operational changes

- `.biz` activated (CZDS approved this morning). `tlds.approved` now contains 13 entries: `app, dev, live, studio, tech, online, site, store, xyz, info, org, shop, biz`. `tlds.pending`: `com, net`.
- GitHub Actions `schedule:` cron removed. Workflow now only triggers via `workflow_dispatch`. Cloudflare Cron Trigger Worker (`domainsifter-cron-trigger`) dispatches via the GitHub API on schedule. Free-tier GHA cron queue delays no longer affect us.
- Newsletter signup wired to Buttondown's public embed endpoint (`https://buttondown.com/api/emails/embed-subscribe/domainsifter`). No API key in client code. Honeypot field, inline submit with status, no page navigation.

### Test surface

- 286 → 325 tests (39 new across `test_carryover.py`, `test_pipeline.py`, `test_output.py`, `test_circuit_breaker.py`, `test_wayback.py`, `test_crtsh.py`).
- All passing in ~12.5 s on Python 3.10.

### Known follow-ups (not blockers)

- Cron is paused. Trigger workflow_dispatch manually after the next Cloudflare Pages deploy to validate the new persistence flow on real data; then re-enable the Worker schedule.
- Tomorrow's first persistent-list run should keep `nomoda.org` as a carryover entry showing "Listed: 1 day ago" in Card 2 (assuming it stays absent from the .org zone overnight).
- DMARC remains `p=none` — calendar reminder for 2026-05-24 to consider stricter policy.

---

## Operational changes since 2026-04-30

Source: `git log` between `4956e95` (the 2026-04-30 STATE refresh) and HEAD. Each entry tagged with `[from git log: <hash>]`. Provenance for every claim is the linked commit-message body — chat-only context that didn't make the commit message is **not** included here; Mario fills those gaps in review.

### 2026-04-30 — Compliance / SEO add-ons (same-day after STATE refresh)

- **Privacy Policy and Terms of Service pages added** at `/privacy` and `/terms` with `noindex`. `[from git log: d2e8509]` Reason not in commit body — `[needs context]`. Likely required ahead of the impact.com (Namecheap affiliate) integration that landed minutes later.
- **`impact.com` site-verification meta tag added** to the global Layout. `[from git log: 8c4dca5]` Reason not in commit body — `[needs context]`. (Affiliate-network identity verification is the conventional reason for such a tag.)

### 2026-05-01 — Persistent-list polish, GMO Registry rate-limit fix, copy honesty

- **First-run schema migration glitch on `nomoda.org`** surgically backfilled in `daily-domains.json`: `first_seen_date 2026-05-01 → 2026-04-30`, `days_listed 0 → 1`, top-level `today_count 5 → 4` and `carryover_count 0 → 1`. `[from git log: 1479693]` This was a one-shot data fix for the cutover from the old schema to the new persistent-list schema; the entry would otherwise have been mis-attributed to "today" instead of carryover.
- **Honesty disclaimer added** beneath the "Last updated" timestamp on the homepage. `[from git log: a895bfd]` Reason not in commit body — `[needs context]` (commit body empty beyond subject). The subject ("availability-staleness disclaimer") implies the user-facing acknowledgement that the listed-as-available status of a domain may have changed between cron run and the visitor reading it.
- **Card 2 renamed "Still available" → "Recent drops"** to avoid an implicit availability claim. `[from git log: b983b2f]` Subject explicitly cites the *why*: the previous label asserted current availability, but the list is a snapshot — a domain may be re-registered between cron runs. "Recent drops" is descriptive without making a real-time guarantee.
- **Per-host RDAP throttle override added for `rdap.gmoregistry.net` (3.0 s, vs the 0.4 s global)**. `[from git log: c0799a1]`
  - Empirical probe on 2026-05-01 measured: 5 s gaps → 80 % 429s; 30 s+ gaps → clean recovery.
  - Today's pipeline lost 51 `.shop` candidates to persistent 429s under the global 0.4 s interval before this fix.
  - GMO Registry serves `.shop` plus 46 other TLDs, so the override has broad reach despite being a single host.
  - Same commit also adds a `Retry-After`-header log line on exhausted backoff so future rate-limit diagnostics don't need a manual probe to discover the registry's hint.
  - The fix is configuration only; the existing `rdap_per_host` lookup chain (added earlier) handled this without code changes.

### 2026-05-02 — Filter-audit tooling

- **`--debug-export PATH` flag added to `scripts/pipeline.py`**. `[from git log: 86cbb8b]` (Commit body is just the subject; the *why* lives in the new module's [scripts/debug_export.py](scripts/debug_export.py) header rather than the commit message.) Module header explains: when the flag is set, the orchestrator dumps five intermediate plain-text lists (`lexical_rejects.txt` with reason suffix, `lexical_survivors.txt`, `trim_kept.txt`, `trim_discards.txt`, `published.txt`) plus an optional `_meta.json` to the given directory. Production runs that don't pass the flag never collect or hold the lists in memory — gating is at collection-time, not write-time, because `lexical_survivors` alone can be 17k+ strings on a tight 7 GB GHA runner. Also adds an additive `rejections_out` kwarg to `lexical_filter.filter_candidates` (default `None` ⇒ no allocation) so per-rejection `(name, rule_key)` pairs can be captured via side-channel without changing production behaviour. Output dir `scripts/state/debug-exports/` added to `.gitignore`.

### 2026-05-03 — Per-host RDAP concurrency + ops headroom

- **Per-host RDAP concurrency landed.** `[from git log: 1bb33d9]` Replaces the previous strictly-sequential availability loop with one `ThreadPoolExecutor` per distinct RDAP host, all pools running concurrently. Per-host workers serialise on the existing thread-safe `HostThrottle` so per-host request rate is unchanged; total wall-clock is cut by `N-1` where `N` = number of distinct hosts touched. Today's 1000-candidate sequential run was ~25 min; with 8 host buckets in parallel at default concurrency=1 the projection is ~10–12 min. New config: `rdap_concurrency.{default_workers_per_host=1, per_host={}}` — defaults are safe, per-host overrides remain empty until empirically validated. **Architectural note from the commit body:** this design scales to `.com` / `.net` joining the fleet (Verisign single host) without code changes — only config tuning if/when we want >1 worker on Verisign's bucket. Adds `resolve_rdap_host(domain, config)` helper to `scripts/enrichment/rdap.py` (used by the orchestrator to bucket candidates without touching `check_availability` itself). 333/333 tests pass (5 new: 1 throttle thread-safety, 4 pipeline concurrency).
- **GitHub Actions workflow `timeout-minutes: 45 → 60`**. `[from git log: 53f55a1]` Belt-and-suspenders headroom while the new RDAP concurrency validates naturally on tomorrow's 05:17 UTC cron. Today's sequential run was 33 min at the 1000-cap; with `.com` / `.net` joining in 1–3 weeks plus an eventual trim-cap bump, 60 min gives margin to absorb both without workflow cancellation. The commit body explicitly notes the timeout *can* revisit downward once steady-state is measured.
- **Dynadot wired as third affiliate registrar.** `[from git log: c7d78c9]` (Retroactive: this commit landed AFTER the same-day 2026-05-03 STATE refresh `61ffb83`, so it was missed in that update. Filled in 2026-05-04 with explicit authorization.) Approved as Dynadot Ambassador on 2026-04-30, ID `domainsifter`. Wired identically to the existing Namecheap and NameSilo pattern at all four documented touchpoints: `scripts/config.json` `registrars[]` entry (link template `https://www.dynadot.com/domain/search?domain={name}&rscreg=domainsifter`), `REGISTRAR_LOGOS` map in `src/components/DomainTable.astro` (third entry), 64×64 PNG at `public/registrar-logos/dynadot.png` fetched from Google's favicon proxy and MD5-verified ≠ generic-globe fallback, and Terms-page disclosure copy updated from "(Namecheap, NameSilo)" to "(Namecheap, NameSilo, Dynadot)". **Caveat from the commit body:** the `?domain={name}` pre-fill parameter is **UNVERIFIED** — `dynadot.com` sits behind a Cloudflare bot challenge that returns HTTP 403 to non-browser clients, so the empirical test specified in the prompt was blocked. Defaulted to the pre-fill pattern for parity with Namecheap/NameSilo UX. If Dynadot silently ignores `?domain=`, the affiliate parameter still tracks correctly — user just lands on the generic search page. Manual browser verification on first live click is the natural follow-up. Out of scope for this commit: Dynadot's auctions/backorders/closeouts category links (Dynadot's program pays commission on those for both new AND existing customers, uncommon — most programs only pay first-time; tracked as a separate future concern). About-page copy not touched (already genericised to "affiliate registrars" per the 2026-04-30 entry above). 333/333 tests pass; the `test_output.py` `REGISTRARS` fixture intentionally left at 2 entries because it tests Namecheap/NameSilo URL-substitution behavior specifically, not iteration over a configurable list.

### Test surface (delta since 2026-04-30)

- 325 → **333 tests passing** (`+8`). New tests cover: filter-audit gating (`+2` in `test_pipeline.py`), per-host RDAP throttle override (`+1` in `test_rdap.py`), per-host RDAP concurrency (`+4` in `test_pipeline.py`), `HostThrottle` thread-safety under concurrent acquirers on the same host (`+1` in `test_circuit_breaker.py`).

### New known follow-ups (additive to the 2026-04-30 list above)

- Tomorrow's 05:17 UTC cron is the natural validation for both the per-host RDAP concurrency (target: <15 min total RDAP phase, vs. ~25 min sequential today) and the `.shop` throttle override (target: zero `.shop` candidates lost to 429s).
- Workflow timeout bump can be revisited downward once steady-state runtime under the new concurrency is measured.
- Trim-cap bump above 1000 is contemplated as a separate config-only change after the RDAP runtime is empirically below the new 60 min ceiling.
- The `rdap_concurrency.per_host` map ships empty. Populate only with empirically-validated overrides — the config doc-string explicitly warns against speculative tuning.

### 2026-05-04 — Length-asc trim retired; full-fleet availability runs

- **Per-host availability caps replace the global 1000-candidate length-asc trim.** `[from git log: 1a5ba36]` The trim heuristic shipped with the original pipeline (`max_candidates_for_enrichment = 1000`) sorted lexical survivors by ascending length and took the top 1000. Today's diagnostic Wayback audit on a 200-name `trim_discards` sample (commit `86cbb8b`'s `--debug-export` tooling, plus a one-shot Wayback enricher in working dir) hit a Wayback 503 stop-loss at 39/200 attempts — partial sample. Within the 27 successful enrichments, 3 (~11 %) had ≥100 Wayback snapshots, similar density to the prior R2-zone audit's `trim_kept` cohort. Conclusion: **length-asc was quality-neutral on Wayback signal** but biased toward already-registered short names — today's prior 1000-cap run had `996/1000 trim_kept` come back unavailable from RDAP, leaving only 4 candidates for enrichment. Replacement is `_bucket_and_cap_for_availability(...)` in `scripts/pipeline.py`: groups lexical survivors by RDAP host (using `resolve_rdap_host` from commit `1bb33d9`), applies a per-host cap derived from a runtime budget, random-shuffles within over-cap buckets (seed = `today.strftime("%Y%m%d")`).
- **Cap math:** `per_host_cap = floor(max_runtime_per_host_seconds × workers / effective_throttle)`. Effective throttle uses the existing `rdap_per_host[host] → rdap → 0.4` chain; workers use the existing `rdap_concurrency.per_host[host] → default_workers_per_host → 1` chain. Each bucket is wall-clock-bounded at the budget regardless of fleet size, and because all host pools run concurrently (commit `1bb33d9`), total RDAP phase wall-clock is `max(bucket_runtimes)`, not the sum.
- **Live config (`scripts/config.json`):**
  - `availability_check.max_runtime_per_host_seconds = 900` (15 min)
  - `availability_check.global_cap = 15000` (safety net for pathological filter-leak days)
  - At today's settings, the per-host caps work out to: `0.4 s` hosts ⇒ 2,250 each; GMO Registry's `3.0 s` override ⇒ 300. Sum across the 9 hosts touched today ≈ 18,000 candidates. **Today's lexical survivors (~14,000) sit below total capacity — caps don't actually engage.** This is the first run since launch where the pipeline operates at design-intent volume; every survivor that reached the trim line gets RDAP-checked.
- **Random-shuffle, NOT length-asc, when a bucket overflows.** The seed is `int(today.strftime("%Y%m%d"))` so a given day's bucket-cuts are reproducible from raw inputs, which matters for debugging.
- **Decisions explicitly NOT made:** (a) `rdap_concurrency.per_host` stays empty — no per-host worker overrides ship in this commit; (b) `rdap_per_host` is unchanged (only GMO at 3.0 s); (c) `max_concurrent_enrichments` stays at 1 (enrichment phase concurrency is a different shape, separate decision); (d) `rdap.check_availability` and `HostThrottle` are not modified — the new logic lives entirely in the orchestrator, consistent with the architectural posture from commit `1bb33d9`.
- **Verisign (`.com` / `.net`) anticipated within 1–3 weeks.** Architecture handles it without code changes: a Verisign bucket would default to a ~2,250 cap at current config. If `.com` / `.net` volumes exceed 2,250, future config-only knobs are (a) raise `max_runtime_per_host_seconds`, or (b) add `rdap_concurrency.per_host["rdap.verisign.com"] = N`. Both are config edits, no pipeline.py changes.
- **GitHub Actions workflow `timeout-minutes: 60 → 120`**. `[from git log: 9677452]` Belt-and-suspenders insurance for the first run with the uncapped availability check. Yesterday's bumped-cap-but-old-trim run was 22 min; today's run with ~14× more candidates flowing through availability could be 40–60 min realistically. 120 min gives margin for early surprises before downward calibration.
- **Then `120 → 150`**. `[from git log: 4d88d39]` Sized against the upper-bound runtime estimate: the GMO Registry bucket dominates at ~1,800 `.shop` candidates × 3.0 s throttle ≈ 90 min of sequential wall-clock for that single host, plus zone download (~5 min), enrichment (variable, depends on how many pass RDAP), and other steps (~3 min). Worst-case ~110–130 min. 150 min leaves ~20 min of headroom without other cost.
- **Test surface today: 333 → 335.** Removed 2 obsolete `_trim_for_enrichment` tests (function deleted), added 4 new tests for the cap-and-bucket logic: per-host cap calc respects the config lookup chain; bucket overflow triggers random-shuffle (NOT length-asc — explicitly asserted by checking the kept names are not the alphabetically smallest); global cap engages when total exceeds it; deterministic seed reproduces the same trim across runs.
- **New known follow-ups (additive):**
  - Tomorrow's 05:17 UTC cron is the empirical validation. Three numbers to watch in the run log: (1) per-bucket sizes before vs after capping (does any bucket actually need its cap today?); (2) total wall-clock (target <60 min, ceiling 150 min); (3) `available` count post-RDAP (target 50–150, vs today's 4 under the old trim).
  - If steady-state runtime is well under 150 min, lower the workflow timeout — kept high deliberately for the first uncapped run.
  - If a single bucket consistently runs near its cap, evaluate raising `max_runtime_per_host_seconds` or adding a per-host concurrency override for that host. Both config-only.
  - The 2026-05-04 trim-discards Wayback audit ran into a 503 storm at 39/200 attempts — an unrelated Wayback availability concern. Re-run on a clean Wayback day to get full 200-sample resolution if we want firmer numbers on the audit ratio.

### 2026-05-05 — First publish day under the new architecture; registry ban response

- **First Promising-tier candidate published since the architecture switch.** Today's run produced **7 publishes**, including `multimediadesigns.org` with 888 Wayback snapshots — the first time the new uncapped-availability + per-host-bucket pipeline (`1a5ba36`, `1bb33d9`) surfaced a strong candidate at scale. Confirms the design intent of the 2026-05-04 trim retirement: removing the length-asc bias toward already-registered short names did surface higher-quality candidates in the discards, the audit's optimism caveat notwithstanding.
- **Registry RDAP ban events triggered emergency throttle recalibration.** `[from git log: b98fb63]` Today's first uncapped run produced multi-hour `Retry-After` bans from registry RDAP servers, all observed in production logs:
  - `rdap.identitydigital.services` returned `Retry-After: 86397s` (~24 hours): 1069 candidates checked, 949 came back unknown.
  - `rdap.publicinterestregistry.org` returned `Retry-After: 3600s` (1 hour): 1966 candidates checked, 1628 came back unknown.
  - `rdap.gmoregistry.net` produced 49 `unknown` results at the previous 3.0s throttle — slightly stressed, no ban.
  - `pubapi.registry.google` and `rdap.nic.biz` showed degraded response times but did not ban.
  - `rdap.centralnic.com` and `rdap.radix.host` were clean at the previous 0.4s default.
- **Throttle calibration shipped (commit `b98fb63`):** conservative empirical settings with safety margin, accepting reduced volume in exchange for zero ban risk going forward, because a permanent ban from any registry would block the pipeline for that TLD entirely.
  - `rdap.identitydigital.services`: `0.4s → 5.0s` (banned today, most cautious)
  - `rdap.publicinterestregistry.org`: `0.4s → 3.0s` (banned today, matches GMO caution)
  - `rdap.gmoregistry.net`: `3.0s → 4.0s` (defensive bump after 49 unknowns)
  - `pubapi.registry.google`, `rdap.nic.biz`: `0.4s → 2.0s` (degraded today)
  - `rdap.centralnic.com`, `rdap.radix.host`: `0.4s → 1.0s` (clean today, defensive margin since they only ran 2250 candidates each — not a stress test)
  - **Default `rdap` throttle: `0.4s → 2.0s`** — protects against new/unknown registries (e.g. Verisign when `.com`/`.net` land).
  - `availability_check.max_runtime_per_host_seconds`: `900 → 2700` to compensate for slower throttles. Per-host candidate volume stays similar; each bucket now wall-clock-bounded at 45 min instead of 15 min. Total RDAP phase still `max(bucket_runtimes)` (concurrent), so worst-case wall-clock ~45 min.
- **Cloudflare Worker cron moved `05:17 UTC → 06:30 UTC`.** No repo commit — configured in the Cloudflare dashboard on the `domainsifter-cron-trigger` Worker. Reason: the previous 05:17 UTC trigger was inside the 24-hour `Retry-After` window of any registry banned during the previous day's run. 06:30 UTC provides 1+ hour buffer past any 24h cooldown started during the previous day's run. The bumped GHA `timeout-minutes: 150` from yesterday is unaffected — it's a job-level ceiling, independent of the trigger time.
- **New CLAUDE.md operational rule.** [CLAUDE.md](CLAUDE.md) gained a new `### Operational rules` sub-category under `## Hard rules — never violate`. **Rule 20** pins the cron-timing constraint: never move the daily trigger earlier than 06:30 UTC without verifying that no recent registry RDAP ban events would still be active at the new time. Same commit also fixes the stale "06:00 UTC" reference in the file's `### GitHub Actions` how-to section to match current reality (06:30 UTC, controlled by Cloudflare Worker not GHA `schedule:`).
- **Decisions explicitly NOT made:** (a) `rdap_concurrency.per_host` stays empty — the ban events were rate-limit problems, not concurrency problems, so adding per-host workers would have made things worse not better; (b) `global_cap` of 15000 unchanged — lexical-survivor throttling stays at the gate, not the bucket; (c) `rdap.check_availability` and `HostThrottle` untouched — the recalibration is config-only.
- **Trade-off accepted:** ~3–4 publishes/day projected at the new throttles, vs. today's 7. A permanent registry ban would block the pipeline for that TLD entirely, which is unrecoverable. Conservative settings buy back ban risk; future tightening should be empirical-from-logs, not speculative.
- **New known follow-ups (additive):**
  - identitydigital's 24-hour ban from today expires around 05:30 UTC tomorrow — just before the new 06:30 UTC cron fires. Watch tomorrow's first identitydigital bucket log line: if `Retry-After` still appears, the actual ban window is longer than the documented 24h and we'd want to bump 5.0s further (or skip identitydigital TLDs for one cycle).
  - PIR's 1-hour ban from today expired hours ago; tomorrow's first PIR bucket should run cleanly at 3.0s. If still bans, escalate to 5.0s.
  - Worker cron schedule lives in Cloudflare dashboard, NOT in repo — there is no source-of-truth file to grep for it. Document the current schedule in CLAUDE.md (rule 20 + the how-to line) so future sessions know the actual trigger time without checking the dashboard.
  - If steady-state under the new throttles stabilises at ~3 publishes/day, evaluate (a) tightening lexical filter pre-conditions to reduce lexical-survivor count, or (b) loosening the most aggressive per-host throttles after a 1–2-week clean-log streak.

### 2026-05-06 to 2026-05-08 — Pipeline stability and the enrichment bottleneck

The arc this week: from "post-calibration, will it hold?" (Wed) → first fresh Promising-tier publishes (Thu) → best day yet (Fri), interrupted by an enrichment-budget bottleneck that produced two coordinated fixes (Wayback package swap + budget bump). Daily refresh commits (`a1755e7`, `ca7eeab`, `f9dcfa8`) are mechanical; the substantive entries below.

- **2026-05-06 — first clean run after the Tuesday recalibration.** No new commits. Production log: zero `Retry-After` warnings across all RDAP buckets (identitydigital's 24-hour ban from 2026-05-05 cleared as expected, throttles holding at the new conservative settings). 22 RDAP-available, top score 47, 7 publishes (carryover-driven — only ~3 fresh today's-drops met the publish floor). Empirical confirmation that the conservative throttle calibration in commit `b98fb63` was correctly sized.
- **2026-05-07 — first fresh Promising-tier publishes under the new architecture.** Production log: `ipstresser.xyz` (Wayback 915) and `livetrackerpro.xyz` (Wayback 122) both scored above the Promising threshold. First time the post-trim-retirement architecture surfaced *fresh* (not carryover) Promising candidates — confirming the 2026-05-04 design intent (commit `1a5ba36`) that random-sampling across all lexical survivors would surface high-Wayback names the length-asc trim was systematically discarding.
- **Same day (2026-05-07): vestigial budget knob discovered, fixed.** `[from git log: c0b2440]` All RDAP host buckets completed at exactly 1500s (25 min) despite `max_runtime_per_host_seconds = 2700` (45 min). Diagnostic identified `availability_budget_seconds = 1500` as a separate, older deadline knob from the pre-per-host-bucketing era (commit `7a43190`, before commit `1a5ba36` introduced the per-host budget) — never reconciled. The single global deadline was firing as a guillotine before any bucket finished its planned 2700s of work — **3,350 candidates `skipped (budget)`** on that run despite per-host caps being correctly sized for 2700s. Fix: `availability_budget_seconds: 1500 → 2700` to match. The global deadline now acts as a redundant safety net rather than the binding constraint. The diagnostic also surfaced that the `validate_availability` doc-string still described the old single-stage model; left in place because it's accurate as a fail-safe description of the now-redundant deadline.
- **2026-05-08 — best day of the launch arc to date.** Production log: 75 RDAP-available (vs Tuesday's 44), top score 67, **median 39 (above the 30 publish floor)**, 18 published total = 10 fresh today's-drops + 8 carryover. **5 Promising-tier publishes**, including:
  - `collegelabor.org` — Wayback 3293, OpenPageRank 2.5
  - `thereedgroup.org` — Wayback 656, OpenPageRank 2.2
  - First time we've seen meaningful OpenPageRank values in the published cohort. Wayback signal alone is "did this site ever exist"; combined with non-zero OpenPageRank it becomes the real "businesses that lapsed" signal the project was designed to surface.
- **Same day (2026-05-08): Wayback degradation surfaced an enrichment-phase bottleneck.** Production log: only **30 of 75** RDAP-available candidates got enriched (45 skipped budget). Multiple 200-second Wayback timeouts (3 attempts × 60s under the legacy 5s+15s backoff) ate ~20 minutes of the 35-min enrichment phase. Total enrichment phase wall-clock 2,243s vs the configured `2100s + 60s` grace ceiling. Two coordinated fixes shipped in sequence:
- **Fix #1: Wayback package swap.** `[from git log: 82c574c]` Replaced the hand-rolled `requests`-based CDX client in `scripts/enrichment/wayback.py` with the canonical EDGI / `internetarchive` `wayback` Python package (BSD-3-Clause; pinned `wayback>=0.4.5` in `requirements.txt`). Same public function signature, same return shape (`wayback_snapshots`, `wayback_last_snapshot`), same logging conventions — drop-in at the call boundary. Why the package handles this better:
  - 60-second adaptive delay on rate-limit errors (vs the previous 5s + 15s)
  - Built-in connection pooling and session reuse
  - Polite defaults designed for IA's documented 60-req/min limit
  - No API key required, used in production by EDGI for environmental data archiving
  - Conservative session settings: `WaybackSession(retries=2, backoff=2, timeout=60s, search_calls_per_second=1/min_interval)` — worst-case ~75s per call instead of the previous 200s.
  - The orchestrator-level `CircuitBreaker("wayback")` is preserved on top of the package's own retry behaviour; the two layers don't conflict because the package's retries reduce per-call failure rate, while the breaker still trips after N consecutive call failures so the whole pipeline doesn't burn budget on a definitively-down host.
  - Test surface unchanged (335 → 335) but tests rewritten to mock at the `WaybackClient` boundary instead of the previous `responses`-based HTTP layer. The old fixtures used partial CDX rows (`["timestamp"]` + `["20200101000000"]`) that wouldn't deserialise under the package; new fixtures mock at the package's `client.search(...)` seam, which is the semantically-correct test boundary.
- **Fix #2: enrichment budget bump.** `[from git log: 19f813b]` `enrichment_time_budget_seconds: 2100 → 3000`. Today's run hit the 2,100s + 60s grace ceiling at 2,243s actual; even with the package swap reducing per-call worst-case timeouts, headroom is cheap and external degradation is unpredictable. 3,000s gives ~12 min margin against the next Wayback or crt.sh slowdown event. Workflow timeout 150 min still has ~50% headroom; on a bad day the total run could approach 100–110 min.
- **Decisions explicitly NOT made:** (a) did not change `crt.sh`, OpenPageRank, RDAP, or the blocklist enrichers — degradation was Wayback-specific; (b) did not bypass the orchestrator-level breaker (the package's retries reduce noise but the breaker remains the ultimate cutoff); (c) did not narrow the catch in the new `wayback.py` to just `WaybackException` — added a defensive `except Exception` that also returns `{}` and records breaker failure, in case the package surfaces urllib3/requests errors that don't subclass its own exception hierarchy. If first-cron logs after the swap show a generic-exception warning we'd have the actual type and could narrow.
- **Cumulative arc this week — qualitative.** From the architecture pre-2026-05-04 (1000-candidate length-asc trim, occasional 0-publish days) to the architecture post-2026-05-08 (8000+ candidates evaluated, top scores in the 60s, consistent fresh-Promising publishes), the discipline that worked: diagnostics before fixes, generous defensive throttles on first calibration, ship one architectural change at a time and validate naturally on next cron. Every week-day this week produced data that validated or invalidated the previous day's configuration; nothing speculative shipped.
- **Test surface (delta since 2026-05-05):** 335 → **335** (net zero). Wayback test count unchanged; their internals were rewritten to mock at the package boundary (12 tests covering success path, multiple failure modes, breaker integration, config wiring).
- **New known follow-ups (additive):**
  - Watch tomorrow's first cron under the package + budget changes. Targets: ≥60 of 75 RDAP-available enriched (vs today's 30/75); total enrichment phase ~25–30 min (vs today's 35+); zero generic-exception warnings from the new defensive catch in `wayback.py` (if any appear, narrow the catch to the actual surfaced exception types).
  - If the package's `WaybackException` catch produces noisier logs than expected (e.g. `BlockedByRobotsError` per-domain), consider downgrading those specific subclasses from WARNING to DEBUG — they're "nothing wrong with our code" signals rather than failure events.
  - Common Crawl backlink integration (Wave 2): see new section below. Trigger to start: this week's stability holding through one more clean cron.

### 2026-05-09 — Verisign approval + User-Agent fingerprint + GMO defensive bump

- **Today's cron shape (pre-Verisign config):** 7,812 evaluated, 28 RDAP-available, 30 enriched, **1 fresh publish (`promo-pro.online`, Caution-tier) + 18 carryover.** Wayback was externally degraded throughout the enrichment phase — `HTTP 111 Connection refused` errors against `web.archive.org` from start to finish. **20 of 28 enriched candidates rejected by the post-enrichment filter for `no_wayback`** (no snapshot signal because the API itself was down). Pipeline architecture worked exactly as designed under the EDGI package's retry behaviour and the orchestrator-level breaker — the package's adaptive backoff did its job, the breaker tripped, the run completed within budget. External degradation cost the day's quality candidates; nothing in our codebase was at fault. Today is therefore not a useful signal for or against the post-2026-05-08 enrichment-bottleneck fixes — Wayback was simply unreachable.
- **Verisign zone-file approval landed.** Two emails received 02:26 UTC: "Zone File Access for .com Approved" and ".net Approved". Today's cron logged `CZDS approved 15 zones; 13 match our TLD list` — the two new zones were sitting unused because `tlds.approved` still listed only the original 13 TLDs. Approval timing matched the 1–3 week estimate captured in the 2026-05-03 STATE entry.
- **Strategic deviation from yesterday's plan, accepted.** Yesterday's STATE.md framed Common Crawl integration as the next milestone, with trigger "this week's stability holding through one more clean cron run." Verisign's approval landing earlier than expected forces a re-prioritisation: **ship Verisign with conservative throttle now, ship Common Crawl next week.** Reasoning: Verisign approval is a one-way door (the CZDS approval is granted; we use it or risk it being treated as inactive); Common Crawl is a green-field add that can wait one more week without operational impact. The 2026-05-08 plan to integrate CC *before* the Verisign-driven volume jump remains correct in spirit — just sequenced differently because the volume jump arrived first.
- **Per-registry throttle audit completed (read-only investigation).** Findings to inform future calibration decisions:
  - **5 of 8 calibrated registries publish no numeric rate limit** and rely on discretionary ToS enforcement: Verisign, PIR, Identity Digital, GoDaddy/.biz, Radix.
  - **GMO publishes a soft behaviour signal**: their `/help` endpoint documents a 60-second backoff after 429 (verbatim from the HTML body), but the threshold itself is not published.
  - **Identity Digital publishes a numeric WHOIS limit (10 q/s per source IP) but no RDAP-specific limit.** Critically, the 2026-05-05 24-hour ban occurred at 2.5 q/s — **4× under** their documented WHOIS rate. Rate alone cannot fully explain the ban; possible secondary contributors include the volume jump from `1a5ba36` (10–25× per-host increase from cap removal) and the User-Agent fingerprint (see next bullet).
  - **CentralNic is the only registry with a documented numeric ceiling for RDAP**: 1,800 queries per 15 minutes (= 2 q/s), with IPv4 /24 prefix aggregation. Our calibrated 1.0 q/s is exactly half — defensible margin given GHA shared-runner /24 aggregation risk.
  - **Google publishes conflicting signals**: a developer-portal page (now redirected, Google Domains was sunset) said "no usage limits"; the active `registry.google` ToS prohibits "high volume automated processes". We treat as discretionary.
  - **GMO flagged as the most-exposed of currently-clean hosts.** Today's cron logged 8 `Retry-After:0` soft warnings against `rdap.gmoregistry.net` at the previous 4.0s throttle — soft warnings argue for tightening, never loosening.
- **Concurrency investigation completed (read-only).** Hypothesis: an unobserved concurrency leak contributed to the 2026-05-05 identitydigital ban. Result: **ruled out on direct code evidence.**
  - Single-worker-per-host posture verified at [scripts/pipeline.py:436-440](scripts/pipeline.py#L436-L440); `default_workers_per_host: 1` and `per_host: {}` confirmed in `scripts/config.json`.
  - HostThrottle reserve-then-sleep pattern correctly serialises concurrent acquirers ([scripts/enrichment/_circuit_breaker.py:184-212](scripts/enrichment/_circuit_breaker.py#L184-L212)); the `_last_request_at[host] = now + wait` write inside the lock at line 194 is the key invariant.
  - No async paths in `scripts/`, no `requests.Session` reuse across calls, no HTTP/1.1 keep-alive across requests (bare `requests.get()` per call). Each RDAP request opens its own TCP connection and closes it.
  - Retry path re-acquires the throttle ([_circuit_breaker.py:320-321](scripts/enrichment/_circuit_breaker.py#L320-L321)) so 429-handling cannot create overlapping requests.
  - **The 2026-05-05 ban root cause was volume-driven, not concurrency-driven**: the `1a5ba36` cap-removal commit produced a 10–25× per-host volume jump at unchanged 0.4s pacing, which PIR (1966 actually checked) and Identity Digital (1069 actually checked) correctly identified as bulk-querying behaviour and banned.
- **User-Agent fingerprint finding (separate epistemic gap surfaced by the concurrency audit).** All RDAP requests prior to today were sending the literal default `python-requests/2.32.3` User-Agent because no `headers={...}` was passed to `requests.get()`. This UA is heavily flagged by WAFs (Cloudflare Bot Management in particular, which fronts Identity Digital's RDAP and almost certainly Verisign's). The 2026-05-05 ban at 4× under-documented-rate is consistent with fingerprint-based detection that throttle calibration alone cannot address. **Fix shipped today** `[from git log: 940cfff]`: named, contactable UA `DomainSifter/1.0 (+https://domainsifter.com; contact: hello@domainsifter.com)` set on all three `requests.get()` calls in [scripts/enrichment/rdap.py](scripts/enrichment/rdap.py) (bootstrap + enrich + check_availability). Test added to assert UA on every outbound RDAP call. Industry best practice for legitimate scrapers — signals "service operator who can be reached" rather than "hostile bot." Particularly relevant before exposing the fingerprint to Verisign.
- **Verisign rollout shipped at 4.0s throttle** `[from git log: cb309f0]`. Mario's choice over the agent's 3.0s recommendation. Reasoning: 70% blast radius if banned (Verisign serves `.com` + `.net` + `.name` + `.cc` from a single shared `rdap.verisign.com` host); undocumented Verisign rate-limit policy; ToS contains the same "high volume automated processes" prohibition that's been the discretionary-policy template across 5 registries we've already calibrated; the Identity Digital mystery ban precedent argues for extra margin on first deployment. **Loosen to 3.0s only after 1–2 weeks of clean Verisign runs.**
- **GMO defensively bumped 4.0s → 5.0s** in the same commit. Today's cron logged 8 `Retry-After:0` soft warnings; today's throttle audit flagged GMO as the most-exposed registry where the current value could plausibly trigger bans. Soft warnings argue for tightening, never loosening.
- **Per-host cap math under the new throttles:**
  - Verisign at 4.0s × 1 worker × 2700s budget → `floor(2700 / 4.0) = 675` candidates/day for the combined `.com+.net` bucket. Expected daily `.com+.net` lexical-survivor volume is ~30–35k (rough estimate; will be calibrated empirically tomorrow). The cap discards ~98% of the bucket; ban-prevention takes priority over volume on first deployment.
  - GMO at 5.0s × 1 × 2700s → `floor(2700 / 5.0) = 540` candidates/day for `.shop` (down from 675 at 4.0s). `.shop` daily volume is well below this so the cap is non-binding.
  - All other buckets unchanged.
- **Test surface (delta since 2026-05-08):** 335 → **336** (+1 for the User-Agent assertion).
- **New known follow-ups (additive):**
  - **Watch tomorrow's first Verisign-enabled cron carefully.** Specifically look for: any `Retry-After` warnings from `rdap.verisign.com` (would mean tighten further, possibly to 5.0s or skip Verisign for one cycle); the Verisign bucket completion time vs configured budget; the unknown count vs the available count; any `cf-*` response headers in transport-failure logs (Cloudflare evidence — informs whether the UA fix was directionally right).
  - **Watch GMO bucket at the new 5.0s throttle.** If soft warnings persist after the bump, escalate to 6.0s or 7.0s rather than accepting them as "soft." Soft warnings are a leading indicator of a hard ban.
  - **The User-Agent change is one new variable.** If any host that ran cleanly yesterday returns a `Retry-After` or `403` tomorrow that didn't appear before, the new variable is implicated and the UA string is the first thing to revert.
  - **Common Crawl backlink integration starts next week** if Verisign holds clean for 2–3 days. Same scope as captured in the Wave 2 section below; deferred from the originally planned 2026-05-09 start because Verisign's earlier-than-expected approval landing forced a re-prioritisation.
  - **Today's Wayback HTTP 111 outage is not a bug to fix.** External degradation was handled correctly by the package + breaker. If it recurs daily, evaluate whether to add a degraded-mode fallback (e.g., publish without Wayback signal but at lower confidence tier) — not before.

---

## Wave 2 — Common Crawl backlink integration (planned, not started)

**Status:** scoped, not started. Trigger to start: post-Verisign-rollout stability for 2–3 days (i.e., 2026-05-10 through 2026-05-12 crons should produce no Verisign-related ban events or new bottlenecks). Listed already in [PLAN.md](PLAN.md) Phase 2 scope as a one-line bullet; this section makes the work concrete.

**Motivation.** Wayback and Common Crawl are independent quality signals on orthogonal axes:

- **Wayback (already integrated)** answers *"did this site exist over time?"* — temporal evidence of a site that was actually crawled and archived.
- **Common Crawl host-graph (planned)** answers *"did other sites think this site mattered?"* — link evidence of a site that other webmasters chose to point at.

Two independent signals combine into a more credible "real domain that lapsed" score than either alone. Specifically, a name with high Wayback **and** non-trivial inbound-host count is much harder to fake than a name with just one signal. This week's published cohort surfaced the first OpenPageRank-positive Promising candidates (`collegelabor.org`, `thereedgroup.org` on 2026-05-08) — the kind of authority signal Common Crawl would corroborate independently and at higher resolution than OpenPageRank's coarse 0–10 bucketing.

**Implementation shape (NOT detailed engineering — scope only):**

- Quarterly download of Common Crawl's host-graph edges file (~50 GB, free at `data.commoncrawl.org/projects/hyperlinkgraph/...`) into Cloudflare R2 (the same R2 bucket already used for zone-snapshot state, so no new infrastructure).
- Convert downloaded WAT/WARC slices to **Parquet** (columnar, compressed) for efficient point-lookup queries via **DuckDB** over R2 range reads. DuckDB can query Parquet files remotely without downloading the whole file each time — same query model used by other "host-graph as a public dataset" tooling.
- New enrichment field `cc_inbound_hosts: int` produced by `scripts/enrichment/common_crawl.py`, alongside the existing `wayback_snapshots`. Plugin contract preserved (uniform `enrich(domain, config) → dict`, returns empty dict on failure, never raises).
- New scoring term in `scripts/score.py` using `cc_inbound_hosts` with a tunable weight in `scripts/config.json`. Same null-aware normalisation as existing components (commit `7a43190`'s scoring fix) so a domain with missing CC data scores on what's actually populated.
- Quarterly refresh script (`scripts/refresh_commoncrawl.py`) — manual or scheduled, ~30 min runtime once per quarter. Does NOT live on the daily cron path; quarterly refresh is the right cadence because Common Crawl publishes monthly and the host-graph topology shifts slowly.

**Why now is the right time to consider it.**

1. The pipeline is stable enough this week to build on. Pre-2026-05-04 we'd have been adding signal to a flaky base.
2. Pre-Verisign approval is the right window. When `.com` / `.net` land (anticipated 1–3 weeks per the existing 2026-05-03 STATE entry), candidate volume jumps ~10× and the marginal value of an independent quality signal is much higher at scale than at today's 14k-survivor baseline. We want CC integrated before that volume change, not after.
3. No new infrastructure required: R2 bucket exists, DuckDB ships as a `pip` dependency (~25 MB), Parquet is well-supported.

**Estimated scope: 4–5 hours of focused work spread over a day.** Not a multi-day project. Concrete deliverable list:

- `scripts/refresh_commoncrawl.py` — download + convert + upload to R2 (~1.5h, mostly waiting for the download)
- `scripts/enrichment/common_crawl.py` — DuckDB point-lookup wrapper, plugin-contract-compliant (~1h)
- `scripts/score.py` — new term + config wiring (~0.5h)
- `tests/enrichment/test_common_crawl.py` — mock the DuckDB connection, verify counts flow through (~1h)
- Update `enrichment/__init__.py` order, frontend column if/when we surface it (~0.5h)
- Buffer for unknown unknowns (~0.5h)

### Architecture refinements (2026-05-09 design review)

Two refinements surfaced in tonight's design review that the original Wave 2 spec above did not capture. Both are additive — the enricher / score-function / quarterly-refresh shapes above remain correct, just with more nuance in the field set and pipeline ordering.

**Three-state output, not a single integer.** The original spec described `cc_inbound_hosts: int` as the only enrichment field. That conflates two semantically different "zero" cases:

1. **Domain not in CC host graph at all** — no data available, score must treat as missing (null component, falls out of normalisation).
2. **Domain in CC graph, zero inbound but has outbound** — real "no one linked to this" signal, score = 0.
3. **Domain in CC graph with N inbound** — positive quality signal, score scales with N.

The CC host-graph file is a list of edges (`from_host TAB to_host`). Distinguishing State 1 from State 2 requires checking whether the domain appears as either endpoint of any edge, not just counting inbound edges. The enricher must therefore query both directions and surface three fields:

- `cc_seen_in_graph: bool` — appears as source or target of any edge
- `cc_inbound_hosts: int` — distinct hosts linking to this domain (target-side count)
- `cc_outbound_hosts: int` — distinct hosts this domain links to (source-side count)

State 1 will be the common case for our candidate set. CC undersamples newer / smaller / long-tail sites — exactly the lapsed-small-business profile this project exists to surface. Treating CC absence as "score zero" would systematically bias against the very candidates we care about.

**Score function logic, three-way branch:**

```python
if not cc_seen_in_graph:
    cc_signal = None  # missing data, score on what is populated
elif cc_inbound_hosts == 0 and cc_outbound_hosts > 0:
    cc_signal = 0  # CC saw this domain, found no inbound — real negative
elif cc_inbound_hosts > 0:
    cc_signal = scaled(cc_inbound_hosts)  # real positive signal
```

The score function already handles missing-data cases correctly for OpenPageRank (null-aware normalisation per commit `7a43190`'s scoring fix). Same pattern applies here.

**CC as priority queue, not filter.** A natural extension once CC data is flowing: use it to **order** candidates entering the (slow, externally-degradable) Wayback enrichment phase. This converts CC's value from "additional signal" to **resilience to Wayback outages** — when Wayback degrades mid-run (as on 2026-05-08 and again on 2026-05-09), the cuts hit lower-priority candidates that were less likely to score well anyway.

Priority order for the enrichment phase:

1. **First priority — `cc_inbound_hosts > 0`.** Real positive CC signal. Highest-confidence "domain that mattered" candidates; we want Wayback data on them before the budget can run out.
2. **Second priority — `cc_seen_in_graph == false`.** No CC data either direction. CC undersamples the long tail; absence is uninformative, so we still need Wayback to tell us anything about these candidates.
3. **Third priority — `cc_seen_in_graph == true and cc_inbound_hosts == 0 and cc_outbound_hosts > 0`.** CC positively saw this domain and found no inbound links. Weakest of the three states; if budget binds, these are the ones we'd skip first.

**Hard rule: never discard, only deprioritise.** Every RDAP-available candidate still enters the enrichment phase. The CC priority just orders them. If enrichment budget binds, lower-priority candidates may not get Wayback signal — but they're still in the candidate set with whatever signals they did get (RDAP, blocklists, OpenPageRank if reachable).

**Sequencing (when work starts):**

- **Phase 1 (~4 hours)** — three-state CC enricher + score-function update. CC runs as a parallel enricher in the existing enrichment phase ordering. No pipeline-shape changes; just one more enricher writing three fields.
- **Phase 2 (~1 hour, 1–2 weeks after Phase 1)** — convert CC to a pre-enrichment priority queue once we have empirical data showing the actual three-state distribution across our candidates. Wait until we have a week of CC data before changing pipeline ordering — premature priority-queue could mask whether the CC signal actually correlates with publish-worthiness.

Total scope estimate now ~5–6 hours (was 4–5 in the original spec above): three-state output adds ~30 min vs single-integer output, priority-queue wiring adds another ~30 min on top of the original `enrichment/__init__.py` ordering work.

**NOT acted on yet.** This is a "next week if this week's stability holds" item. Adding it here so future sessions can resume it without re-discovering the scope.