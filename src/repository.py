from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, RENEWAL_STATES, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        renewal_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in RENEWAL_STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    permit_version INTEGER NOT NULL DEFAULT 1,
                    permit_expires_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS renewal_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    proposed_capacity REAL NOT NULL,
                    effective_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ({renewal_statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    review_comment TEXT,
                    issued_version INTEGER,
                    issued_expires_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_open
                    ON renewal_applications(item_id)
                    WHERE status NOT IN ('approved');
                CREATE TABLE IF NOT EXISTS renewal_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    renewal_id INTEGER NOT NULL
                        REFERENCES renewal_applications(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(renewal_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
            """)
        self._migrate()

    def _migrate(self) -> None:
        with self._lock, self.conn:
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(items)")}
            if "permit_version" not in columns:
                self.conn.execute(
                    "ALTER TABLE items ADD COLUMN permit_version INTEGER NOT NULL DEFAULT 1")
            if "permit_expires_at" not in columns:
                self.conn.execute("ALTER TABLE items ADD COLUMN permit_expires_at TEXT")

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def create_renewal(self, item_id: int, proposed_capacity: float,
                       effective_date: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewal_applications(item_id, proposed_capacity,
                       effective_date, status, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, proposed_capacity, effective_date, RENEWAL_STATES[0], 1,
                     actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该许可单已存在未结束的续期申请") from exc
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewal_applications WHERE id=?", (renewal_id,)).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return dict(row)

    def list_renewals(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM renewal_applications WHERE item_id=? ORDER BY id DESC",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_material(self, renewal_id: int, kind: str, detail: str,
                     external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_renewal(renewal_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewal_materials(renewal_id, kind, detail, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?)""",
                    (renewal_id, kind, detail, external_ref, actor, now),
                )
                material_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("材料唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewal_materials WHERE id=?", (material_id,)).fetchone()
        return dict(row)

    def list_materials(self, renewal_id: int) -> List[Dict[str, Any]]:
        self.get_renewal(renewal_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM renewal_materials WHERE renewal_id=? ORDER BY id",
                (renewal_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def transition_renewal(self, renewal_id: int, target: str, expected_version: int,
                           actor: str, comment: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewal_applications
                   SET status=?, review_comment=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, comment, now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewal_applications WHERE id=?", (renewal_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_renewal(renewal_id)

    def amend_renewal(self, renewal_id: int, proposed_capacity: float,
                      effective_date: str, expected_version: int,
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewal_applications
                   SET proposed_capacity=?, effective_date=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (proposed_capacity, effective_date, now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewal_applications WHERE id=?", (renewal_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_renewal(renewal_id)

    def approve_renewal(self, renewal_id: int, expected_version: int,
                        new_permit_version: int, expires_at: str,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewal_applications
                   SET status='approved', version=version+1, issued_version=?,
                       issued_expires_at=?, updated_at=?
                   WHERE id=? AND version=?""",
                (new_permit_version, expires_at, now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM renewal_applications WHERE id=?", (renewal_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            row = self.conn.execute(
                "SELECT item_id FROM renewal_applications WHERE id=?", (renewal_id,)).fetchone()
            self.conn.execute(
                """UPDATE items SET permit_version=?, permit_expires_at=?,
                   version=version+1, updated_at=? WHERE id=?""",
                (new_permit_version, expires_at, now, int(row["item_id"])),
            )
        return self.get_renewal(renewal_id)

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
