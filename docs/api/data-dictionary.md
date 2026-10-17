# DomainSifter read-only API: data dictionary

**API version:** 1.0.0. **Base URL:** `https://api.domainsifter.com/v1`. **Contract:** `GET /v1/openapi.json`.

This answers the Asset X-Ray requirements v1.0 (2026-10-16). Where DomainSifter does not hold what the spec asks for, the API says so with `null` plus a status, or `501`, and this document explains why.

## What DomainSifter is, and what it is not

DomainSifter finds domains that disappear from gTLD zone files, confirms with the registry that they are available, and enriches them. It is **not** a scraper of ExpiredDomains.net or any other list site, and it holds no per-link backlink data.

**Domains covered:** every domain DomainSifter confirmed available from 2026-06-03, plus every domain it published between 2026-04-28 and 2026-06-02.

**Data per domain:** one event per day it was confirmed available or listed. Each event becomes several observations. Domains are never deleted.

**Freshness:** the snapshot is rebuilt daily at 15:10 UTC, after the daily run. `GET /v1/health` gives `last_ingest_at`.

**Day precision:** timestamps derived from these events have day precision only: `YYYY-MM-DDT00:00:00Z` means "on that UTC day".

## Sources (`GET /v1/sources`)

`terms_status` and `may_display` are DomainSifter's own reading of each provider's terms, not legal advice. A source whose terms were not reviewed is `unknown` with `may_display: false`.

| id | method | terms_status | may_display | What it provides |
|---|---|---|---|---|
| `icann_czds` | licensed_feed | permitted_with_conditions | false | Domain names that left a gTLD zone between two daily CZDS downloads, and the drop date. See the conditions below. |
| `registry_rdap` | api | unknown | false | Availability: the registry's RDAP server answered 404 (no registration). |
| `internet_archive_wayback` | api | unknown | false | Wayback snapshot count, year of the latest capture, and the latest archived page title (`sample_title`). |
| `common_crawl_webgraph` | licensed_feed | permitted_with_conditions | false | Number of distinct domains linking to the domain in a Common Crawl domain-graph release. |
| `open_page_rank` | api | unknown | false | Open PageRank score, 0 to 10. |
| `crtsh` | api | unknown | false | Whether any TLS certificate was ever logged for the domain. |
| `domainsifter_content_classifier` | api | permitted | true | AI opinion of what the archived site was: legitimate / parked / empty / toxic. |
| `domainsifter_name_ranker` | api | permitted | true | AI opinion of the domain name's brandability, 0 to 100. |

**ICANN CZDS conditions.** Which agreement applies depends on the TLD:
- **.com and .net:** Verisign's own Registry Agreements (.com, 1 Dec 2024; .net, 1 Jul 2023), Appendix 3 (Zone File Access) §1.5.
- **New gTLDs** (.app, .dev, .live, .studio, .tech, .online, .site, .store, .xyz): ICANN base Registry Agreement, Specification 4 §2.1.5.
- **.org, .info and .biz:** not separately verified.

Both texts allow use for lawful purposes, provided that:
- the user takes reasonable steps to protect the zone data against unauthorized access, use and disclosure;
- it is not used for marketing to anyone other than the user's existing customers;
- it is not used for high-volume automated processes that query registry systems;
- it is not used to interfere with registrants.

Neither has an express redistribution or publication clause.

`may_display` is `false` at Asset X-Ray's request (2026-10-17): lists of dropped or new domains derived from zone diffs are not displayed. Serving single-domain lookups is fine. Whether to show a per-domain fact a user asked about, or a watchlist alert, is Asset X-Ray's decision under its own review. This API never serves zone file contents.

## Statuses: unknown is `null`, never `0`

A `0` is always a real zero (for example `open_page_rank: 0.0`, or `wayback.snapshots: 0` with status `none_found`).

| Status | Meaning |
|---|---|
| `ok` | The source answered; `value` is its answer. |
| `none_found` | Wayback only: the lookup succeeded and found no captures. |
| `lookup_failed` | Wayback only: the lookup ran and failed. |
| `not_checked` | No value was recorded. |

**Known ambiguity.** For `referring_domains` and `open_page_rank`, the pipeline did not record why a value is missing. `not_checked` therefore covers three cases:
- never looked up;
- looked up and failed;
- for `referring_domains`, not present in the Common Crawl graph at all.

Treat it as "no information", not as zero.

## Metric names

| name | value | source | Notes |
|---|---|---|---|
| `referring_domains` | integer | `common_crawl_webgraph` | Distinct linking domains in the Common Crawl release current on `observed_at`. Domain level only. |
| `open_page_rank` | number | `open_page_rank` | |
| `cert_history` | boolean | `crtsh` | |

Each metric in `/v1/domains/{domain}` is the newest observation with status `ok`, with its own `observed_at`. If there is none, it is the newest observation of any status.

**Not provided** (no data behind them): `backlinks`, `dmoz_listed` (DMOZ closed in 2017), `prior_registrations`, `archive_birth_year`.

## Domain object (`GET /v1/domains/{domain}`)

**Lookup.** `404` if the domain is not in DomainSifter. Unicode input is converted to punycode.

**Dates.**
- `first_seen_at`: the first day DomainSifter saw the domain.
- `last_seen_at`: the most recent day.

**`availability`**
- `status`:
  - `available`: RDAP-confirmed.
  - `dropped`: zone diff only. This applies to published records before 2026-04-30, when RDAP validation was not yet in place.
  - `pending_delete`, `auction` and `registered` are never returned: DomainSifter does not track them.
- `source`: `registry_rdap` or `icann_czds`.
- `drop_date`: the zone-diff date.

**`wayback`**
- `first_year`: always `null`. DomainSifter stores the snapshot count and the latest capture, not the first.
- `sample_title`: `null` when no archived title was stored.

**`analysis`**
- `null` when the domain was never successfully classified.
- `verdict`: the content classifier's label.
- `spam_signal`: `high` for `toxic`, `low` otherwise.
- `model`: `claude-haiku-4-5`.
- `prompt_version`: the classifier version (`v1`, `v2`, `v3`). It changes whenever the prompt or the rules that can move a label change.
- `niche` and `score`: always `null`.
- `name_quality` (additive): the name ranker's 0–100 score.

All of this is an AI opinion; show it labelled as such.

**Other fields.**
- `published_on_domainsifter` (additive): whether the domain ever appeared on domainsifter.com.

## Other endpoints

**`/v1/domains/{domain}/observations`.** Every observation, oldest first, paginated. Each one has:
- `kind`: `availability`, `metric`, `wayback` or `analysis`;
- `name`: as in the tables above, plus `snapshots` (wayback), `content_category` and `name_quality` (analysis);
- `value`, `status` and `source`.

**`/v1/domains/{domain}/backlinks` and `/referring-domains`.** Always `501`: there's no per-link data, and referring-domain names are not served. The error body's `code` is `not_found`, since the spec's code list has no `not_implemented`; the HTTP status is the signal.

**`/v1/candidates`.**
- Filters: `status`, `tld`, `min_referring_domains` (excludes domains whose count is unknown), `has_wayback`, `first_seen_after`, `updated_after`, `sort`.
- `niche` returns `400`, because niche is not classified.
- `sort=wayback_first_year_asc` is accepted, but `first_year` is always `null`, so it orders by domain name.

**`/v1/changes?since=`.**
- Domains whose `last_seen_at` is after `since`, oldest change first.
- `added` if first seen after `since`, `updated` otherwise.
- `removed` is never emitted, because nothing is deleted.

## Transport

**Auth.** `Authorization: Bearer <token>`.
- Two tokens can be valid at once, for rotation.
- To rotate: the owner adds the new token, restarts the service (about one second), Asset X-Ray switches over, then the old token is removed.

**Rate limit.** 20 requests/s per token, burst 40. Above that you get `429` with `Retry-After`.

**Caching.** Every `200` carries an `ETag` and `Last-Modified` (snapshot build time). `If-None-Match` returns `304`.

**Pagination.**
- `?cursor=&limit=`, default 100, maximum 1000.
- A cursor is bound to the query that produced it.
- After a daily rebuild, offsets can shift by the day's additions. Re-sync via `/v1/changes`.

**Errors.** `{"error": {"code", "message"}}` with codes `not_found`, `bad_request`, `unauthorized`, `rate_limited` and `internal`. `503` (code `internal`) means the snapshot is not loaded yet.

**Change policy.** Breaking changes go to `/v2`; additive fields can appear at any time.
