import json
import sqlite3
from datetime import datetime, timezone

from . import rules
from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_number TEXT UNIQUE,
                    item_id INTEGER NOT NULL,
                    station_id TEXT NOT NULL,
                    frequency_mhz REAL NOT NULL,
                    observed_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    report_count INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, station_id, frequency_mhz, observed_at),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                """
            )
        finally:
            conn.close()

    def _row_to_batch(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def get_batch(self, batch_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFoundError("batch_not_found", "测量批次不存在")
            return self._row_to_batch(row)
        finally:
            conn.close()

    def list_batches(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM batches WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            return [self._row_to_batch(row) for row in rows]
        finally:
            conn.close()

    def upsert_measurement(self, item_id, measurement, actor, role):
        """Record a monitoring-station report, merging duplicates into a batch.

        Reports sharing ``(item_id, station_id, frequency_mhz, observed_at)``
        are merged into the same batch instead of creating a new one. A
        client-supplied ``batch_number`` that already exists is returned
        idempotently so a failed write can be retried without adding records.
        Any batch update invalidates the item's derived conclusions.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item_row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            batch_number = measurement.get("batch_number")
            if batch_number:
                existing = conn.execute("SELECT * FROM batches WHERE batch_number=?", (batch_number,)).fetchone()
                if existing is not None:
                    conn.execute("ROLLBACK")
                    return self._row_to_batch(existing), self.get_item(item_id), False, False
            now = now_iso()
            batch_row = conn.execute(
                "SELECT * FROM batches WHERE item_id=? AND station_id=? AND frequency_mhz=? AND observed_at=?",
                (item_id, measurement["station_id"], measurement["frequency_mhz"], measurement["observed_at"]),
            ).fetchone()
            if batch_row is None:
                cur = conn.execute(
                    "INSERT INTO batches(batch_number,item_id,station_id,frequency_mhz,observed_at,payload,report_count,version,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,1,1,?,?)",
                    (batch_number, item_id, measurement["station_id"], measurement["frequency_mhz"], measurement["observed_at"], canonical_json(measurement), now, now),
                )
                batch_id = cur.lastrowid
                if not batch_number:
                    conn.execute("UPDATE batches SET batch_number=? WHERE id=?", ("B-%d" % batch_id, batch_id))
                created = True
            else:
                batch_id = batch_row["id"]
                merged = dict(json.loads(batch_row["payload"]))
                merged.update(measurement)
                conn.execute(
                    "UPDATE batches SET payload=?,report_count=report_count+1,version=version+1,updated_at=? WHERE id=?",
                    (canonical_json(merged), now, batch_id),
                )
                created = False
            item_payload = json.loads(item_row["payload"])
            new_payload, invalidated = rules.invalidate_on_measurement(item_payload, measurement)
            new_status = rules.regress_on_measurement(item_row["status"])
            new_version = int(item_row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, new_version, canonical_json(new_payload), now, item_id),
            )
            self.append_audit(
                conn,
                item_id,
                "measurement_recorded",
                actor,
                role,
                {"batch_id": batch_id, "created": created, "invalidated": invalidated},
            )
            conn.execute("COMMIT")
            return self.get_batch(batch_id), self.get_item(item_id), created, bool(invalidated)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def merge_batch(self, item_id, batch_id, theirs, base_payload, expected_version, actor, role):
        """Apply a concurrent batch edit with field-level 3-way merge.

        Non-conflicting fields are applied; fields both sides changed to
        different values are returned as conflicts for manual choice. The
        batch update invalidates the item's derived conclusions.
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute("SELECT * FROM batches WHERE id=? AND item_id=?", (batch_id, item_id)).fetchone()
            if batch is None:
                raise NotFoundError("batch_not_found", "测量批次不存在")
            current = json.loads(batch["payload"])
            if int(batch["version"]) == int(expected_version):
                merged = dict(current)
                merged.update(theirs)
                conflicts = []
            else:
                if base_payload is None:
                    raise ConflictError("version_conflict", "批次已被其他操作更新，请重新读取后合并")
                merged, conflicts = rules.three_way_merge(base_payload, theirs, current)
            now = now_iso()
            new_version = int(batch["version"]) + 1
            conn.execute(
                "UPDATE batches SET payload=?,version=?,updated_at=? WHERE id=?",
                (canonical_json(merged), new_version, now, batch_id),
            )
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            item_payload = json.loads(item_row["payload"])
            new_payload, invalidated = rules.invalidate_on_measurement(item_payload, merged)
            new_status = rules.regress_on_measurement(item_row["status"])
            conn.execute(
                "UPDATE items SET status=?,version=version+1,payload=?,updated_at=? WHERE id=?",
                (new_status, canonical_json(new_payload), now, item_id),
            )
            self.append_audit(
                conn,
                item_id,
                "batch_merged",
                actor,
                role,
                {"batch_id": batch_id, "conflicts": [c["field"] for c in conflicts], "invalidated": invalidated},
            )
            conn.execute("COMMIT")
            return self.get_batch(batch_id), conflicts
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
