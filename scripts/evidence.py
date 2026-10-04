#!/usr/bin/env python3
"""Build a submission-ready evidence report from the agent's local state (state/agent.db + state/cycles.jsonl).

    python3 scripts/evidence.py > evidence/EVIDENCE.md          # offline, from local memory
    python3 scripts/evidence.py --live > evidence/EVIDENCE.md   # also re-reads Canvas to prove no duplicate posts

Includes only the agent's OWN posts (never other people's text or names). Secrets are never printed.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RECOVERY = ("FAULT_INJECTION_ARMED", "retry", "write_ambiguous", "recovered_no_duplicate", "reconciled_found",
            "reconciled_missing", "duplicate_prevented", "cycle_failed", "HALTED", "write_blocked")


def ts(x):
    return datetime.fromtimestamp(float(x), timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if x else "?"


def load_events(path):
    out = []
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("HW3_STATE_DIR", "state"))
    ap.add_argument("--live", action="store_true", help="re-read Canvas and verify one live entry per verified post")
    a = ap.parse_args()
    db = sqlite3.connect(Path(a.state) / "agent.db")
    db.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in db.execute("SELECT * FROM meta")}
    base = os.environ.get("CANVAS_BASE_URL", "https://canvas.mit.edu").rstrip("/")
    cid, tid = meta.get("course_id", "?"), meta.get("topic_id", "?")
    events = load_events(Path(a.state) / "cycles.jsonl")
    cycles = db.execute("SELECT * FROM cycles ORDER BY id").fetchall()
    actions = db.execute("SELECT * FROM actions ORDER BY id").fetchall()
    P = print

    P("# Evidence report (generated from local agent state)\n")
    done = [c for c in cycles if c["outcome"]]
    oc = Counter(c["outcome"].split(":")[0] for c in done)
    P(f"- Cycles recorded: **{len(cycles)}** ({ts(cycles[0]['started']) if cycles else '?'} -> "
      f"{ts(cycles[-1]['started']) if cycles else '?'})")
    P("- Outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(oc.items())))
    P(f"- Halted: {meta.get('halted') or 'no'} | consecutive failures now: {meta.get('consecutive_failures', 0)}\n")

    P("## 1. Forum posts made autonomously (verified on Canvas)\n")
    verified = [x for x in actions if x["status"] == "verified"]
    P("| # | When | Kind | Link | Opening text |\n|---|---|---|---|---|")
    for i, x in enumerate(verified, 1):
        link = f"{base}/courses/{cid}/discussion_topics/{tid}?entry_id={x['canvas_id']}"
        P(f"| {i} | {ts(x['created'])} | {x['kind']} | {link} | {x['body'][:110].replace('|', '/')!r} |")
    P(f"\nForum: {base}/courses/{cid}/discussion_topics/{tid}\n")

    P("## 2. Scheduled cycles\n")
    P("| Cycle | Started | Outcome | Detail |\n|---|---|---|---|")
    for c in cycles[-40:]:
        P(f"| {c['id']} | {ts(c['started'])} | {c['outcome'] or 'unfinished/crashed'} | {(c['detail'] or '')[:80]} |")

    P("\n## 3. Cycles where the agent deliberately did not post\n")
    nop = [e for e in events if e.get("event") == "no_post"]
    if not nop:
        P("_None logged yet._")
    for e in nop:
        P(f"- cycle {e.get('cycle')} at {ts(e['ts'])}: reason = `{e.get('reason')}`")
    for e in events:
        if e.get("event") == "control_line" and e.get("state") != "RUNNING":
            P(f"- cycle {e.get('cycle')} at {ts(e['ts'])}: forum control line was `{e.get('state')}` -> read-only")

    P("\n## 4. Failure and recovery\n")
    by_cycle = defaultdict(list)
    for e in events:
        if e.get("event") in RECOVERY:
            by_cycle[e.get("cycle")].append(e)
    if not by_cycle:
        P("_No failure/recovery events logged yet. Run once with `HW3_FAULT=lost_ack python3 -m agent run`._")
    for c, evs in by_cycle.items():
        P(f"\n**{'Before cycle start (agent boot)' if c is None else f'Cycle {c}'}**")
        for e in evs:
            extra = {k: v for k, v in e.items() if k not in ("ts", "cycle", "event")}
            P(f"- {ts(e['ts'])} `{e['event']}` {json.dumps(extra)[:200]}")
    keys = Counter((x["kind"], x["parent_id"], x["body_norm"]) for x in actions)
    ids = [x["canvas_id"] for x in verified]
    P("\n**Duplicate-effect checks (local ledger)**")
    P(f"- Intents with identical (kind, parent, text): {sum(1 for v in keys.values() if v > 1)} (UNIQUE constraint => always 0)")
    P(f"- Verified posts: {len(verified)}; distinct Canvas ids: {len(set(ids))}")
    P(f"- Intents still pending/posted (unresolved): {sum(1 for x in actions if x['status'] in ('pending', 'posted'))}")
    for x in actions:
        if x["attempts"] and x["attempts"] > 1:
            P(f"- action {x['id']} needed {x['attempts']} attempts but produced status `{x['status']}`, canvas id {x['canvas_id']}")

    if a.live:
        from agent.canvas import Canvas
        from agent.config import Config
        cfg = Config.from_env()
        entries = Canvas(cfg.canvas_base, cfg.canvas_token).get_entries(cid, tid)
        mine = [e for e in entries if e.user_id == meta.get("self_id")]
        texts = Counter(re.sub(r"\W+", " ", e.text.lower()) for e in mine)
        P("\n**Live Canvas check**")
        P(f"- Own entries on Canvas: {len(mine)} | ledger verified: {len(verified)} | "
          f"duplicate texts on Canvas: {sum(1 for v in texts.values() if v > 1)}")
        P(f"- Ledger ids all present on Canvas: {set(ids) <= {e.id for e in mine}}")

    P("\n## 5. Secret scan of logs and this report\n")
    blobs = [Path(a.state, "cycles.jsonl").read_text() if Path(a.state, "cycles.jsonl").exists() else ""]
    pats = re.compile(r"(sk-or-v1-[A-Za-z0-9]{10,}|\b\d{3,6}~[A-Za-z0-9]{20,})")
    secrets = [v for k in ("CANVAS_TOKEN", "OPENROUTER_API_KEY") if (v := os.environ.get(k)) and len(v) > 6]
    hit = any(pats.search(b) or any(s in b for s in secrets) for b in blobs)
    P("- " + ("**FOUND something key-shaped in cycles.jsonl: do not submit it**" if hit else
              "No token/key-shaped strings or env secret values found in cycles.jsonl."))


if __name__ == "__main__":
    main()
