#!/usr/bin/env python3
"""Pre-submit gate for HW3. Run on the machine that has state/:   python3 scripts/presubmit_check.py [--live]

Checks the hand-in requirements and the things that cost points or leak data. Exit code 1 if any FAIL.
--live also re-reads Canvas (needs CANVAS_TOKEN) to prove there are no duplicate posts on the forum.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent.config import load_dotenv  # noqa: E402

KEYS = re.compile(r"(sk-or-v1-[A-Za-z0-9]{10,}|\b\d{3,6}~[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,})")
SILENCE_BY_JUDGMENT = {"nothing_new", "model_chose_silence", "all_proposals_rejected"}
results = []


def rec(level, name, detail=""):
    results.append((level, name, detail))
    print(f"[{level:4}] {name}" + (f": {detail}" if detail else ""))


def sh(*cmd):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).stdout


def events(path):
    out = []
    if path.exists():
        for ln in path.read_text().splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("HW3_STATE_DIR", "state"))
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    load_dotenv()
    secrets = [v for k in ("CANVAS_TOKEN", "OPENROUTER_API_KEY") if (v := os.environ.get(k)) and len(v) > 6]

    # ---- repo hygiene -------------------------------------------------------------------------------
    tracked = sh("git", "ls-files").split()
    bad = [f for f in tracked if re.search(r"(^|/)(\.env|state/)|\.(db|sqlite|log)$", f) and f != ".env.example"]
    rec("FAIL" if bad else "PASS", "no .env / state / db / log files tracked", ", ".join(bad))
    hist = sh("git", "log", "--all", "--name-only", "--format=")
    hbad = sorted({f for f in hist.split() if re.search(r"(^|/)(\.env|state/)|\.(db|sqlite)$", f) and f != ".env.example"})
    rec("FAIL" if hbad else "PASS", "no secret-bearing files anywhere in git history", ", ".join(hbad))
    leaks = []
    for f in tracked + ["SUBMISSION.md"] + [str(p.relative_to(ROOT)) for p in (ROOT / "evidence").glob("*")]:
        p = ROOT / f
        if p.is_file() and not f.startswith("tests/"):
            txt = p.read_text(errors="ignore")
            if KEYS.search(txt) or any(s in txt for s in secrets):
                leaks.append(f)
    hist_txt = sh("git", "log", "--all", "-p", "--no-color")
    if KEYS.search(hist_txt.replace("sk-or-v1-fakekey", "")) or any(s in hist_txt for s in secrets):
        leaks.append("<git history>")
    rec("FAIL" if leaks else "PASS", "no keys/tokens in files, evidence or history", ", ".join(leaks))

    # ---- write-up ---------------------------------------------------------------------------------
    sub = (ROOT / "SUBMISSION.md").read_text() if (ROOT / "SUBMISSION.md").exists() else ""
    rec("FAIL" if "TODO" in sub else "PASS", "SUBMISSION.md has no TODO left")
    m = re.search(r"https://github\.com/[\w.-]+/[\w.-]+", sub)
    if m:
        try:
            code = urllib.request.urlopen(m.group(0), timeout=15).status
        except urllib.error.HTTPError as e:
            code = e.code
        except OSError:
            code = None
        rec("PASS" if code == 200 else "WARN", "repo URL is publicly reachable",
            "" if code == 200 else f"HTTP {code}: graders cannot open it -> make it public or submit the ZIP")

    # ---- agent state --------------------------------------------------------------------------------
    db_path = Path(a.state) / "agent.db"
    if not db_path.exists():
        rec("FAIL", "state/agent.db found", f"{db_path} missing: run this on the machine where the agent runs")
        return finish()
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    meta = {r["key"]: r["value"] for r in db.execute("SELECT * FROM meta")}
    ev = events(Path(a.state) / "cycles.jsonl")
    cycles = [dict(r) for r in db.execute("SELECT * FROM cycles WHERE outcome IS NOT NULL ORDER BY id")]
    acts = [dict(r) for r in db.execute("SELECT * FROM actions ORDER BY id")]

    rec("FAIL" if meta.get("halted") else "PASS", "agent is not halted", meta.get("halted", ""))
    spaced, last = [], None
    for c in cycles:
        if last is None or c["started"] - last >= 3600:
            spaced.append(c)
        last = c["started"]
    ok = [c for c in spaced if c["outcome"] in ("posted", "no_post") or c["outcome"].startswith("skipped")]
    rec("PASS" if len(ok) >= 4 else "WARN", "several scheduled-looking cycles (>=1h apart, completed)",
        f"{len(ok)} found (heuristic: manual back-to-back runs are not counted); aim for 4+")
    own_posts = [c for c in ok if c["outcome"] == "posted"]
    rec("PASS" if len(own_posts) >= 2 else "WARN", "at least 2 of those cycles posted", f"{len(own_posts)}")
    nop = [e for e in ev if e.get("event") == "no_post"]
    judged = [e for e in nop if e.get("reason") in SILENCE_BY_JUDGMENT]
    rec("PASS" if judged else ("WARN" if nop else "FAIL"), "a run where the agent chose not to post (by judgment)",
        "" if judged else ("only 'rate_limited' no-posts so far: a cap, not a choice; wait for a quiet cycle" if nop
                           else "none logged yet"))

    # ---- failure/recovery -------------------------------------------------------------------------
    names = [e.get("event") for e in ev]
    armed, amb = "FAULT_INJECTION_ARMED" in names, "write_ambiguous" in names
    rec("PASS" if armed and amb else "FAIL", "injected failure is in the log", "FAULT_INJECTION_ARMED + write_ambiguous")
    rec("PASS" if {"recovered_no_duplicate", "reconciled_found"} & set(names) else "FAIL",
        "recovery event is in the log", "recovered_no_duplicate or reconciled_found")
    rec("FAIL" if "write_unconfirmed" in names and not {"reconciled_found", "reconciled_missing"} & set(names)
        else "PASS", "every unconfirmed write was later reconciled")
    unresolved = [x["id"] for x in acts if x["status"] in ("pending", "posted")]
    rec("WARN" if unresolved else "PASS", "no unresolved intents in the ledger", str(unresolved))
    dup = {}
    for x in acts:
        if x["status"] == "verified":
            dup.setdefault(x["canvas_id"], 0)
            dup[x["canvas_id"]] += 1
    rec("FAIL" if any(v > 1 for v in dup.values()) else "PASS", "ledger: one Canvas id per verified post")

    # ---- consistency between write-up and logs ------------------------------------------------------
    quoted = [json.loads(l) for l in re.findall(r"^\s*(\{\"[^\n]*\})\s*$", sub, re.M) if "..." not in l]
    quoted = [q for q in quoted if isinstance(q, dict) and "event" in q]
    miss = [q["event"] for q in quoted if not any(all(e.get(k) == v for k, v in q.items()) for e in ev)]
    rec("FAIL" if miss else "PASS", "every log line quoted in SUBMISSION.md exists in cycles.jsonl",
        f"not found: {miss}" if miss else f"{len(quoted)} lines checked")
    unlinked = [x["canvas_id"] for x in acts if x["status"] == "verified" and x["canvas_id"] not in sub]
    rec("WARN" if unlinked else "PASS", "every verified post is linked in SUBMISSION.md",
        f"missing: {unlinked}" if unlinked else "")

    if a.live:
        from agent.canvas import Canvas
        from agent.config import Config
        cfg = Config.from_env()
        entries = Canvas(cfg.canvas_base, cfg.canvas_token).get_entries(meta["course_id"], meta["topic_id"])
        mine = [e for e in entries if e.user_id == meta.get("self_id")]
        ledger = {x["canvas_id"] for x in acts if x["status"] == "verified"}
        extra = [e.id for e in mine if e.id not in ledger]
        norm = lambda s: re.sub(r"\W+", " ", s.lower()).strip()  # noqa: E731
        seen, twins = {}, []
        for e in mine:
            k = (e.parent_id, norm(e.text))
            if k in seen:
                twins.append((seen[k], e.id))
            seen.setdefault(k, e.id)
        rec("FAIL" if extra else "PASS", "LIVE: every own post on Canvas is in the ledger",
            f"on Canvas but NOT in ledger: {extra} (an unrecorded extra post = duplicate effect)" if extra else
            f"{len(mine)} own entries")
        rec("FAIL" if twins else "PASS", "LIVE: no duplicate posts on the forum",
            f"identical reply pairs (keep first, delete the later one in the Canvas UI): {twins}" if twins else "")
    else:
        rec("WARN", "LIVE duplicate check skipped", "re-run with --live (needs CANVAS_TOKEN)")
    return finish()


def finish():
    n = {k: sum(1 for r in results if r[0] == k) for k in ("PASS", "WARN", "FAIL")}
    print(f"\n{n['PASS']} pass, {n['WARN']} warn, {n['FAIL']} fail")
    sys.exit(1 if n["FAIL"] else 0)


if __name__ == "__main__":
    main()
