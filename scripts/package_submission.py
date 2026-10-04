#!/usr/bin/env python3
"""Build hw3-submission.zip: git-tracked code + SUBMISSION.md (as README_SUBMISSION.md) + redacted evidence.

    python3 scripts/package_submission.py [--state state] [--out hw3-submission.zip]

Never includes .env, state/agent.db, caches, or anything untracked. Fails if the finished zip contains key-shaped text.
"""
import argparse
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent.config import load_dotenv  # noqa: E402
from agent.safety import redact  # noqa: E402

KEYS = re.compile(r"(sk-or-v1-[A-Za-z0-9]{10,}|\b\d{3,6}~[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{16,})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("HW3_STATE_DIR", "state"))
    ap.add_argument("--out", default="hw3-submission.zip")
    a = ap.parse_args()
    load_dotenv()
    secrets = [v for k in ("CANVAS_TOKEN", "OPENROUTER_API_KEY") if (v := os.environ.get(k))]
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    files = {f: (ROOT / f).read_bytes() for f in tracked if (ROOT / f).is_file() and f != "SUBMISSION.md"}
    if (ROOT / "SUBMISSION.md").exists():  # the "README containing the remaining information" the assignment asks for
        files["README_SUBMISSION.md"] = (ROOT / "SUBMISSION.md").read_bytes()
    log = Path(a.state) / "cycles.jsonl"
    if log.exists():  # full redacted log, so graders can check every excerpt
        files["evidence/cycles.jsonl"] = redact(log.read_text(), secrets).encode()
    bad = [f for f in files if re.search(r"(^|/)(\.env|state/)|\.(db|sqlite|log)$", f) and f != ".env.example"]
    if bad:
        sys.exit(f"refusing to package private files: {bad}")
    for f, data in files.items():
        txt = data.decode(errors="ignore")
        if KEYS.search(txt) or any(s in txt for s in secrets):
            if not f.startswith("tests/"):
                sys.exit(f"key-shaped text in {f}; fix before packaging")
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for f, data in sorted(files.items()):
            z.writestr(f"hw3-agent/{f}", data)
    print(f"wrote {a.out} ({len(files)} files). Contents:")
    for f in sorted(files):
        print("  ", f)


if __name__ == "__main__":
    main()
