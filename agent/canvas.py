"""Minimal Canvas client. Reads retry with backoff; writes never blind-retry on ambiguity."""
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

from .net import NetError, TransportError, http, retry
from .safety import strip_html


class SimulatedCrash(BaseException):
    pass


class FaultInjector:
    """One-shot failure for the HW3 recovery demo. Armed only via HW3_FAULT (or tests)."""
    MODES = ("timeout", "http500", "lost_ack", "malformed", "crash")

    def __init__(self, mode=None, die=None):
        if mode and mode not in self.MODES:
            raise SystemExit(f"HW3_FAULT must be one of {self.MODES}")
        self.mode, self.fired = mode, False
        self.die = die or (lambda: os._exit(137))  # simulate kill -9 right after the write landed

    def around_write(self, do_write):
        if not self.mode or self.fired:
            return do_write()
        self.fired = True
        m = self.mode
        if m == "timeout":
            raise NetError("injected timeout (request never sent)", ambiguous=True)
        if m == "http500":
            raise NetError("injected HTTP 500", 500, ambiguous=True)
        res = do_write()  # the write really happens...
        if m == "lost_ack":
            raise NetError("injected: response lost after write applied", ambiguous=True)
        if m == "malformed":
            raise NetError("injected: malformed response after write applied", ambiguous=True)
        if m == "crash":
            self.die()
        return res


@dataclass
class Entry:
    id: str
    parent_id: str  # '' for top-level
    user_id: str
    author: str
    text: str
    created_at: str
    depth: int = 0


def flatten_view(view):
    names = {str(p["id"]): p.get("display_name", "") for p in view.get("participants", [])}
    out = []

    def walk(items, parent, depth):
        for e in items or []:
            if not e.get("deleted"):
                uid = str(e.get("user_id", ""))
                out.append(Entry(str(e["id"]), parent, uid, e.get("user_name") or names.get(uid, ""),
                                 strip_html(e.get("message", ""), 1500), e.get("created_at", ""), depth))
            walk(e.get("replies"), str(e["id"]), depth + 1)

    walk(view.get("view"), "", 0)
    return out


class Canvas:
    def __init__(self, base, token, *, sleep=time.sleep, attempts=4, timeout=30, fault=None, log=None):
        self.base, self._token = base.rstrip("/"), token
        self.sleep, self.attempts, self.timeout = sleep, attempts, timeout
        self.fault = fault or FaultInjector(None)
        self.log = log or (lambda *a, **k: None)

    # -- plumbing --
    def _once(self, method, url, form, idempotent):
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = None
        if form is not None:
            data = urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            status, hdrs, body = http(method, url, headers, data, self.timeout)
        except TransportError as e:
            raise NetError(f"transport: {e}", retryable=idempotent, ambiguous=not idempotent)
        if status == 429 or (status == 403 and b"rate limit" in body.lower()):
            raise NetError(f"HTTP {status} rate limited", status, retryable=True)  # not executed => safe
        if status >= 500:
            raise NetError(f"HTTP {status}", status, retryable=idempotent, ambiguous=not idempotent)
        if status >= 400:
            raise NetError(f"HTTP {status}", status)
        try:
            return json.loads(body or b"null"), hdrs
        except ValueError:
            raise NetError("malformed JSON response", status, retryable=idempotent, ambiguous=not idempotent)

    def _call(self, method, path, params=None, form=None, idempotent=True):
        url = self.base + path + (("?" + urlencode(params, doseq=True)) if params else "")
        return retry(lambda: self._once(method, url, form, idempotent), attempts=self.attempts, sleep=self.sleep,
                     on_retry=lambda n, d, e: self.log("retry", n=n, delay=round(d, 1), why=str(e)))

    def _get(self, path, params=None):
        return self._call("GET", path, params)[0]

    def _paginate(self, path, params=None):
        url, items = self.base + path + "?" + urlencode({"per_page": 100, **(params or {})}), []
        for _ in range(20):
            data, hdrs = retry(lambda u=url: self._once("GET", u, None, True), attempts=self.attempts, sleep=self.sleep)
            items += data
            m = re.search(r'<([^>]+)>;\s*rel="next"', {k.lower(): v for k, v in hdrs.items()}.get("link", ""))
            if not m or not m.group(1).startswith(self.base + "/"):  # never send the token to another host
                break
            url = m.group(1)
        return items

    # -- reads --
    def self_user(self):
        return self._get("/api/v1/users/self")

    def find_course(self, hint):
        for c in self._paginate("/api/v1/courses", {"enrollment_state": "active"}):
            if hint.lower() in f"{c.get('course_code', '')} {c.get('name', '')}".lower():
                return str(c["id"])
        raise NetError(f"no active course matching {hint!r}", 404)

    def find_topic(self, course_id, title):
        for t in self._paginate(f"/api/v1/courses/{course_id}/discussion_topics", {"search_term": title}):
            if t.get("title", "").strip().lower() == title.lower():
                return str(t["id"])
        raise NetError(f"discussion topic {title!r} not found / not visible", 404)

    def get_topic(self, course_id, topic_id):
        return self._get(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}")

    def get_entries(self, course_id, topic_id):
        return flatten_view(self._get(f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/view"))

    # -- writes (single attempt on ambiguity; caller reconciles) --
    def post_entry(self, course_id, topic_id, html_message):
        p = f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries"
        return self._write(p, html_message)

    def post_reply(self, course_id, topic_id, entry_id, html_message):
        p = f"/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries/{entry_id}/replies"
        return self._write(p, html_message)

    def _write(self, path, html_message):
        def do():
            data, _ = self._call("POST", path, form={"message": html_message}, idempotent=False)
            return data
        res = self.fault.around_write(do)
        if not isinstance(res, dict) or "id" not in res:
            raise NetError("write response missing entry id", ambiguous=True)
        return res
