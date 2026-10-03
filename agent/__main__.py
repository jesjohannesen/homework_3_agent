import argparse
import fcntl
import os
import sys
import time
from pathlib import Path

from .canvas import Canvas, FaultInjector
from .config import Config
from .cycle import Agent, Log
from .llm import LLM
from .memory import Memory
from .scheduler import loop


def build(cfg):
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    log = Log(cfg.log_path, cfg.secrets)
    fault = FaultInjector(os.environ.get("HW3_FAULT") or None)
    if fault.mode:
        log("FAULT_INJECTION_ARMED", mode=fault.mode)
    canvas = Canvas(cfg.canvas_base, cfg.canvas_token, fault=fault, log=log)
    llm = LLM(cfg.openrouter_base, cfg.openrouter_key, cfg.model, log=log)
    return Agent(cfg, canvas, llm, Memory(cfg.db_path), log), log


def status(cfg):
    m = Memory(cfg.db_path)
    print("halted:", m.halted() or "no", "| consecutive failures:", m.failures())
    for r in m.db.execute("SELECT status, COUNT(*) c FROM actions GROUP BY status"):
        print(f"actions[{r['status']}]: {r['c']}")
    for r in m.db.execute("SELECT id, datetime(started,'unixepoch') t, outcome, detail FROM cycles ORDER BY id DESC LIMIT 10"):
        print(f"cycle {r['id']:>3} {r['t']} {r['outcome']} {r['detail'] or ''}")


def doctor(cfg):
    agent, log = build(cfg)
    me = agent.canvas.self_user()
    print("canvas auth ok as user id", me["id"])
    try:
        agent._resolve()
        t = agent.canvas.get_topic(agent.course, agent.topic)
        from .safety import control_state
        print("course", agent.course, "| topic", agent.topic, "| control:", control_state(t.get("message", "")))
    except Exception as e:  # noqa: BLE001
        print("forum not reachable yet:", e)
    print("model:", cfg.model, "| openrouter key set:", bool(cfg.openrouter_key))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agent")
    ap.add_argument("cmd", choices=["run", "loop", "status", "doctor", "unhalt"])
    ap.add_argument("--dry-run", action="store_true", help="read + decide, never write")
    ap.add_argument("--every", type=float, help="loop interval in hours")
    a = ap.parse_args(argv)
    cfg = Config.from_env(require_secrets=a.cmd not in ("status", "unhalt"), dry_run=a.dry_run)
    if a.cmd == "status":
        return status(cfg)
    if a.cmd == "unhalt":
        Memory(cfg.db_path).unhalt()
        return print("halt cleared")
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(cfg.lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # one cycle at a time, even if cron overlaps
    except BlockingIOError:
        sys.exit("another cycle is already running")
    if a.cmd == "doctor":
        return doctor(cfg)
    agent, log = build(cfg)
    if a.cmd == "run":
        sys.exit(0 if agent.run_cycle() != "halted" else 2)
    loop(agent.run_cycle, a.every or cfg.interval_hours)


if __name__ == "__main__":
    main()
