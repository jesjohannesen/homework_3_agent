# HW3 Submission — FeynBro

## 1. Forum Links

**Discussion forum:** https://canvas.mit.edu/courses/40577/discussion_topics/448963

**Agent posts (verified, autonomous):**

| Canvas Entry ID | Type | Parent | Cycle |
|---|---|---|---|
| 229542 | Reply | — | 3 (posted), 4 (reconciled) |
| 229543 | Reply | 229028 | 4 |
| 229551 | Reply | 229546 | 6 (fault-injected) |
| 229553 | Reply | 229550 | 6 (fault-injected) |

## 2. Code

**Repo:** https://github.com/jesjohannesen/homework_3_agent

**Setup:**
```bash
git clone https://github.com/jesjohannesen/homework_3_agent && cd homework_3_agent
# Copy .env.example to .env and add CANVAS_TOKEN, OPENROUTER_API_KEY
cp .env.example .env && chmod 600 .env
python3 -m agent doctor          # verify Canvas auth, find forum
python3 -m agent run --dry-run   # test without posting
python3 -m agent run             # one real cycle
```

Stdlib-only Python 3.11 — nothing to install. Schedule via cron: `0 */3 * * * /path/to/scripts/run_cycle.sh`.

## 3. Architecture & Autonomy

The agent runs fully unattended, cycling every ~3h. No human prompt needed after setup.

| Concern | Where | How |
|---|---|---|
| Scheduler | cron / `agent/scheduler.py` | Every ~3h (+/-10% jitter), file lock prevents overlapping cycles |
| Canvas access | `agent/canvas.py` | Bearer token from env only; reads retry with exponential backoff + jitter; token never sent to non-Canvas hosts; redacted from all logs |
| Decision logic | `agent/decide.py` | Deterministic gates first (rate budget, "anything new?") → LLM proposes JSON → deterministic validator disposes. No new entries + no reason to start a thread = **no LLM call, no post** |
| Persistent memory | `agent/memory.py` | SQLite `state/agent.db`: entries seen/handled/own, write-ahead action intent log, cycle history, failure counter |
| Idempotency | `memory.begin_action` | Write-ahead intent with `UNIQUE(kind, parent, normalized body)`; one reply per parent ever; similarity check vs. earlier posts |
| Verification | `Agent._verify` | Re-reads forum and confirms post by user ID + text exists before marking `verified` |
| Rate limits | `config.py`, `cycle.py` | ≤2 posts/cycle, ≤2/hour (hard cap 3), ≤8/day, new threads ≤1 per 12h; re-checked at write time |
| Control line | `safety.control_state` | Topic re-fetched before **every** write; only exact first line `COURSE-TEAM CONTROL: RUNNING` allows writes (PAUSED/missing/malformed → read-only) |
| Stopping rule | `memory.bump_failure` | 3 consecutive failed cycles → `halted`; cycles refuse to run until `python3 -m agent unhalt` |
| Prompt-injection defense | `persona.py`, `safety.py` | Forum text wrapped as untrusted data; LLM has **no tools** (text in, JSON out); only two write paths to one hard-coded topic; suspicious entries flagged; outputs with links/emails/key-shaped strings/secrets rejected |
| Blast radius | — | One token, one topic, no shell/file tools for model, no edit/delete of others' posts |

## 4. Supporting Activity Evidence (Scheduled Runs)

Status at submission time:
```
halted: no | consecutive failures: 0
actions[verified]: 4
cycle   6 2026-10-03 16:47:56 posted 2 verified post(s)
cycle   5 2026-10-03 16:00:01 no_post rate_limited       ← deliberate silence
cycle   4 2026-10-03 15:05:13 posted 1 verified post(s)
cycle   3 2026-10-03 15:04:24 error (reconciliation)
cycle   2 2026-10-03 15:03:06 dry_run
cycle   1 2026-10-03 14:58:31 error (setup)
```

**Cycle 5 — deliberate no_post (rate_limited):**
```json
{"ts": 1791043202.7, "cycle": 5, "event": "control_line", "state": "RUNNING"}
{"ts": 1791043203.5, "cycle": 5, "event": "no_post", "reason": "rate_limited"}
{"ts": 1791043203.5, "cycle": 5, "event": "cycle_end", "outcome": "no_post", "detail": "rate_limited"}
```

The agent correctly stayed silent because it had already posted within the hour (2 posts in cycle 4, 2/hour cap).

**Cycle 4 — autonomous post + reconciliation:**
```json
{"ts": 1791039914.0, "cycle": 4, "event": "reconciled_found", "action": 1, "canvas_id": "229542"}
{"ts": 1791039919.8, "cycle": 4, "event": "llm_decision", "shown": 17, "proposed": 1, ...}
{"ts": 1791039923.6, "cycle": 4, "event": "post_verified", "kind": "reply", "canvas_id": "229543", "parent": "229028"}
```

Cycle 4 first reconciled the previous cycle's post (229542) that had a verification-timing issue, then autonomously decided to reply to entry 229028.

## 5. Failure & Recovery Evidence

**Fault injected:** `HW3_FAULT=lost_ack` — Canvas acknowledged the write but the response was dropped before the agent could read it.

```json
{"ts": 1791046076.4, "event": "FAULT_INJECTION_ARMED", "mode": "lost_ack"}
{"ts": 1791046091.2, "cycle": 6, "event": "llm_decision", "shown": 2, "proposed": 2, ...}
{"ts": 1791046092.0, "cycle": 6, "event": "write_ambiguous", "why": "injected: response lost after write applied", "attempt": 1}
{"ts": 1791046095.4, "cycle": 6, "event": "post_verified", "kind": "reply", "canvas_id": "229551", "parent": "229546"}
{"ts": 1791046098.0, "cycle": 6, "event": "post_verified", "kind": "reply", "canvas_id": "229553", "parent": "229550"}
```

**What happened:** The fault injector simulated a lost acknowledgement — the POST succeeded (Canvas returned entry IDs) but the response was dropped. The agent recognized the ambiguous failure (`write_ambiguous`), re-read the forum to check if the posts actually landed, found them, and marked them `verified` — zero duplicates.

Full `cycles.jsonl` available in `state/cycles.jsonl` (all secrets already redacted by the log layer).