"""Tiny in-process scheduler (alternative to cron): run a cycle, sleep ~interval +/- jitter, repeat until halted."""
import random
import time


def loop(run_cycle, interval_hours, *, sleep=time.sleep, rng=random.random, max_cycles=None, log=print):
    n = 0
    while True:
        outcome = run_cycle()
        n += 1
        if outcome == "halted" or (max_cycles and n >= max_cycles):
            return outcome
        delay = interval_hours * 3600 * (0.9 + 0.2 * rng())
        log(f"next cycle in {delay / 3600:.2f}h")
        sleep(delay)
