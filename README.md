# HW3 agent: FeynBro

Autonomous agent for MAS.665 Homework 3. Wakes up on a schedule, reads the Canvas *Agent Discussion Forum*,
decides whether it has anything useful to add, posts (max 2/hour, hard-capped at 3), verifies the post landed,
and remembers everything in a local SQLite file. Persona: Feynman x Taleb x terminally-online Gen Z.
Stdlib-only Python 3.11, so there is nothing to install. Default model: `deepseek/deepseek-v4-flash` via OpenRouter
(about $0.03 in / $0.06 out per 1M tokens).

## Setup
```bash
export CANVAS_TOKEN=...          # Canvas > Account > Approved Integrations (short expiry)
export OPENROUTER_API_KEY=...    # or put both in a gitignored .env (see .env.example)
python3 -m agent doctor          # checks auth, finds course + forum, prints the control line
python3 -m agent run --dry-run   # reads and decides, never writes
python3 -m agent run             # one real cycle
```
Schedule (every 3h, no human prompt): `0 */3 * * * /path/to/repo/scripts/run_cycle.sh`
or `python3 -m agent loop --every 3` (in tmux/systemd). A file lock prevents overlapping cycles.
Other commands: `status` (memory + recent cycles), `unhalt` (clear the stop rule), tests: `python3 -m unittest discover -s tests -t .`

## Architecture
| Concern | Where | How |
|---|---|---|
| Scheduler | cron / `agent/scheduler.py` | cycle every ~3h (+/-10% jitter), flock so cycles never overlap |
| Canvas access | `agent/canvas.py` | Bearer token from env only; reads retry with exp. backoff + jitter; token never sent to a non-Canvas host; token redacted from all logs |
| Decision logic | `agent/decide.py` | deterministic gates (rate budget, "anything new?") -> LLM *proposes* JSON -> deterministic validator disposes. No new entries and no reason to start a thread = **no LLM call, no post** |
| Persistent memory | `agent/memory.py` | SQLite `state/agent.db`: entries seen/handled/own, action intent log, cycle history, failure counter |
| Idempotency | `memory.begin_action` | write-ahead intent with `UNIQUE(kind,parent,normalized body)`; one reply per parent, ever; similarity check vs. earlier posts |
| Verification | `Agent._verify` | re-reads the forum and confirms an entry by our user id with our text exists before marking `verified` |
| Rate limits | `config.py`, `cycle.py` | <=2 posts/cycle, <=2/hour (hard cap 3, enforced in config), <=8/day, new threads <=1 per 12h; checked again at write time |
| Control line | `safety.control_state` | topic re-fetched before **every** write; only an exact first line `COURSE-TEAM CONTROL: RUNNING` allows writes (PAUSED / missing / malformed => read-only) |
| Stopping rule | `memory.bump_failure` | 3 consecutive failed cycles => `halted`; cycles refuse to run until `unhalt` |
| Prompt-injection defense | `persona.py`, `safety.py` | forum text is wrapped as untrusted data; the LLM has **no tools** (text in, JSON out); only two writes exist (reply / entry) to one hard-coded topic; suspicious entries are flagged; outputs with links, emails, key-shaped strings or the real secrets are rejected |
| Blast radius | | one token, one topic, no shell/file tools for the model, no edit/delete of others' posts (no such code path) |

## Recovery
Writes are never blindly retried. Before posting, intent is stored as `pending`. If the response is lost, times
out, is a 5xx, or is malformed, the agent re-reads the forum: if its post is there it records it and does **not**
repost; if not, it retries once. If the process dies mid-write, the next cycle reconciles leftover `pending`
intents against Canvas first. Demo any of them live (one-shot, off by default):
```bash
HW3_FAULT=lost_ack python3 -m agent run   # also: timeout | http500 | malformed | crash
```
These are covered by tests against an in-process fake Canvas (`tests/`), including a real dropped socket and a
crash-and-restart with the same DB.
