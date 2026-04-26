# DomainSifter — Current State

Last updated: April 26, 2026 (V1 PIPELINE COMPLETE — ready for first manual run)

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
- scripts/diff.py: BUILT — `load_yesterday`, `compute_drops`, `commit_today`, `diff_and_commit`. State files at `{state_dir}/{tld}_yesterday.txt`, sorted, one per line. Cold start returns empty drops and writes today's snapshot for tomorrow.
- tests/test_diff.py: BUILT — 12 tests, all passing. Covers cold start, warm run, sorted output, dir creation, roundtrip, blank-line tolerance, TLD-case normalization.
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
- scripts/env_check.py: BUILT — `validate_env()` raises `MissingEnvVarsError` listing every missing required var (CZDS_USERNAME, CZDS_PASSWORD, SAFE_BROWSING_KEY). OPENPAGERANK_KEY is optional → warning. Empty strings count as missing. Pipeline.py will call this first thing.
- tests/test_env_check.py: BUILT — 7 tests, all passing.
- scripts/enrichment/spam_check.py (UPDATED): missing `SAFE_BROWSING_KEY` now raises `SpamCheckConfigError` (was: silent empty dict). Per-domain network/5xx still returns empty dict — those are transient. Reasoning: spam_check is a CORE filter rule; degraded malware filtering is worse than no daily run. env_check is the first line of defence; the raise here is defence-in-depth.
- scripts/filter.py: BUILT — `keep(candidate, config, *, strict_spam_check=True)` returns `(bool, reason|None)`. Rules: punycode, length, all-numeric, keyword, spam_flagged, surbl_listed, spamhaus_listed, min wayback (only enforced when field present). `strict_spam_check=True` rejects when spam_flagged field is missing. `filter_candidates(...)` logs per-reason rejection counts.
- tests/test_filter.py: BUILT — 16 tests, all passing.
- scripts/score.py: BUILT — Composite weighted score in [0, 100]. wayback log-scaled, OPR linear /10, cert boolean, length inverted (shorter = better). Missing fields = 0 contribution (not a penalty). `score_candidates(...)` mutates in-place adds `score` field, sorts desc by score then asc by name for determinism.
- tests/test_score.py: BUILT — 11 tests, all passing.
- scripts/output.py: BUILT — `build_payload(...)` projects to the locked PLAN.md Principle 5 contract (no internal fields leak), caps at `max_candidates_per_day`, applies `affiliate_link_template`. `write_output(...)` writes atomically via tempfile + os.replace so Cloudflare Pages never serves a partial file. Cleans up temp on error.
- tests/test_output.py: BUILT — 12 tests, all passing.
- scripts/pipeline.py: BUILT — Orchestrator. Order: `env_check.validate_env()` → CZDS auth → list links → per-TLD download/parse/diff/commit (tempdir + per-zone failure tolerance) → `ThreadPoolExecutor` enrichment with `max_workers = config.max_concurrent_enrichments` (each candidate runs all 7 sources sequentially within one worker; candidates run in parallel across the pool) → filter (strict_spam_check=True) → score → atomic write. Per-enricher exceptions logged & continued; `SpamCheckConfigError` re-raised to abort run. CLI: `python scripts/pipeline.py [--config path] [--output path]`.
- tests/test_pipeline.py: BUILT — 9 tests, all passing.
- .github/workflows/daily-diff.yml: BUILT — Triggers `cron: "0 6 * * *"` + `workflow_dispatch`. `permissions: contents: write`. Concurrency group prevents overlap. Steps: checkout → setup-python 3.11 (pip cache) → install requirements.txt → run pipeline (secrets passed as env) → commit `scripts/state/` and `src/data/daily-domains.json` as `github-actions[bot]` with message `data: daily refresh YYYY-MM-DD`. Skips commit when nothing changed.
- scripts/state/.gitkeep: BUILT.
- Full suite: 135/135 passing.
- requirements.txt + requirements-dev.txt: BUILT
- .gitignore: BUILT — blocks *.zone, *.zone.gz, .env, __pycache__, .venv

### External API keys needed

- OpenPageRank API key (free, 10k/day): NOT YET SIGNED UP — sign up at https://www.domcop.com/openpagerank/
- Google Safe Browsing API key (free, 10k/day): NOT YET SIGNED UP — Google Cloud Console → enable Safe Browsing API
- Both keys must be added to GitHub Secrets as OPENPAGERANK_KEY and SAFE_BROWSING_KEY
- CZDS credentials must be added to GitHub Secrets as CZDS_USERNAME and CZDS_PASSWORD

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

### Step 2 — Add four GitHub Actions secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add these four (names are case-sensitive):

- `CZDS_USERNAME`
- `CZDS_PASSWORD`
- `SAFE_BROWSING_KEY`
- `OPENPAGERANK_KEY`

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
INFO scripts.pipeline .app: NNNNNN in zone today, 0 dropped since yesterday  (×11 — "0 dropped" is correct on day 1)
INFO scripts.diff Wrote NNNNNN names to scripts/state/app_yesterday.txt     (×11)
INFO scripts.pipeline Collected 0 total drops across 11 TLDs                (day 1 only — day 2+ will be hundreds to thousands)
INFO scripts.pipeline Enriching 0 candidates with 10 concurrent workers     (day 1 only)
INFO scripts.filter Filter kept 0 / 0 candidates                            (day 1 only)
INFO scripts.output Wrote 0 domains to src/data/daily-domains.json (generated_at=2026-MM-DDTHH:MM:SSZ)
INFO scripts.pipeline Pipeline complete: 0 domains in output                (day 1 only)
```

Final workflow steps:

```
Commit refreshed state and output → "data: daily refresh YYYY-MM-DD"
[main XXXXXXX] data: daily refresh 2026-MM-DD
 12 files changed, ...
```

If you see "No changes to commit", that's a problem on day 1 — the
state files should be brand new. Check whether `scripts/state/` was
committed empty earlier or whether the pipeline crashed before writing.

### Step 6 — Verify the bot commit landed

Refresh the repo's main page on github.com. You should see:

- Latest commit: `data: daily refresh YYYY-MM-DD` by `github-actions[bot]`
- `scripts/state/` now contains 11 new files: `app_yesterday.txt`, `dev_yesterday.txt`, etc.
- `src/data/daily-domains.json` exists (0 domains on day 1, hundreds on day 2+)

### Step 7 — Verify Cloudflare Pages auto-rebuilds

The bot commit on main should trigger Cloudflare Pages. Within ~1–2 min:

1. Cloudflare dashboard → Pages → domainsifter → Deployments shows a new build
2. https://domainsifter.com loads with the fresh JSON (0 domains shown on day 1)

### Step 8 — Wait for the cron

Cron is already set to `0 6 * * *` (06:00 UTC daily) in
`.github/workflows/daily-diff.yml`. No further action needed. The next
06:00 UTC tick produces the first real list using day 1's seeded snapshots
as the "yesterday" baseline.

### Common failure modes

| Symptom in log | Cause | Fix |
|---|---|---|
| `MissingEnvVarsError: ...` | A repo secret is missing or empty | Re-check Settings → Secrets, names are case-sensitive |
| `CzdsAuthError: HTTP 401` | Wrong CZDS username/password | Verify in https://czds.icann.org login, update secret |
| `CzdsAuthError: HTTP 403 ... must accept terms` | One or more zones have ToS pending | Log into czds.icann.org, accept any pending T&Cs |
| `SpamCheckConfigError` mid-run | `SAFE_BROWSING_KEY` set but invalid | Re-generate key in Google Cloud Console; whitelist the API |
| Workflow fails at "Commit refreshed state" with `Permission denied` | `permissions: contents: write` not honored | Repo Settings → Actions → General → Workflow permissions: select "Read and write permissions" |
| `No changes to commit` on day 1 | Pipeline crashed before writing state | Read earlier log lines for the actual error |

---

## Total cost to date

26 EUR (domain registration only). Everything else: free.