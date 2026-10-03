"""One autonomous cycle: reconcile -> read -> decide -> guarded write -> verify."""
import json
import time

from . import safety
from .decide import make_plan
from .net import NetError


class Log:
    def __init__(self, path, secrets, echo=True, clock=time.time):
        self.path, self.secrets, self.echo, self.clock, self.cycle = path, secrets, echo, clock, None

    def __call__(self, event, **kw):
        rec = {"ts": round(self.clock(), 1), "cycle": self.cycle, "event": event, **kw}
        line = safety.redact(json.dumps(rec, ensure_ascii=False, default=str), self.secrets)
        if self.path:
            with open(self.path, "a") as f:
                f.write(line + "\n")
        if self.echo:
            print(line, flush=True)


class Skip(Exception):
    """Benign reason not to continue (not a failure)."""


class Agent:
    def __init__(self, cfg, canvas, llm, mem, log, clock=time.time, sleep=time.sleep):
        self.cfg, self.canvas, self.llm, self.mem, self.log = cfg, canvas, llm, mem, log
        self.clock, self.sleep = clock, sleep
        self.course = self.topic = self.self_id = None

    # ---------------------------------------------------------------- cycle
    def run_cycle(self):
        mem, now = self.mem, self.clock()
        if mem.halted():
            self.log("halted", reason=mem.halted())
            return "halted"
        cid = mem.start_cycle(now)
        self.log.cycle = cid
        outcome, detail = "error", ""
        try:
            outcome, detail = self._cycle()
            mem.clear_failures()
        except Skip as s:
            outcome, detail = f"skipped:{s}", str(s)
            mem.clear_failures()
        except (NetError, Exception) as e:  # noqa: BLE001 - a cycle must never crash the scheduler
            detail = safety.redact(f"{type(e).__name__}: {e}", self.cfg.secrets)
            n = mem.bump_failure(self.cfg.max_consecutive_failures, detail)
            self.log("cycle_failed", why=detail, consecutive=n)
            if mem.halted():
                self.log("HALTED", reason=mem.halted())
        finally:
            mem.end_cycle(cid, self.clock(), outcome, detail)
            self.log("cycle_end", outcome=outcome, detail=detail)
        return outcome

    def _cycle(self):
        cfg, mem = self.cfg, self.mem
        self._resolve()
        topic = self.canvas.get_topic(self.course, self.topic)
        state = safety.control_state(topic.get("message", ""))
        self.log("control_line", state=state)
        entries = self.canvas.get_entries(self.course, self.topic)
        self._reconcile(entries)
        entries = self.canvas.get_entries(self.course, self.topic)
        mem.ingest(entries, self.self_id, self.clock(), safety.looks_like_injection)
        flagged = mem.db.execute("SELECT COUNT(*) c FROM entries WHERE flagged=1 AND handled=0").fetchone()["c"]
        if flagged:
            self.log("injection_suspected", count=flagged)

        if state != "RUNNING":
            raise Skip(f"control_{state.lower()}")  # read-only: leaves entries unhandled for later
        if topic.get("locked") or topic.get("published") is False:
            raise Skip("topic_locked_or_unpublished")

        plan = make_plan(cfg, self.llm, mem, entries, self.self_id, self.clock(), self.log)
        if plan.used_llm:
            mem.mark_handled(plan.shown_ids)
            self.log("llm_decision", shown=len(plan.shown_ids), proposed=len(plan.actions),
                     rejected=plan.rejected, skip_reason=plan.skip_reason, usage=self.llm.last_usage)
        if not plan.actions:
            self.log("no_post", reason=plan.skip_reason)
            return "no_post", plan.skip_reason
        if cfg.dry_run:
            for a in plan.actions:
                self.log("dry_run_action", **a)
            return "dry_run", f"{len(plan.actions)} action(s) not sent"
        done = 0
        for a in plan.actions:
            if self._guarded_write(a):
                done += 1
        return ("posted" if done else "no_post"), f"{done} verified post(s)"

    # ---------------------------------------------------------- resolution
    def _resolve(self):
        cfg, mem = self.cfg, self.mem
        self.self_id = mem.get("self_id")
        if not self.self_id:
            self.self_id = str(self.canvas.self_user()["id"])
            mem.set("self_id", self.self_id)
        try:
            self.course = cfg.course_id or mem.get("course_id") or self.canvas.find_course(cfg.course_hint)
            self.topic = cfg.topic_id or mem.get("topic_id") or self.canvas.find_topic(self.course, cfg.topic_title)
            mem.set("course_id", self.course)
            mem.set("topic_id", self.topic)
            t = self.canvas.get_topic(self.course, self.topic)
        except NetError as e:
            if e.status == 404:
                raise Skip("topic_not_visible_yet")  # forum not published yet: wait, don't count as failure
            raise
        if t.get("title", "").strip().lower() != cfg.topic_title.lower():
            raise NetError(f"topic {self.topic} title mismatch -- refusing to write to the wrong place")

    # ----------------------------------------------------------- reconcile
    def _find_own(self, entries, action):
        want = safety.norm(action["body"])
        for e in entries:
            if e.user_id == self.self_id and e.parent_id == action["parent_id"] and safety.norm(e.text) == want:
                return e
        return None

    def _reconcile(self, entries):
        """Resolve intents left 'pending'/'posted' by a crash, timeout or lost ack -- BEFORE doing anything new."""
        for a in self.mem.pending():
            e = self._find_own(entries, a)
            if e:
                self.mem.set_status(a["id"], "verified", self.clock(), e.id, "reconciled from Canvas")
                self.log("reconciled_found", action=a["id"], canvas_id=e.id)
            else:
                self.mem.set_status(a["id"], "abandoned", self.clock(), detail="not on Canvas; safe to retry later")
                self.log("reconciled_missing", action=a["id"])

    # --------------------------------------------------------------- write
    def _guarded_write(self, a):
        cfg, mem = self.cfg, self.mem
        body = a["body"]
        aid = None
        for attempt in (1, 2):
            # Required by the course: fetch the topic and read the control line before EVERY write.
            topic = self.canvas.get_topic(self.course, self.topic)
            state = safety.control_state(topic.get("message", ""))
            if state != "RUNNING":
                self.log("write_blocked", state=state)
                if aid:
                    mem.set_status(aid, "abandoned", self.clock(), detail=f"control {state}")
                return False
            now = self.clock()
            if mem.writes_since(now - 3600) >= cfg.max_posts_per_hour or mem.writes_since(now - 86400) >= cfg.max_posts_per_day:
                self.log("write_blocked", state="rate_limited")
                return False
            if aid is None:
                aid = mem.begin_action(a["kind"], a["parent_id"], body, safety.norm(body), now)
                if aid is None:
                    self.log("duplicate_prevented", kind=a["kind"], parent=a["parent_id"])
                    return False
            else:
                mem.set_status(aid, "pending", now)
            mem.bump_attempt(aid)
            html = safety.to_html(body)
            try:
                if a["kind"] == "reply":
                    res = self.canvas.post_reply(self.course, self.topic, a["parent_id"], html)
                else:
                    res = self.canvas.post_entry(self.course, self.topic, html)
            except NetError as e:
                if not e.ambiguous:
                    mem.set_status(aid, "failed", self.clock(), detail=str(e))
                    raise
                self.log("write_ambiguous", why=str(e), attempt=attempt)
                found = self._find_own(self.canvas.get_entries(self.course, self.topic), mem.get_action(aid))
                if found:  # the write DID land: record it, never repost
                    mem.set_status(aid, "verified", self.clock(), found.id, "recovered after ambiguous failure")
                    self.log("recovered_no_duplicate", canvas_id=found.id)
                    return True
                self.sleep(2 ** attempt)
                if attempt == 2:
                    mem.set_status(aid, "failed", self.clock(), detail="ambiguous twice, not on Canvas")
                    raise
                continue
            mem.set_status(aid, "posted", self.clock(), str(res["id"]))
            return self._verify(aid, str(res["id"]))
        return False

    def _verify(self, aid, canvas_id):
        act = self.mem.get_action(aid)
        for _ in range(3):
            e = self._find_own(self.canvas.get_entries(self.course, self.topic), act)
            if e:
                self.mem.set_status(aid, "verified", self.clock(), e.id)
                self.log("post_verified", kind=act["kind"], canvas_id=e.id, parent=act["parent_id"])
                return True
            self.sleep(1)
        raise NetError(f"post {canvas_id} acknowledged but not visible on re-read")
