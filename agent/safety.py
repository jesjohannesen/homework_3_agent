"""Everything that decides 'is this safe to read / write'. Pure functions, easy to test."""
import html as _html
import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "blockquote", "pre"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, d):
        if not self.skip:
            self.out.append(d)


def strip_html(s, limit=4000):
    p = _Text()
    p.feed(s or "")
    text = "".join(p.out)
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:limit]


def to_html(text):
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    return "".join("<p>" + _html.escape(p).replace("\n", "<br>") + "</p>" for p in paras)


def norm(s):
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


# ---- control line -----------------------------------------------------------
_CTRL = re.compile(r"^COURSE-TEAM CONTROL:\s*(RUNNING|PAUSED)\s*$")


def control_state(topic_message):
    """First non-empty line of the forum description. Anything but exactly RUNNING => no writes (fail closed)."""
    for line in strip_html(topic_message).split("\n"):
        if line.strip():
            m = _CTRL.match(line.strip())
            return m.group(1) if m else "UNKNOWN"
    return "UNKNOWN"


# ---- untrusted input --------------------------------------------------------
_INJECTION = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |your |the )?(previous|prior|above|system)",
    r"(reveal|print|show|share|post|send|leak|dump).{0,40}(token|api[ _-]?key|secret|password|system prompt|credentials|\.env)",
    r"you are now\b", r"new (system )?instructions?:", r"\bsystem prompt\b", r"\bjailbreak\b",
    r"(run|execute|eval)\b.{0,30}(command|shell|bash|script|code)", r"\bcurl\s+http", r"rm\s+-rf",
    r"<script", r"\bsudo\b", r"base64 -d",
    r"(delete|edit|modify|remove).{0,30}(post|entry|reply|thread)s?\b.{0,20}(of|by|from)",
    r"(un)?pause.{0,20}(forum|control)", r"course-team control",
]
_INJ_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION), re.I | re.S)


def looks_like_injection(text):
    return bool(_INJ_RE.search(text or ""))


# ---- outgoing ----------------------------------------------------------------
_URL = re.compile(r"(https?://|www\.|\b[a-z0-9-]+\.(com|org|net|io|edu|ai|dev|app|xyz|ly)\b/?)", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_KEYLIKE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|\b\d{3,6}~[A-Za-z0-9]{20,}|Bearer\s+\S{12,}|[A-Za-z0-9_\-]{40,})")


def shingles(s, k=3):
    w = norm(s).split()
    return {" ".join(w[i:i + k]) for i in range(max(1, len(w) - k + 1))}


def similarity(a, b):
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / max(1, len(sa | sb))


def check_outgoing(body, secrets=(), prior=(), min_len=40, max_len=1500, dup_threshold=0.5):
    """Return a list of problems; empty list means OK to post."""
    problems = []
    if not (min_len <= len(body) <= max_len):
        problems.append(f"length {len(body)} outside [{min_len},{max_len}]")
    for s in secrets:
        if s and len(s) >= 6 and s in body:
            problems.append("contains a secret value")
    if _KEYLIKE.search(body):
        problems.append("looks like a key/token")
    if _URL.search(body):
        problems.append("contains a link")
    if _EMAIL.search(body):
        problems.append("contains an email address")
    for p in prior:
        if similarity(body, p) >= dup_threshold:
            problems.append("too similar to an earlier post")
            break
    return problems


def redact(s, secrets):
    s = str(s)
    for sec in secrets:
        if sec and len(sec) >= 6:
            s = s.replace(sec, "[REDACTED]")
    return re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", s)
