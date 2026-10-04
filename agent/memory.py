"""Persistent local memory (SQLite). Survives restarts; the single source of truth for idempotency."""
import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries(
  id TEXT PRIMARY KEY, parent_id TEXT, user_id TEXT, author TEXT, text TEXT, created_at TEXT,
  first_seen REAL, is_own INTEGER DEFAULT 0, handled INTEGER DEFAULT 0, flagged INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS actions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, parent_id TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL, body_norm TEXT NOT NULL, status TEXT NOT NULL, canvas_id TEXT,
  attempts INTEGER DEFAULT 0, created REAL, updated REAL, detail TEXT,
  UNIQUE(kind, parent_id, body_norm));
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, ended REAL, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""
LIVE = ("pending", "posted", "verified")  # statuses meaning the post may exist on Canvas


class Memory:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        if path != ":memory:":
            os.chmod(path, 0o600)

    # -- meta --
    def get(self, key, default=None):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key, value):
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, str(value)))
        self.db.commit()

    # -- failure / stop rule --
    def failures(self):
        return int(self.get("consecutive_failures", 0))

    def bump_failure(self, limit, reason):
        n = self.failures() + 1
        self.set("consecutive_failures", n)
        if n >= limit:
            self.set("halted", f"{n} consecutive failed cycles; last: {reason}")
        return n

    def clear_failures(self):
        self.set("consecutive_failures", 0)

    def halted(self):
        return self.get("halted") or ""

    def unhalt(self):
        self.db.execute("DELETE FROM meta WHERE key='halted'")
        self.set("consecutive_failures", 0)

    # -- cycles --
    def start_cycle(self, now):
        cur = self.db.execute("INSERT INTO cycles(started) VALUES(?)", (now,))
        self.db.commit()
        return cur.lastrowid

    def end_cycle(self, cid, now, outcome, detail=""):
        self.db.execute("UPDATE cycles SET ended=?, outcome=?, detail=? WHERE id=?", (now, outcome, detail[:500], cid))
        self.db.commit()

    # -- entries seen --
    def ingest(self, entries, self_id, now, is_flagged):
        own_ids = {r["canvas_id"] for r in self.db.execute("SELECT canvas_id FROM actions WHERE canvas_id IS NOT NULL")}
        for e in entries:
            own = int(e.user_id == str(self_id) or e.id in own_ids)
            self.db.execute(
                "INSERT INTO entries(id,parent_id,user_id,author,text,created_at,first_seen,is_own,flagged) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET is_own=MAX(is_own,excluded.is_own)",
                (e.id, e.parent_id, e.user_id, e.author, e.text, e.created_at, now, own, int(is_flagged(e.text))))
        self.db.commit()

    def unhandled_foreign(self):
        return self.db.execute("SELECT * FROM entries WHERE is_own=0 AND handled=0 ORDER BY id").fetchall()

    def mark_handled(self, ids):
        self.db.executemany("UPDATE entries SET handled=1 WHERE id=?", [(i,) for i in ids])
        self.db.commit()

    def unhandle(self, entry_id):
        """A planned reply that never landed makes its target eligible for reconsideration."""
        if entry_id:
            self.db.execute("UPDATE entries SET handled=0 WHERE id=?", (entry_id,))
            self.db.commit()

    def own_texts(self, limit=12):
        rows = self.db.execute("SELECT body FROM actions WHERE status IN ('pending','posted','verified') "
                               "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [r["body"] for r in rows]

    # -- actions (write-ahead intent log) --
    def begin_action(self, kind, parent_id, body, body_norm, now):
        """Record intent BEFORE posting. Returns action id, or None if an equivalent live action exists."""
        row = self.db.execute("SELECT id,status FROM actions WHERE kind=? AND parent_id=? AND body_norm=?",
                              (kind, parent_id, body_norm)).fetchone()
        if row:
            if row["status"] in LIVE:
                return None
            self.db.execute("UPDATE actions SET status='pending', updated=? WHERE id=?", (now, row["id"]))
            self.db.commit()
            return row["id"]
        cur = self.db.execute("INSERT INTO actions(kind,parent_id,body,body_norm,status,created,updated) "
                              "VALUES(?,?,?,?, 'pending',?,?)", (kind, parent_id, body, body_norm, now, now))
        self.db.commit()
        return cur.lastrowid

    def set_status(self, aid, status, now, canvas_id=None, detail=""):
        self.db.execute("UPDATE actions SET status=?, updated=?, canvas_id=COALESCE(?,canvas_id), detail=? WHERE id=?",
                        (status, now, canvas_id, detail[:300], aid))
        self.db.commit()

    def bump_attempt(self, aid):
        self.db.execute("UPDATE actions SET attempts=attempts+1 WHERE id=?", (aid,))
        self.db.commit()

    def get_action(self, aid):
        return self.db.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()

    def pending(self):
        return self.db.execute("SELECT * FROM actions WHERE status IN ('pending','posted') ORDER BY id").fetchall()

    def has_acted_on(self, parent_id):
        return self.db.execute(f"SELECT 1 FROM actions WHERE kind='reply' AND parent_id=? AND status IN "
                               f"{LIVE!r}", (parent_id,)).fetchone() is not None

    def writes_since(self, ts):
        return self.db.execute(f"SELECT COUNT(*) c FROM actions WHERE status IN {LIVE!r} AND created>=?",
                               (ts,)).fetchone()["c"]

    def last_new_thread(self):
        r = self.db.execute(f"SELECT MAX(created) m FROM actions WHERE kind='thread' AND status IN {LIVE!r}").fetchone()
        return r["m"]
