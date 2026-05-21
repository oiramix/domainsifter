# DomainSifter Operations Recovery Runbook

For the operator (Mario, or future Claude session) when something has
gone wrong with the daily pipeline. Read this end-to-end the first
time; then jump to the relevant scenario.

This document is paired with the incident that produced it on
2026-05-21 (see "Incident: 2026-05-21 catch-up corruption attempt"
at the end). Read that section if you're not sure why the same-day
guard exists.

---

## Quick reference

| If you see this... | Do this |
|---|---|
| `systemctl list-timers \| grep domainsifter` shows `NEXT: -` | The pipeline unit is stuck in `active (exited)` or the timer has been stopped. See "Timer not firing." |
| Today's 06:30 UTC cron didn't run by 07:30 UTC | See "Today's cron didn't fire." |
| Pipeline ran but `git push` failed (FAILED email but R2 state OK) | See "Pipeline succeeded, push failed." |
| You want to re-run the pipeline TODAY after fixing a config bug | `sudo systemctl start domainsifter-force.service` |
| `daily-domains.json` looks wrong / has bad data on production | See "Bad data published." |
| The archive chain didn't fire | See "Archive didn't run." |
| You manually stopped the pipeline mid-run | See "Manual stop mid-run." |
| Server was down all of 06:30 UTC | See "Server downtime." |

---

## Mental model: what state lives where

| State | Location | Lifecycle |
|---|---|---|
| `*_yesterday.txt` per-TLD baselines | Cloudflare R2 `state/<tld>_yesterday.txt` | Overwritten with TODAY's snapshot at the end of each successful per-TLD diff. The name "yesterday" is historical — at any point in time, the file contains the most recent successful baseline. |
| Published daily list | `src/data/daily-domains.json` on `origin/main`, served by Cloudflare Pages | Rewritten and pushed by run-daily.sh at the end of each successful pipeline run. |
| Archive index | `src/data/archive-index.json` on `origin/main` | Appended-to by domainsifter-archive.service after each pipeline run. Used to dedupe — same domain never gets two .md pages. |
| Archive pages | `src/content/archive/<name>.md` on `origin/main` | One per Clean/Promising domain. Never modified after creation. |
| Sidecar excerpts | `src/data/wayback_excerpts.json` on `origin/main` | Merged sidecar, written by Phase 4 classifier (pipeline.py Stage 4b). |
| Same-day sentinel | `~/.local/state/domainsifter/last-success.date` on OVH (NOT in repo, NOT in R2) | Single line, UTC date. Written by run-daily-with-guard.sh after every invocation. |

**Critical mental-model correction:** `*_yesterday.txt` does NOT contain yesterday's snapshot in the calendar sense. It contains the most-recent-successful-pipeline-run's snapshot, which on any given day is today's snapshot (after the morning run completes). Tomorrow's run treats this as "yesterday" — that's the entire point of the name. Do NOT panic-restore these files thinking the morning run "corrupted" them; that's normal lifecycle.

---

## The same-day guard

`scripts/run-daily-with-guard.sh` (invoked by `systemd/domainsifter.service` as ExecStart=) checks the sentinel before invoking `scripts/run-daily.sh`. If the sentinel's date matches today (UTC), the wrapper exits 0 immediately without invoking run-daily.sh.

The sentinel is written **unconditionally** after invoking run-daily.sh (regardless of its exit code), because pipeline.py mutates R2 state mid-run. A failed pipeline that got partway through has already corrupted the baseline for some TLDs. An automatic same-day retry on partial state would compound the inconsistency. Manual retries via `domainsifter-force.service` are always available and require an explicit operator decision.

### Bypass

```
sudo systemctl start domainsifter-force.service
```

The force unit is identical to `domainsifter.service` except it sets `DOMAINSIFTER_FORCE_RUN=1`, which makes the wrapper skip the sentinel check. The force unit still chains the archive on success.

### What the wrapper does NOT block

- Manual invocation of `scripts/run-daily.sh` directly (bypasses the wrapper entirely; expected for development).
- Manual invocation of `python -m scripts.pipeline` (bypasses both wrapper and run-daily.sh; expected for development).
- `domainsifter-force.service` (the force unit).

The guard is for the SCHEDULED path only. Manual interventions are assumed to be considered.

---

## Scenario: "Today's cron didn't fire"

By 07:30 UTC there should already be:
- An email at hello@domainsifter.com with the daily report
- A new commit on `origin/main` (or skip-commit if no changes)
- `systemctl list-timers` showing `NEXT: <tomorrow 06:30 UTC>` and `LAST: <today 06:30 UTC>`

If you don't see those:

### Step 1 — Confirm the diagnosis

```bash
systemctl list-timers | grep domainsifter
systemctl status domainsifter.service
systemctl status domainsifter-archive.service
journalctl -u domainsifter.service --since today
```

Common patterns:

| `list-timers` shows | `status` shows | Likely cause |
|---|---|---|
| `NEXT: -` | `Active: active (exited)` | Pipeline is stuck active (RemainAfterExit relic, or stuck-in-flight run). |
| `NEXT: <tomorrow>` | `Active: failed` | Pipeline ran and failed. Check journalctl for the error. |
| `NEXT: <tomorrow>` | `Active: inactive (dead) since today 06:30` | Pipeline ran SUCCESSFULLY at 06:30 — you may just not have noticed. Check email. |
| No domainsifter timer listed | n/a | Timer is disabled. Re-enable: `sudo systemctl enable --now domainsifter.timer`. |

### Step 2 — Decide whether to run today or wait

**If the diagnosis is "ran successfully":** nothing to do. Tomorrow's 06:30 UTC fire is the next event.

**If the diagnosis is "ran and failed":** read journalctl to find the error. Common failures:
- CZDS auth: token expired or rate-limited
- R2 transport: network blip
- Git push: rebase needed (someone pushed during the run)

After identifying and fixing the root cause:

```bash
# If pipeline.py NEVER reached "Wrote N domains to ..." line:
#   R2 may be partially mutated. The morning sentinel was already
#   written (unconditional). To retry today, use force:
sudo systemctl start domainsifter-force.service

# If pipeline.py succeeded but git push failed (the 2026-05-21 case):
#   R2 is fine. daily-domains.json was written locally on OVH but
#   not pushed. Manual recovery:
sudo -u domainsifter -i
cd /home/domainsifter/domainsifter
git fetch origin main
git rebase origin/main
git push https://x-access-token:$(grep GITHUB_TOKEN .env | cut -d= -f2)@github.com/oiramix/domainsifter.git main
# DO NOT then start domainsifter.service — it would be a same-day
# auto-retry. The sentinel already blocks it; do not force.
```

**If the diagnosis is "stuck active":**

```bash
sudo systemctl stop domainsifter.service
# After stop completes:
systemctl status domainsifter.service  # expect: inactive (dead)
systemctl list-timers | grep domainsifter  # expect: NEXT: <next 06:30 UTC>
```

Do NOT restart the timer (`systemctl start domainsifter.timer`) after a stop. The timer is already enabled and will fire at the next scheduled time. **Starting the timer when it's been stopped during the day will fire any missed catch-up immediately.** The same-day guard blocks this from corrupting R2, but the unnecessary fire still consumes API budget and wall-clock.

If for some reason the timer was disabled and you need to re-enable it mid-day, the guard will block the catch-up:

```bash
sudo systemctl enable --now domainsifter.timer
# Wrapper will run, see today's sentinel, log "Skipping," exit 0.
# Tomorrow's 06:30 UTC fire works normally.
```

---

## Scenario: "Pipeline succeeded, push failed"

This is the classic 2026-05-21 morning failure mode. Detect it by checking the email report: it'll show pipeline-internal lines completing (CZDS, enrichment, score, output) followed by a git push failure.

```bash
sudo -u domainsifter -i
cd /home/domainsifter/domainsifter

# Pull latest origin/main:
git fetch origin main

# Inspect: is the local branch ahead of origin?
git log --oneline origin/main..HEAD

# If yes, rebase the local commit onto current origin:
git rebase origin/main

# Push using the token from .env (do not echo it):
git push "https://x-access-token:$(grep ^GITHUB_TOKEN= .env | cut -d= -f2)@github.com/oiramix/domainsifter.git" main
```

After successful push, **do not** trigger another pipeline run today. The sentinel (written by the failed run's wrapper) already blocks scheduled retries. R2 state is correct. daily-domains.json is now on origin/main and Cloudflare Pages will rebuild. Done.

---

## Scenario: "Bad data published" (need to republish today)

Symptoms: today's `daily-domains.json` on origin/main contains visibly bad data — wrong verdict assignments, missing domains that should be there, the wrong TLD list, etc. Root cause is in code/config, not infrastructure.

```bash
# 1. Fix the bug in code or config on a feature branch (or hotfix on main).
#    Push to origin/main.
#
# 2. SSH to OVH:
sudo -u domainsifter -i
cd /home/domainsifter/domainsifter
git fetch origin main
git rebase origin/main    # sync OVH to latest origin
exit

# 3. Force a re-run (bypasses same-day guard):
sudo systemctl start domainsifter-force.service

# 4. Monitor:
journalctl -u domainsifter-force.service -f
```

The force run will:
- Re-download all approved zones (idempotent)
- Compute drops = baseline(today_morning) − today_now (essentially empty, since zones don't change intra-day)
- Re-publish daily-domains.json with the fixed code/config applied
- Re-write sentinel = today
- Chain the archive service (which dedupes via archive-index.json and is a no-op if today's pages already exist)

**Caveat:** drops in the force-run will be a near-empty set because the baseline IS today's snapshot from the morning run. New "drops" only get picked up if a domain actually got removed from the zone between morning and now. Carryover handling means yesterday's still-available list survives in `daily-domains.json` regardless. Force-run for a CODE bug fix is fine; force-run because "I expected more domains today" is not — there's nothing to discover that the morning run didn't already discover.

---

## Scenario: "Archive didn't run"

After a successful pipeline run, `domainsifter-archive.service` should fire via `OnSuccess=`. If it didn't:

```bash
systemctl status domainsifter-archive.service
journalctl -u domainsifter-archive.service --since today
```

Common patterns:

| Status | Cause |
|---|---|
| `inactive (dead), no recent runs` | OnSuccess= didn't fire. Check that `domainsifter.service` actually completed successfully (not stuck active). |
| `failed`, Anthropic 5xx errors in journal | Anthropic API was overloaded. The archive will self-heal tomorrow — `_filter_qualifying` has no date filter, it iterates all Clean/Promising not yet in archive-index.json. |
| `failed`, "ANTHROPIC_API_KEY missing" | `.env` file issue. Check `/home/domainsifter/domainsifter/.env` has the key set and EnvironmentFile= can read it. |
| `inactive (dead)`, "No new qualifying domains" in journal | Clean run, nothing to do. Today's list had zero Clean/Promising entries that weren't already archived. |

To manually retry the archive after a transient failure:

```bash
sudo systemctl start domainsifter-archive.service
```

This is safe — the archive is idempotent. The same-day guard does NOT apply to the archive service (only the pipeline). Run it as many times as you need.

---

## Scenario: "Manual stop mid-run"

If you've stopped the pipeline (`systemctl stop domainsifter.service`) while it was in flight:

1. The pipeline subprocess was killed. R2 state may be partially mutated (some TLDs have today's snapshot in their yesterday.txt, others don't).
2. The wrapper's sentinel write happens AFTER run-daily.sh exits, so the sentinel may or may not have been written depending on where in the script the SIGTERM landed. Check:
   ```bash
   cat /home/domainsifter/.local/state/domainsifter/last-success.date
   ```
3. If sentinel == today: scheduled retries blocked, you can force a retry if desired.
4. If sentinel != today (or missing): scheduled retries will proceed at next 06:30 UTC. Today's automatic retry will NOT happen.

In either case, R2 is in a partial state. Tomorrow's 06:30 UTC run will see baselines = mixed-time-of-day snapshots for the TLDs that got processed before the stop. Diff math is still correct (each TLD's "yesterday" baseline → "today" parse is consistent); just slightly different intra-day delta from a normal run.

If you want a clean today's run, force it:

```bash
sudo systemctl start domainsifter-force.service
```

---

## Scenario: "Server downtime"

If OVH was unreachable across all of 06:30 UTC and came back up later in the day, `Persistent=true` on the timer means systemd will fire the missed schedule immediately on the next boot (or when the timer is next started). The same-day guard then decides:

- If no successful run had completed today before the downtime: sentinel is from a previous day → wrapper allows the catch-up → pipeline runs.
- If a successful run completed today before the downtime: sentinel = today → wrapper skips.

This is the legitimate use case for `Persistent=true`. The guard makes it safe.

---

## Validating the same-day guard (without corrupting state)

Use this when you want to confirm the wrapper is wired up correctly without actually triggering a pipeline run:

```bash
sudo -u domainsifter -i
mkdir -p ~/.local/state/domainsifter
echo "$(date -u +%Y-%m-%d)" > ~/.local/state/domainsifter/last-success.date
exit

sudo systemctl start domainsifter.service
journalctl -u domainsifter.service --since "1 minute ago"
# Expect: "same-day guard: a run already completed today ... Skipping."
# Expect: service status = inactive (dead)
# Expect: NO new commit on origin/main, NO R2 changes

# Cleanup (so tomorrow's cron works):
sudo -u domainsifter rm ~/.local/state/domainsifter/last-success.date
```

After this, the next 06:30 UTC fire will run normally (no sentinel for tomorrow's date until tomorrow's run completes).

---

## Incident: 2026-05-21 catch-up corruption attempt

This section exists so future operators understand WHY the guard architecture is what it is.

**Timeline:**

| UTC | Event |
|---|---|
| 06:30 | Scheduled cron fired the timer's start job. Pipeline service was already in `active (exited)` state from yesterday (RemainAfterExit=yes had not yet been removed). systemd's start-job deduplication treated the queued start as already-satisfied. **No pipeline ran.** No email. `systemctl list-timers` showed `NEXT: -`. |
| ~10:00 | Operator noticed missing email. Diagnosed RemainAfterExit interaction. |
| 10:12 | Manual recovery: `systemctl stop domainsifter.service` (transitioned to inactive); `systemctl start domainsifter.service` (re-fired). |
| 11:13 | Pipeline.py completed for all 11 approved TLDs. R2 baselines updated to 2026-05-21 snapshot for every TLD. `daily-domains.json` written locally. Git push attempted. |
| 11:13 | Git push REJECTED: `origin/main` had moved (commit `623b947` landed during the run — the OnSuccess= fix). run-daily.sh exited non-zero. Email reported FAILED. |
| 11:30 | Operator manually rebased local commit onto origin/main, pushed as `1c4783f`. daily-domains.json published correctly. |
| 11:35 | OnSuccess= fix from `623b947` deployed via `deploy_systemd.sh`. |
| 11:40 | **Disaster.** Operator (on Claude's recommendation) ran `sudo systemctl start domainsifter.timer` intending to bring the timer back online. systemd interpreted the missed 06:30 UTC firing (with `Persistent=true`) as a queued event and immediately ran a **catch-up pipeline invocation** as a SECOND same-day run. |
| 11:43 | Operator killed the catch-up. Six TLDs had their R2 baselines overwritten with T_noon snapshots (.app, .studio, .tech, .shop, .org, .store). |

**Post-incident analysis:**

The R2 state was not catastrophically corrupted — intra-day registry-zone churn over 27 minutes is essentially zero, so T_morning and T_noon were functionally identical for diff purposes. Tomorrow's diff would compute drops correctly regardless of which snapshot is the baseline.

But the architecture had allowed a class of failure that, with worse timing or more processing, could have:
- Re-run enrichment and re-written `daily-domains.json` with stale carryover semantics
- Triggered a second archive run, potentially racing on git push
- Corrupted R2 state with a partial second-pass overwrite if a TLD had genuinely churned in the gap

**The fix:**

1. **OnSuccess=** (commit `623b947`) replaced the ExecStartPost + Requires= + RemainAfterExit chain with the proper systemd directive. Pipeline service no longer stays artificially active.
2. **Same-day guard** (this commit) blocks all automatic same-day re-invocations of the pipeline. `Persistent=true` is preserved on the timer for legitimate downtime recovery — the guard is what prevents the same-day catch-up footgun.
3. **Force unit** provides an explicit operator-initiated path for legitimate same-day re-runs. The act of typing `sudo systemctl start domainsifter-force.service` is the operator's affirmation that they understand they're overriding the guard.

**Lessons captured here:**

- "yesterday.txt" is a misnomer — the file contains the last-successful-baseline, not "yesterday's data." After this morning's run, every TLD's "yesterday.txt" contains today's snapshot. Future operators must not panic-restore.
- `Persistent=true` is correct for the downtime-recovery use case but dangerous without a guard. Never restart the timer during the day; rely on the timer's own scheduling.
- `put_object` on R2 is atomic; partial-write corruption is not a failure mode to plan for.
- Cross-unit triggers should be done via `OnSuccess=` (PID 1, internal) rather than `ExecStartPost=systemctl start` (user-space, polkit, fragile).
