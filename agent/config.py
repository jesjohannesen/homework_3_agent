import os
from dataclasses import dataclass, field
from pathlib import Path

MAX_POSTS_PER_HOUR_HARD = 3  # course rule; config can only go lower


_HERMES_WHITELIST = {"CANVAS_TOKEN", "OPENROUTER_API_KEY"}


def load_dotenv(path=".env"):
    for p in (Path(path), Path.home() / ".hermes" / ".env"):
        if not p.exists():
            continue
        whitelist = _HERMES_WHITELIST if p.match(str(Path.home() / ".hermes" / ".env")) else None
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if v and (whitelist is None or k in whitelist):
                os.environ.setdefault(k, v)


@dataclass(frozen=True)
class Config:
    canvas_token: str = field(repr=False)
    openrouter_key: str = field(repr=False)
    canvas_base: str = "https://canvas.mit.edu"
    openrouter_base: str = "https://openrouter.ai/api/v1"
    model: str = "deepseek/deepseek-v4-flash"
    agent_name: str = "FeynBro"
    course_hint: str = "MAS.665"
    course_id: str = ""
    topic_id: str = ""
    topic_title: str = "Homework 3: Agent Discussion Forum"
    state_dir: str = "state"
    interval_hours: float = 3.0
    max_posts_per_cycle: int = 2
    max_posts_per_hour: int = 2
    max_posts_per_day: int = 8
    new_thread_min_hours: float = 12.0
    max_consecutive_failures: int = 3
    dry_run: bool = False

    @classmethod
    def from_env(cls, require_secrets=True, **over):
        load_dotenv()
        e = os.environ
        tok, key = e.get("CANVAS_TOKEN", ""), e.get("OPENROUTER_API_KEY", "")
        if require_secrets and not (tok and key):
            raise SystemExit("Set CANVAS_TOKEN and OPENROUTER_API_KEY in the environment (see .env.example).")
        kw = dict(
            canvas_token=tok, openrouter_key=key,
            canvas_base=e.get("CANVAS_BASE_URL", cls.canvas_base).rstrip("/"),
            model=e.get("HW3_MODEL", cls.model),
            agent_name=e.get("HW3_AGENT_NAME", cls.agent_name),
            course_id=e.get("CANVAS_COURSE_ID", ""), topic_id=e.get("CANVAS_TOPIC_ID", ""),
            state_dir=e.get("HW3_STATE_DIR", cls.state_dir),
            interval_hours=float(e.get("HW3_INTERVAL_HOURS", cls.interval_hours)),
        )
        kw.update(over)
        cfg = cls(**kw)
        if cfg.max_posts_per_hour > MAX_POSTS_PER_HOUR_HARD:
            raise SystemExit(f"max_posts_per_hour cannot exceed {MAX_POSTS_PER_HOUR_HARD} (course rule).")
        return cfg

    @property
    def secrets(self):
        return [s for s in (self.canvas_token, self.openrouter_key) if s]

    @property
    def db_path(self):
        return str(Path(self.state_dir) / "agent.db")

    @property
    def log_path(self):
        return str(Path(self.state_dir) / "cycles.jsonl")

    @property
    def lock_path(self):
        return str(Path(self.state_dir) / "agent.lock")
