import base64
import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .protocol import MEDIA_METHODS, READ_CACHE


class Store:
    def __init__(self, directory, media_limit=3 * 1024**3):
        if not isinstance(media_limit, int) or isinstance(media_limit, bool) or media_limit < 0:
            raise ValueError("invalid media cache limit")
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("state directory must not be a symlink")
        os.chmod(self.root, 0o700)
        self.files = self.root / "media"
        self.files.mkdir(exist_ok=True, mode=0o700)
        if self.files.is_symlink():
            raise ValueError("media directory must not be a symlink")
        self.media_limit = media_limit
        self.db = sqlite3.connect(self.root / "archive.sqlite3")
        os.chmod(self.root / "archive.sqlite3", 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                body TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(client TEXT NOT NULL, id TEXT NOT NULL,
                digest TEXT NOT NULL, response TEXT, PRIMARY KEY(client,id));
            CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, method TEXT NOT NULL,
                body TEXT, file TEXT, size INTEGER NOT NULL, accessed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS versions(peer TEXT NOT NULL, id INTEGER NOT NULL,
                digest TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(peer,id,digest));
            CREATE TABLE IF NOT EXISTS tombstones(peer TEXT NOT NULL, id INTEGER NOT NULL,
                created REAL NOT NULL, PRIMARY KEY(peer,id));
            CREATE TABLE IF NOT EXISTS history_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, message TEXT NOT NULL, created INTEGER NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('epoch',?)", (uuid.uuid4().hex,))
        self.db.commit()
        self.epoch = self.db.execute("SELECT value FROM settings WHERE key='epoch'").fetchone()[0]
        self.reconcile()

    def close(self):
        self.db.close()

    def begin_request(self, client, request_id, digest):
        row = self.db.execute("SELECT digest,response FROM requests WHERE client=? AND id=?",
                              (client, request_id)).fetchone()
        if row:
            if row[0] != digest:
                raise ValueError("request id reused with different content")
            return ("done", json.loads(row[1])) if row[1] else ("unknown", None)
        with self.db:
            self.db.execute("INSERT INTO requests VALUES(?,?,?,NULL)", (client, request_id, digest))
        return "new", None

    def finish_request(self, client, request_id, frame):
        with self.db:
            self.db.execute("UPDATE requests SET response=? WHERE client=? AND id=?",
                            (json.dumps(frame), client, request_id))

    def append_update(self, body, messages=(), deleted=(), checkpoint=None):
        with self.db:
            row = self.db.execute("INSERT INTO events(body,created) VALUES(?,?)", (body, time.time()))
            seq = row.lastrowid
            self._archive(messages, deleted)
            if checkpoint is not None:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('sync',?)", (json.dumps(checkpoint),))
        return seq

    def archive(self, messages=(), deleted=()):
        with self.db:
            self._archive(messages, deleted)

    def _archive(self, messages, deleted):
        for message in messages:
            raw = json.dumps(message, sort_keys=True, separators=(",", ":"))
            previous = self.db.execute("SELECT body FROM versions WHERE peer=? AND id=? ORDER BY created DESC,rowid DESC LIMIT 1",
                                       (message["peer"], message["id"])).fetchone()
            inserted = self.db.execute("INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?)",
                            (message["peer"], message["id"], hashlib.sha256(raw.encode()).hexdigest(),
                             raw, time.time())).rowcount
            if inserted and previous:
                before = json.loads(previous[0])
                if (before.get("text"), before.get("edit_date", 0)) != (message.get("text"), message.get("edit_date", 0)):
                    self.db.execute("INSERT INTO history_events(kind,message,created) VALUES('edited',?,?)",
                                    (previous[0], int(time.time())))
            if inserted and self.history(message["peer"], message["id"])["deleted"]:
                self.db.execute("INSERT INTO history_events(kind,message,created) VALUES('deleted',?,?)", (raw, int(time.time())))
        for deletion in deleted:
            peer = deletion["peer"]
            for mid in deletion["ids"]:
                if peer == "nonchannel":
                    peers = [r[0] for r in self.db.execute(
                        "SELECT DISTINCT peer FROM versions WHERE id=? AND peer NOT LIKE 'channel:%'", (mid,))]
                    peers.append("nonchannel")
                else:
                    peers = [peer]
                for key in peers:
                    inserted = self.db.execute("INSERT OR IGNORE INTO tombstones VALUES(?,?,?)", (key, mid, time.time())).rowcount
                    previous = self.db.execute("SELECT body FROM versions WHERE peer=? AND id=? ORDER BY created DESC,rowid DESC LIMIT 1", (key, mid)).fetchone()
                    if inserted and previous:
                        self.db.execute("INSERT INTO history_events(kind,message,created) VALUES('deleted',?,?)", (previous[0], int(time.time())))

    def history_events(self, after, limit=32):
        return [{"type": "archive", "seq": str(seq), "kind": kind, "message": json.loads(message), "created": created}
                for seq, kind, message, created in self.db.execute(
                    "SELECT seq,kind,message,created FROM history_events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]

    def history(self, peer, message_id):
        rows = self.db.execute("SELECT body FROM versions WHERE peer=? AND id=? ORDER BY created,rowid",
                               (peer, message_id)).fetchall()
        deleted = self.db.execute("SELECT 1 FROM tombstones WHERE id=? AND (peer=? OR (peer='nonchannel' AND ? NOT LIKE 'channel:%'))",
                                  (message_id, peer, peer)).fetchone() is not None
        return {"versions": [json.loads(r[0]) for r in rows], "deleted": deleted}

    def updates(self, after, limit=100):
        return [{"type": "update", "seq": str(seq), "body": body} for seq, body in self.db.execute(
            "SELECT seq,body FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))]

    def latest_seq(self):
        return self.db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]

    def cached(self, key):
        row = self.db.execute("SELECT body,file FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        if row[1]:
            try:
                data = (self.files / row[1]).read_bytes()
            except FileNotFoundError:
                with self.db:
                    self.db.execute("DELETE FROM cache WHERE key=?", (key,))
                return None
            result = base64.b64encode(data).decode()
        else:
            result = row[0]
        with self.db:
            self.db.execute("UPDATE cache SET accessed=? WHERE key=?", (time.time(), key))
        return result

    def cache(self, key, method, body):
        if method not in READ_CACHE:
            return
        data = base64.b64decode(body, validate=True)
        if len(data) >= 4 and data[:4] == b"\x19\xca\x44\x21":
            return
        if method not in MEDIA_METHODS:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?,NULL,?,?)",
                                (key, method, body, len(data), time.time()))
            return
        if len(data) > self.media_limit:
            return
        self.evict(self.media_limit - len(data), replacing=key)
        name = hashlib.sha256(key.encode()).hexdigest()
        path = self.files / name
        temporary = self.files / ("tmp-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,NULL,?,?,?)",
                            (key, method, name, len(data), time.time()))

    def evict(self, limit=None, replacing=None):
        limit = self.media_limit if limit is None else limit
        rows = self.db.execute("SELECT key,file,size FROM cache WHERE file IS NOT NULL ORDER BY accessed,key").fetchall()
        total = sum(r[2] for r in rows)
        for key, name, size in rows:
            if total <= limit and key != replacing:
                continue
            (self.files / name).unlink(missing_ok=True)
            with self.db:
                self.db.execute("DELETE FROM cache WHERE key=?", (key,))
            total -= size

    def reconcile(self):
        expected = {r[0] for r in self.db.execute("SELECT file FROM cache WHERE file IS NOT NULL")}
        for path in self.files.iterdir():
            if path.name not in expected and (path.is_file() or path.is_symlink()):
                path.unlink()
        with self.db:
            for name in expected:
                path = self.files / name
                if not path.is_file() or path.is_symlink():
                    self.db.execute("DELETE FROM cache WHERE file=?", (name,))
        self.evict()
