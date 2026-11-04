"""Disposable SQLite snapshot: bounded body storage and a fixed processing queue."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path


class DiskSnapshot:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="onboarding-snapshot-")
        self.path = Path(self.directory.name) / "records.sqlite3"
        self.db = None
        try:
            self.db = sqlite3.connect(self.path)
            self.db.execute("PRAGMA cache_size=-2048")  # About 2 MiB, not the whole snapshot.
            self.db.execute("PRAGMA temp_store=FILE")
            self.db.execute("""CREATE TABLE records (
                seq INTEGER PRIMARY KEY, table_name TEXT NOT NULL,
                record_id TEXT NOT NULL, fields TEXT NOT NULL,
                UNIQUE(table_name, record_id))""")
        except BaseException:
            self.close()
            raise

    def table(self, name):
        return DiskRecords(self.db, name)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self.directory.cleanup()


class DiskRecords:
    def __init__(self, db, name):
        self.db, self.name = db, name

    def __iter__(self):
        cursor = self.db.execute(
            "SELECT record_id, fields FROM records WHERE table_name=? ORDER BY seq", (self.name,))
        try:
            for record_id, fields in cursor:
                yield {"record_id": record_id, "fields": json.loads(fields)}
        finally:
            cursor.close()

    def __len__(self):
        return self.db.execute("SELECT COUNT(*) FROM records WHERE table_name=?", (self.name,)).fetchone()[0]

    def append(self, record):
        self.db.execute("INSERT INTO records(table_name, record_id, fields) VALUES (?, ?, ?)",
                        (self.name, record["record_id"], json.dumps(record["fields"], ensure_ascii=False)))

    def get(self, record_id):
        row = self.db.execute("SELECT fields FROM records WHERE table_name=? AND record_id=?",
                              (self.name, record_id)).fetchone()
        return {"record_id": record_id, "fields": json.loads(row[0])} if row else None

    def replace(self, record):
        self.db.execute("UPDATE records SET fields=? WHERE table_name=? AND record_id=?",
                        (json.dumps(record["fields"], ensure_ascii=False), self.name, record["record_id"]))

    def identities(self, names):
        # Project inside SQLite so long descriptions never enter Python memory.
        columns = ", ".join("json_extract(fields, ?)" for _ in names)
        paths = [f'$."{name}"' for name in names]
        cursor = self.db.execute(
            f"SELECT record_id, {columns} FROM records WHERE table_name=? ORDER BY seq", (*paths, self.name))
        try:
            return [{"record_id": row[0], "fields": dict(zip(names, (value or "" for value in row[1:])))}
                    for row in cursor]
        finally:
            cursor.close()

    def pending_count(self):
        return self.db.execute("""SELECT COUNT(*) FROM records WHERE table_name=?
            AND COALESCE(json_extract(fields, '$."AI处理"'), '') != 'AI已处理'""", (self.name,)).fetchone()[0]

    def pending_snapshot(self, time_key, limit=None):
        # Freeze IDs separately: status updates cannot shrink/reorder this queue.
        self.db.execute("DROP TABLE IF EXISTS pending_queue")
        self.db.execute("CREATE TABLE pending_queue (seq INTEGER PRIMARY KEY, record_id TEXT, received TEXT)")
        for record in self:
            if record["fields"].get("AI处理") != "AI已处理":
                received = time_key(record).isoformat(timespec="microseconds")
                self.db.execute("INSERT INTO pending_queue(record_id, received) VALUES (?, ?)",
                                (record["record_id"], received))
        self.db.execute("CREATE INDEX pending_order ON pending_queue(received, seq)")
        self.db.commit()
        count = self.db.execute("SELECT COUNT(*) FROM pending_queue").fetchone()[0]
        total = count if limit is None else min(count, limit)

        def records():
            # Do not hold a SQLite cursor across AI calls or writes. Exceptions
            # can retain generator frames, especially through test/mock tracebacks.
            last_time, last_seq = "", 0
            for offset in range(0, total, 50):
                cursor = self.db.execute(
                    """SELECT received, seq, record_id FROM pending_queue
                       WHERE (received, seq) > (?, ?) ORDER BY received, seq LIMIT ?""",
                    (last_time, last_seq, min(50, total - offset)))
                try:
                    batch = cursor.fetchall()
                finally:
                    cursor.close()
                for last_time, last_seq, record_id in batch:
                    yield self.get(record_id)
        return total, records()
