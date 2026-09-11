import csv
import io
import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS days (
                day TEXT PRIMARY KEY, baseline TEXT NOT NULL, halted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS intents (
                cid TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL,
                day TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, order_id TEXT, snapshot TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
                created REAL NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
                color INTEGER NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS closes (
                symbol TEXT PRIMARY KEY, reason TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS equity (
                bucket INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, equity TEXT NOT NULL,
                cash TEXT NOT NULL, day_pl TEXT NOT NULL);
        """)

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def session(self, day, baseline):
        self.db.execute("INSERT OR IGNORE INTO days(day,baseline) VALUES (?,?)", (day, str(baseline)))
        return dict(self.db.execute("SELECT * FROM days WHERE day=?", (day,)).fetchone())

    def halt(self, day):
        self.db.execute("UPDATE days SET halted=1 WHERE day=?", (day,))

    def entries(self, day):
        # Count submissions, including rejected or unknown attempts, conservatively.
        return self.db.execute("SELECT COUNT(*) FROM intents WHERE day=? AND kind='entry'", (day,)).fetchone()[0]

    def traded_symbol(self, day, symbol):
        return bool(self.db.execute("SELECT 1 FROM intents WHERE day=? AND symbol=? AND kind='entry'",
                                    (day, symbol)).fetchone())

    def reserve(self, cid, symbol, kind, day, payload):
        cur = self.db.execute("INSERT OR IGNORE INTO intents VALUES (?,?,?,?,?,?,?,NULL,NULL)",
                              (cid, symbol, kind, day, time.time(), json.dumps(payload), "unknown"))
        return cur.rowcount == 1

    def intents(self, active=False):
        suffix = " WHERE state IN ('unknown','active')" if active else ""
        return [dict(r) for r in self.db.execute("SELECT * FROM intents" + suffix + " ORDER BY created")]

    def intent(self, cid):
        row = self.db.execute("SELECT * FROM intents WHERE cid=?", (cid,)).fetchone()
        return dict(row) if row else None

    def order(self, cid, order, finished=False):
        self.db.execute("UPDATE intents SET state=?, order_id=?, snapshot=? WHERE cid=?",
                        ("finished" if finished else "active", order["id"], json.dumps(order), cid))

    def mark(self, cid, state):
        self.db.execute("UPDATE intents SET state=? WHERE cid=?", (state, cid))

    def event(self, key, title, body, color=0x3498DB):
        self.db.execute("INSERT OR IGNORE INTO events(event_key,created,title,body,color) VALUES(?,?,?,?,?)",
                        (key, time.time(), title, body[:3900], color))

    def pending_events(self, limit=5):
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE sent=0 ORDER BY id LIMIT ?", (limit,))]

    def acknowledge(self, event_id):
        self.db.execute("UPDATE events SET sent=1 WHERE id=?", (event_id,))

    def events(self, limit=15):
        return [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def request_close(self, symbol, reason):
        self.db.execute("INSERT OR IGNORE INTO closes VALUES (?,?,?)", (symbol, reason, time.time()))

    def closes(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM closes ORDER BY created")]

    def finish_close(self, symbol):
        self.db.execute("DELETE FROM closes WHERE symbol=?", (symbol,))

    def equity_point(self, timestamp, equity, cash, day_pl):
        self.db.execute("INSERT OR IGNORE INTO equity VALUES(?,?,?,?,?)",
                        (int(timestamp.timestamp()) // 300, timestamp.isoformat(), str(equity), str(cash), str(day_pl)))
        # Keep one year of 5-minute snapshots and 90 days of delivered alerts.
        self.db.execute("DELETE FROM equity WHERE bucket < ?", (int(time.time()) // 300 - 105120,))
        self.db.execute("DELETE FROM events WHERE sent=1 AND created < ?", (time.time() - 90*86400,))

    def export_csv(self, table):
        queries = {
            "konto": ("SELECT timestamp,equity,cash,day_pl FROM equity ORDER BY bucket",),
            "ereignisse": ("SELECT created,title,body FROM events ORDER BY id",),
            "orders": ("SELECT cid,symbol,kind,day,state,snapshot FROM intents ORDER BY created",),
        }
        cursor = self.db.execute(queries[table][0])
        text = io.StringIO(newline="")
        writer = csv.writer(text, delimiter=";")
        writer.writerow([d[0] for d in cursor.description])
        for row in cursor:
            # Treat broker-controlled text as text, not spreadsheet formulas.
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in row])
        return text.getvalue().encode("utf-8-sig")

    def close(self):
        self.db.close()
