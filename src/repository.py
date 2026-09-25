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
        open_renewal_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in RENEWAL_STATES[:-1])
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
                CREATE TABLE IF NOT EXISTS renewals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    proposed_capacity REAL NOT NULL,
                    effective_date TEXT NOT NULL,
                    materials TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL CHECK(status IN ({renewal_statuses})),
                    review_comment TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_renewals_open
                    ON renewals(item_id) WHERE status IN ({open_renewal_statuses});
                CREATE TABLE IF NOT EXISTS permit_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    renewal_id INTEGER NOT NULL REFERENCES renewals(id),
                    version INTEGER NOT NULL,
                    capacity REAL NOT NULL,
                    effective_date TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, version)
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

    def close_record(self, record_id: int, actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=?", (record_id,)
            )
            if cur.rowcount == 0:
                raise NotFoundError("记录不存在")
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def update_item_capacity(self, item_id: int, capacity: float) -> None:
        now = utc_now()
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE items SET quantity=?, version=version+1, updated_at=? WHERE id=?",
                (capacity, now, item_id),
            )

    @staticmethod
    def _renewal(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["materials"] = json.loads(result["materials"])
        return result

    def create_renewal(self, item_id: int, proposed_capacity: float,
                       effective_date: str, materials: list, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO renewals(item_id, proposed_capacity, effective_date,
                       materials, status, version, created_by, created_at, updated_at)
                       VALUES(?,?,?,?, 'draft', 1, ?, ?, ?)""",
                    (item_id, proposed_capacity, effective_date,
                     json.dumps(materials, ensure_ascii=False), actor, now, now),
                )
                renewal_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该许可单已有未结束的续期申请") from exc
        return self.get_renewal(renewal_id)

    def get_renewal(self, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM renewals WHERE id=?", (renewal_id,)).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return self._renewal(row)

    def list_renewals(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM renewals WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self._renewal(row) for row in rows]

    def update_renewal(self, renewal_id: int, proposed_capacity: float,
                       effective_date: str, materials: list, expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET proposed_capacity=?, effective_date=?, materials=?,
                   version=version+1, updated_at=?, review_comment=NULL, reviewed_by=NULL,
                   reviewed_at=NULL
                   WHERE id=? AND version=? AND status IN ('draft','returned')""",
                (proposed_capacity, effective_date,
                 json.dumps(materials, ensure_ascii=False), now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                row = self.conn.execute("SELECT status FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if row is None:
                    raise NotFoundError("续期申请不存在")
                if row["status"] not in ("draft", "returned"):
                    raise ConflictError("只有草稿或退回的申请才能补充修改")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_renewal(renewal_id)

    def submit_renewal(self, renewal_id: int, expected_version: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE renewals SET status='submitted', version=version+1, updated_at=?,
                   review_comment=NULL, reviewed_by=NULL, reviewed_at=NULL
                   WHERE id=? AND version=? AND status IN ('draft','returned')""",
                (now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                row = self.conn.execute("SELECT status FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if row is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_renewal(renewal_id)

    def transition_renewal(self, renewal_id: int, target: str, expected_version: int,
                           actor: str, comment: Optional[str],
                           from_statuses: tuple) -> Dict[str, Any]:
        now = utc_now()
        placeholders = ",".join("?" for _ in from_statuses)
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"""UPDATE renewals SET status=?, version=version+1, updated_at=?,
                   review_comment=?, reviewed_by=?, reviewed_at=?
                   WHERE id=? AND version=? AND status IN ({placeholders})""",
                (target, now, comment, actor, now, renewal_id, expected_version,
                 *from_statuses),
            )
            if cur.rowcount == 0:
                row = self.conn.execute("SELECT status FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if row is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_renewal(renewal_id)

    def approve_renewal(self, renewal_id: int, item_id: int, capacity: float,
                        effective_date: str, expires_at: str, expected_version: int,
                        actor: str, comment: Optional[str]) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM permit_versions WHERE item_id=?",
                (item_id,),
            ).fetchone()
            next_version = int(row["v"]) + 1
            cur = self.conn.execute(
                """UPDATE renewals SET status='approved', version=version+1, updated_at=?,
                   review_comment=?, reviewed_by=?, reviewed_at=?
                   WHERE id=? AND version=? AND status='submitted'""",
                (now, comment, actor, now, renewal_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM renewals WHERE id=?", (renewal_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("续期申请不存在")
                raise ConflictError("版本冲突，请刷新后重试")
            self.conn.execute(
                """INSERT INTO permit_versions(item_id, renewal_id, version, capacity,
                   effective_date, expires_at, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (item_id, renewal_id, next_version, capacity, effective_date,
                 expires_at, actor, now),
            )
            self.conn.execute(
                """UPDATE items SET quantity=?, version=version+1, updated_at=? WHERE id=?""",
                (capacity, now, item_id),
            )
        return self.get_renewal(renewal_id)

    def list_permit_versions(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM permit_versions WHERE item_id=? ORDER BY version DESC",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
