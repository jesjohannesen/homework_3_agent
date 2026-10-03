"""In-process fake Canvas + fake OpenRouter on localhost, with knobs for failure injection."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN, KEY = "fake-canvas-token-1234567890", "sk-or-v1-fakekeyfakekeyfakekey"


class State:
    def __init__(self):
        self.control = "RUNNING"
        self.entries = []  # dicts: id,user_id,user_name,parent_id,message,created_at,deleted
        self.next_id = 100
        self.me = 7
        self.drop_response_once = False   # apply POST then close socket without replying
        self.fail_status = 0              # respond with this status to all requests when >0
        self.llm_script = []              # list of dicts returned by successive LLM calls
        self.llm_calls = 0
        self.posts = 0
        self.topic_published = True

    def add(self, user_id, name, message, parent=None):
        self.next_id += 1
        e = dict(id=self.next_id, user_id=user_id, user_name=name, parent_id=parent, message=message,
                 created_at="2026-10-03T10:00:00Z")
        self.entries.append(e)
        return e

    def tree(self):
        def kids(pid):
            return [dict(e, replies=kids(e["id"])) for e in self.entries if e["parent_id"] == pid]
        return kids(None)


def make_server(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

        def _route(self, method):
            u = urlparse(self.path); path = u.path
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            if state.fail_status:
                return self._send(state.fail_status, {"errors": "boom"})
            if path.endswith("/chat/completions"):
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    return self._send(401, {})
                state.llm_calls += 1
                out = state.llm_script.pop(0) if state.llm_script else {"skip_reason": "script empty", "actions": []}
                return self._send(200, {"choices": [{"message": {"content": json.dumps(out)}}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                return self._send(401, {"errors": "unauthorized"})
            if path == "/api/v1/users/self":
                return self._send(200, {"id": state.me, "name": "Me"})
            if path == "/api/v1/courses":
                return self._send(200, [{"id": 5, "course_code": "MAS.665", "name": "Agentic AI"}])
            if path == "/api/v1/courses/5/discussion_topics":
                if not state.topic_published:
                    return self._send(200, [])
                return self._send(200, [{"id": 9, "title": "Homework 3: Agent Discussion Forum"}])
            if path == "/api/v1/courses/5/discussion_topics/9":
                if not state.topic_published:
                    return self._send(404, {})
                return self._send(200, {"id": 9, "title": "Homework 3: Agent Discussion Forum",
                                        "message": f"<p>COURSE-TEAM CONTROL: {state.control}</p><p>Purpose...</p>"})
            if path == "/api/v1/courses/5/discussion_topics/9/view":
                return self._send(200, {"participants": [], "view": state.tree()})
            if method == "POST" and path.startswith("/api/v1/courses/5/discussion_topics/9/entries"):
                parts = path.split("/")
                parent = int(parts[-2]) if path.endswith("/replies") else None
                msg = parse_qs(body.decode())["message"][0]
                e = state.add(state.me, "Me", msg, parent)
                state.posts += 1
                if state.drop_response_once:
                    state.drop_response_once = False
                    self.close_connection = True
                    self.connection.close()
                    return
                return self._send(200, {"id": e["id"]})
            self._send(404, {})

        def do_GET(self): self._route("GET")
        def do_POST(self): self._route("POST")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
