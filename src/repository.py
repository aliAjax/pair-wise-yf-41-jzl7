import json
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id TEXT PRIMARY KEY,
                    subscriber TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(event_id, subscription_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_deliveries_event
                    ON deliveries(event_id, version);
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_withdrawals_event
                    ON withdrawals(event_id);
                CREATE TABLE IF NOT EXISTS withdrawal_receipts (
                    withdrawal_id TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    acked_at TEXT,
                    PRIMARY KEY(withdrawal_id, subscription_id)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _subscription_from_row(row):
        return {
            "id": row["id"],
            "subscriber": row["subscriber"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _delivery_from_row(row):
        return {
            "id": row["id"],
            "event_id": row["event_id"],
            "subscription_id": row["subscription_id"],
            "version": int(row["version"]),
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "delivered_at": row["delivered_at"],
        }

    @staticmethod
    def _withdrawal_from_row(row):
        return {
            "id": row["id"],
            "event_id": row["event_id"],
            "version": int(row["version"]),
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _receipt_from_row(row):
        return {
            "withdrawal_id": row["withdrawal_id"],
            "subscription_id": row["subscription_id"],
            "status": row["status"],
            "acked_at": row["acked_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def create_subscription(self, subscription_id, subscriber, actor_id):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO subscriptions(id, subscriber, created_by, created_at) "
                "VALUES (?, ?, ?, ?)",
                (subscription_id, subscriber, actor_id, now),
            )
        return self.get_subscription(subscription_id)

    def get_subscription(self, subscription_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
            ).fetchone()
        return self._subscription_from_row(row) if row else None

    def list_subscriptions(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM subscriptions ORDER BY created_at, id"
            ).fetchall()
        return [self._subscription_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Deliveries
    # ------------------------------------------------------------------

    def create_delivery(self, delivery_id, event_id, subscription_id, version, status="pending"):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO deliveries(id, event_id, subscription_id, version, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (delivery_id, event_id, subscription_id, int(version), status, now, now),
            )
            row = connection.execute(
                "SELECT * FROM deliveries WHERE event_id = ? AND subscription_id = ? AND version = ?",
                (event_id, subscription_id, int(version)),
            ).fetchone()
        return self._delivery_from_row(row)

    def get_delivery(self, delivery_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        return self._delivery_from_row(row) if row else None

    def list_deliveries(self, event_id=None, subscription_id=None):
        clauses = []
        params = []
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if subscription_id:
            clauses.append("subscription_id = ?")
            params.append(subscription_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deliveries" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._delivery_from_row(row) for row in rows]

    def mark_delivery(self, delivery_id, status):
        now = utcnow()
        with self._connect() as connection:
            if status == "delivered":
                connection.execute(
                    "UPDATE deliveries SET status = ?, attempts = attempts + 1, updated_at = ?, delivered_at = ? "
                    "WHERE id = ?",
                    (status, now, now, delivery_id),
                )
            else:
                connection.execute(
                    "UPDATE deliveries SET status = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                    (status, now, delivery_id),
                )
        return self.get_delivery(delivery_id)

    def void_undelivered_deliveries(self, event_id, below_version):
        """作废指定事件版本以下、尚未送达的投递（版本更新后旧投递立即作废）。"""
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE deliveries SET status = 'voided', updated_at = ? "
                "WHERE event_id = ? AND version < ? AND status IN ('pending', 'failed')",
                (now, event_id, int(below_version)),
            )

    def void_event_deliveries(self, event_id):
        """作废事件所有尚未送达的投递（撤回时防止误报内容继续流出）。"""
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "UPDATE deliveries SET status = 'voided', updated_at = ? "
                "WHERE event_id = ? AND status IN ('pending', 'failed')",
                (now, event_id),
            )

    # ------------------------------------------------------------------
    # Withdrawals
    # ------------------------------------------------------------------

    def create_withdrawal(self, withdrawal_id, event_id, version, actor_id):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO withdrawals(id, event_id, version, status, created_by, created_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (withdrawal_id, event_id, int(version), actor_id, now),
            )
        return self.get_withdrawal(withdrawal_id)

    def get_withdrawal(self, withdrawal_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM withdrawals WHERE id = ?", (withdrawal_id,)
            ).fetchone()
        return self._withdrawal_from_row(row) if row else None

    def list_withdrawals(self, event_id=None):
        clauses = []
        params = []
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM withdrawals" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._withdrawal_from_row(row) for row in rows]

    def create_receipt(self, withdrawal_id, subscription_id):
        now = utcnow()
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO withdrawal_receipts(withdrawal_id, subscription_id, status, acked_at) "
                "VALUES (?, ?, 'pending', NULL)",
                (withdrawal_id, subscription_id),
            )

    def list_receipts(self, withdrawal_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM withdrawal_receipts WHERE withdrawal_id = ? ORDER BY subscription_id",
                (withdrawal_id,),
            ).fetchall()
        return [self._receipt_from_row(row) for row in rows]

    def ack_receipt(self, withdrawal_id, subscription_id):
        now = utcnow()
        with self._connect() as connection:
            cur = connection.execute(
                "UPDATE withdrawal_receipts SET status = 'acked', acked_at = ? "
                "WHERE withdrawal_id = ? AND subscription_id = ?",
                (now, withdrawal_id, subscription_id),
            )
            if cur.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM withdrawal_receipts "
                "WHERE withdrawal_id = ? AND subscription_id = ?",
                (withdrawal_id, subscription_id),
            ).fetchone()
            agg = connection.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'acked' THEN 1 ELSE 0 END) AS acked "
                "FROM withdrawal_receipts WHERE withdrawal_id = ?",
                (withdrawal_id,),
            ).fetchone()
            total = agg["total"] or 0
            acked = agg["acked"] or 0
            if total > 0 and total == acked:
                connection.execute(
                    "UPDATE withdrawals SET status = 'complete', completed_at = ? WHERE id = ?",
                    (utcnow(), withdrawal_id),
                )
        return self._receipt_from_row(row)

    # ------------------------------------------------------------------
    # Upgrade / backfill
    # ------------------------------------------------------------------

    def backfill_deliveries(self):
        """旧数据没有投递记录：对已发布（含已修订/已撤回）事件按当前已送达版本回填。"""
        with self._connect() as connection:
            events = connection.execute(
                "SELECT * FROM entities WHERE kind = 'event' "
                "AND status IN ('published', 'revised', 'withdrawn')"
            ).fetchall()
            subscriptions = connection.execute(
                "SELECT id FROM subscriptions ORDER BY id"
            ).fetchall()
        subscription_ids = [row["id"] for row in subscriptions]
        created = 0
        for event_row in events:
            event = self._entity_from_row(event_row)
            if self.list_deliveries(event_id=event["id"]):
                continue
            for subscription_id in subscription_ids:
                self.create_delivery(
                    uuid4().hex,
                    event["id"],
                    subscription_id,
                    event["version"],
                    status="delivered",
                )
                created += 1
        return created

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
