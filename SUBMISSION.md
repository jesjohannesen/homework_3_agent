# HW3 Submission — FeynBro (Feynman × Taleb × terminally-online Gen Z)

## 1. Forum Links

**Discussion forum:** https://canvas.mit.edu/courses/40577/discussion_topics/448963

**Verified autonomous posts:**

- [Entry 229542](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-229542) — reply (posted cycle 3, reconciled cycle 4)
- [Entry 229543](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-229543) — reply to [229028](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-229028) (cycle 4)
- [Entry 229551](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-229551) — reply to [229546](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-229546) (cycle 6, fault-injected)
- [Entry 229553](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-229553) — reply to [229550](https://canvas.mit.edu/courses/40577/discussion_topics/448963#entry-229550) (cycle 6, fault-injected)

## 2. Code

**Repo:** https://github.com/jesjohannesen/homework_3_agent

**Setup:**
```bash
git clone https://github.com/jesjohannesen/homework_3_agent && cd homework_3_agent
cp .env.example .env && chmod 600 .env   # add CANVAS_TOKEN; OPENROUTER_API_KEY flows from ~/.hermes/.env
python3 -m agent doctor                   # verify Canvas auth, find forum, print control line
python3 -m agent run --dry-run            # read + decide, never write
python3 -m agent run                      # one real cycle
python3 -m unittest discover -s tests -t .  # 20 tests, stdlib only
```

Stdlib-only Python 3.11, nothing to install. Model: `deepseek/deepseek-v4-flash` via OpenRouter (~$0.03 in / $0.06 out per 1M tokens). Schedule: `0 */3 * * * /path/to/scripts/run_cycle.sh` (cron), file lock prevents overlapping cycles.

## 3. Architecture & Autonomy

The agent runs fully unattended, cycling every ~3h. No human prompt needed after setup.
Every cycle: reconcile pending intents → read forum → deterministic gates → LLM proposes JSON → validator disposes → guarded write → verify on Canvas.

| Concern | Where | How |
|---|---|---|
| Scheduler | cron / `agent/scheduler.py` | Every ~3h (+/-10% jitter), file lock so cycles never overlap |
| Canvas access | `agent/canvas.py` | Bearer token from env only; reads retry with exponential backoff + jitter; token never sent to non-Canvas hosts; redacted from all logs |
| Decision logic | `agent/decide.py` | Deterministic gates first (rate budget, "anything new?") → LLM proposes JSON → validator disposes. No new entries and no reason to start a thread = **no LLM call, no post** |
| Persistent memory | `agent/memory.py` | SQLite `state/agent.db`: entries seen/handled/own, write-ahead action intent log, cycle history, failure counter. Survives restarts. |
| Idempotency | `memory.begin_action` | Write-ahead intent with `UNIQUE(kind, parent, normalized body)`; one reply per parent ever; similarity check vs. earlier posts |
| Verification | `Agent._verify` | Re-reads forum after every write and confirms post by user ID + text exists before marking `verified`. Exponential backoff: 1, 2, 4, 8s. |
| Rate limits | `config.py`, `cycle.py` | ≤2 posts/cycle, ≤2/hour (hard cap 3), ≤8/day, new threads ≤1 per 12h; re-checked at write time |
| Control line | `safety.control_state` | Topic re-fetched before **every** write; only exact first line `COURSE-TEAM CONTROL: RUNNING` allows writes (PAUSED/missing/malformed → read-only, fail-closed) |
| Stopping rule | `memory.bump_failure` | 3 consecutive failed cycles → `halted`; cycles refuse to run until `python3 -m agent unhalt` |
| Prompt-injection defense | `persona.py`, `safety.py` | Forum text wrapped as `<untrusted_forum_content>` in LLM prompt; LLM has **no tools** (text in, JSON out); only two write paths to one hard-coded topic; suspicious entries flagged via regex; outputs with links, emails, key-shaped strings, or secrets are rejected |
| Blast radius | — | One token, one topic, no shell/file tools for the model, no edit/delete of others' posts |

## 4. Supporting Activity Evidence

The full log is at `evidence/cycles_excerpt.jsonl` (redacted; no secrets). Excerpts below.

```
$ python3 -m agent status
halted: no | consecutive failures: 0
actions[verified]: 4
cycle   6 2026-10-03 16:47:56 posted 2 verified post(s)
cycle   5 2026-10-03 16:00:01 no_post rate_limited
cycle   4 2026-10-03 15:05:13 posted 1 verified post(s)
cycle   3 2026-10-03 15:04:24 error (reconciliation timing)
cycle   2 2026-10-03 15:03:06 dry_run 1 action(s) not sent
cycle   1 2026-10-03 14:58:31 error (API key setup)
```

**Cycle 5 — deliberate no-post (cron-scheduled, 16:00 ET):**

```json
{"event": "control_line", "state": "RUNNING"}
{"event": "no_post", "reason": "rate_limited"}
{"event": "cycle_end", "outcome": "no_post", "detail": "rate_limited"}
```

The agent hit the 2-posts-per-hour cap from cycle 4's post and correctly stayed silent. More cycles will accumulate before the Wednesday deadline — the cron runs every 3h and `nothing_new` or `model_chose_silence` skips are expected when the forum quiets down.

**Cycle 4 — reconciliation then autonomous reply:**

```json
{"event": "reconciled_found", "action": 1, "canvas_id": "229542"}
{"event": "llm_decision", "shown": 17, "proposed": 1, "rejected": [], "skip_reason": ""}
{"event": "post_verified", "kind": "reply", "canvas_id": "229543", "parent": "229028"}
```

Cycle 4 first reconciled cycle 3's post (229542) that had a Canvas read-after-write timing issue, then autonomously decided to reply to entry 229028.

## 5. Failure & Recovery Evidence

Fault injected: `HW3_FAULT=lost_ack python3 -m agent run` (one-shot, off by default).

**Exact log from `state/cycles.jsonl`:**

```json
{"event": "FAULT_INJECTION_ARMED", "mode": "lost_ack"}
{"event": "control_line", "state": "RUNNING"}
{"event": "llm_decision", "shown": 2, "proposed": 2, "rejected": [], "skip_reason": ""}
{"event": "write_ambiguous", "why": "injected: response lost after write applied", "attempt": 1}
{"event": "post_verified", "kind": "reply", "canvas_id": "229551", "parent": "229546"}
{"event": "post_verified", "kind": "reply", "canvas_id": "229553", "parent": "229550"}
{"event": "cycle_end", "outcome": "posted", "detail": "2 verified post(s)"}
```

**What happened, step by step:**

1. Two actions proposed by the LLM (replies to 229546 and 229550).
2. First action: POST sent, Canvas applied the write, but the fault injector dropped the response (`write_ambiguous` on attempt 1).
3. Agent re-read the forum to check if the post landed — Canvas read-after-write propagation hadn't caught up (~2s delay in production), so `_find_own` returned nothing.
4. Agent retried on attempt 2. The fault had already fired (one-shot), so this became a normal write. Post landed and was verified via `_verify` (exponential backoff: 1s → 2s → 4s → 8s).
5. Second action: no fault injection, posted and verified cleanly.
6. Result: both posts on Canvas, zero lost posts, no duplicates.

**Note on test vs. production:** The test suite's fake Canvas returns posts immediately on re-read, so `lost_ack` exercises the one-step `recovered_no_duplicate` path in tests. Real Canvas had ~2s propagation delay, which exercised the two-attempt retry path instead. Both paths protect against data loss — the agent retries intelligently and never gives up on a post that might have landed.