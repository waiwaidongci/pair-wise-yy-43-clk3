from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import RESOURCE_KINDS, ConflictError, NotFoundError
from .rules import ENTITY, ID_PREFIX, STATES


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
        kinds = ",".join("'" + k.replace("'", "''") + "'" for k in RESOURCE_KINDS)
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
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_sign TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK(kind IN ({kinds})),
                    home_area TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
                    area TEXT NOT NULL,
                    leader TEXT NOT NULL,
                    task TEXT NOT NULL,
                    planned_start TEXT NOT NULL,
                    planned_end TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','released')),
                    released_at TEXT,
                    actual_hours REAL,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_assignments_active_resource
                    ON assignments(resource_id) WHERE status='active';
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

    @contextmanager
    def locked(self):
        with self._lock:
            yield

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

    def create_resource(self, call_sign: str, kind: str, home_area: str,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO resources(call_sign, kind, home_area, created_by, created_at)
                       VALUES(?,?,?,?,?)""",
                    (call_sign, kind, home_area, actor, now),
                )
                resource_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("呼号已存在") from exc
        return self.get_resource(resource_id)

    def get_resource(self, resource_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return dict(row)

    def get_resource_by_call_sign(self, call_sign: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM resources WHERE call_sign=?", (call_sign,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return dict(row)

    def list_resources(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.*, a.id AS active_assignment_id, a.item_id AS active_item_id,
                          a.area AS active_area
                   FROM resources r
                   LEFT JOIN assignments a
                       ON a.resource_id=r.id AND a.status='active'
                   ORDER BY r.call_sign"""
            ).fetchall()
        result = []
        for row in rows:
            resource = dict(row)
            on_duty = resource.pop("active_assignment_id") is not None
            active_item_id = resource.pop("active_item_id")
            active_area = resource.pop("active_area")
            resource["on_duty"] = on_duty
            resource["current_area"] = active_area if on_duty else resource["home_area"]
            resource["active_item_id"] = active_item_id if on_duty else None
            result.append(resource)
        return result

    def create_assignment(self, item_id: int, resource_id: int, area: str,
                          leader: str, task: str, planned_start: str,
                          planned_end: str, external_ref: Optional[str],
                          actor: str, audit_detail: Dict[str, Any]) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO assignments(item_id, resource_id, area, leader, task,
                       planned_start, planned_end, status, external_ref, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,'active',?,?,?)""",
                    (item_id, resource_id, area, leader, task, planned_start,
                     planned_end, external_ref, actor, now),
                )
                assignment_id = int(cur.lastrowid)
                self._insert_audit("dispatch", ENTITY, item_id, actor,
                                   dict(audit_detail, assignment_id=assignment_id))
        except sqlite3.IntegrityError as exc:
            if "ux_assignments_active_resource" in str(exc):
                raise ConflictError("资源尚未撤收，不能改派") from exc
            raise ConflictError("调派唯一标识已存在") from exc
        return self.get_assignment(assignment_id)

    def get_assignment(self, assignment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                """SELECT a.*, r.call_sign, r.kind AS resource_kind, r.home_area
                   FROM assignments a JOIN resources r ON r.id=a.resource_id
                   WHERE a.id=?""",
                (assignment_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("调派记录不存在")
        return dict(row)

    def release_assignment(self, assignment_id: int, released_at: str,
                           actual_hours: float, actor: str,
                           audit_detail: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise NotFoundError("调派记录不存在")
            if row["status"] != "active":
                raise ConflictError("该调派已撤收")
            self.conn.execute(
                """UPDATE assignments SET status='released', released_at=?, actual_hours=?
                   WHERE id=?""",
                (released_at, actual_hours, assignment_id),
            )
            self._insert_audit("release", ENTITY, row["item_id"], actor, audit_detail)
        return self.get_assignment(assignment_id)

    def list_assignments(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT a.*, r.call_sign, r.kind AS resource_kind, r.home_area
                   FROM assignments a JOIN resources r ON r.id=a.resource_id
                   WHERE a.item_id=? ORDER BY a.id""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_assignments(self, item_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT a.*, r.call_sign, r.kind AS resource_kind, r.home_area
                   FROM assignments a JOIN resources r ON r.id=a.resource_id
                   WHERE a.item_id=? AND a.status='active' ORDER BY a.id""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_assignments_by_item(self) -> Dict[int, List[Dict[str, Any]]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT a.*, r.call_sign, r.kind AS resource_kind, r.home_area
                   FROM assignments a JOIN resources r ON r.id=a.resource_id
                   WHERE a.status='active' ORDER BY a.id"""
            ).fetchall()
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(int(row["item_id"]), []).append(dict(row))
        return grouped

    def active_assignment_for_resource(self, resource_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM assignments WHERE resource_id=? AND status='active'",
                (resource_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def active_call_signs(self, item_id: int) -> List[str]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.call_sign FROM assignments a
                   JOIN resources r ON r.id=a.resource_id
                   WHERE a.item_id=? AND a.status='active' ORDER BY r.call_sign""",
                (item_id,),
            ).fetchall()
        return [str(row["call_sign"]) for row in rows]

    def _insert_audit(self, action: str, entity_type: str, entity_id: int,
                      actor: str, detail: dict) -> Dict[str, Any]:
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
        event["id"] = int(cur.lastrowid)
        return event

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            return self._insert_audit(action, entity_type, entity_id, actor, detail)

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
