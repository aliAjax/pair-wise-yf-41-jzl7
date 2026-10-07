from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        self._after_transition(actor, action, updated)
        return updated

    def _after_transition(self, actor, action, entity):
        if entity["kind"] != "event":
            return
        if action == "publish":
            self._create_deliveries(entity)
        elif action == "revise":
            # 版本更新后，没送出的旧投递立刻作废
            self.repository.void_undelivered_deliveries(entity["id"], entity["version"])
            self._create_deliveries(entity)
        elif action == "withdraw":
            # 撤回时防止误报内容继续流出
            self.repository.void_event_deliveries(entity["id"])
            self._create_withdrawal(actor, entity)

    def _create_deliveries(self, entity):
        subscriptions = self.repository.list_subscriptions()
        for subscription in subscriptions:
            self.repository.create_delivery(
                uuid4().hex, entity["id"], subscription["id"], entity["version"]
            )

    def _create_withdrawal(self, actor, entity):
        withdrawal = self.repository.create_withdrawal(
            uuid4().hex, entity["id"], entity["version"], actor.user_id
        )
        for subscription in self.repository.list_subscriptions():
            self.repository.create_receipt(withdrawal["id"], subscription["id"])
        return withdrawal

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def create_subscription(self, actor, subscriber):
        name = (subscriber or "").strip()
        if not name:
            raise ValidationError("subscriber name is required")
        return self.repository.create_subscription(uuid4().hex, name, actor.user_id)

    def list_subscriptions(self):
        return self.repository.list_subscriptions()

    # ------------------------------------------------------------------
    # Deliveries
    # ------------------------------------------------------------------

    def list_deliveries(self, event_id=None):
        return self.repository.list_deliveries(event_id=event_id)

    def retry_delivery(self, actor, delivery_id):
        delivery = self.repository.get_delivery(delivery_id)
        if not delivery:
            raise NotFoundError("delivery not found: " + delivery_id)
        if delivery["status"] == "voided":
            raise ConflictError("delivery was voided and cannot be retried: " + delivery_id)
        if delivery["status"] == "delivered":
            # 幂等：已送达的投递重试不重复投递
            return delivery
        updated = self.repository.mark_delivery(delivery_id, "delivered")
        self.audit.record(
            delivery["event_id"],
            actor,
            "deliver",
            delivery["status"],
            updated["status"],
            {"delivery_id": delivery_id, "attempts": updated["attempts"]},
        )
        return updated

    # ------------------------------------------------------------------
    # Withdrawals
    # ------------------------------------------------------------------

    def list_withdrawals(self, event_id=None):
        return self.repository.list_withdrawals(event_id=event_id)

    def list_receipts(self, withdrawal_id):
        withdrawal = self.repository.get_withdrawal(withdrawal_id)
        if not withdrawal:
            raise NotFoundError("withdrawal not found: " + withdrawal_id)
        return self.repository.list_receipts(withdrawal_id)

    def ack_withdrawal(self, actor, withdrawal_id, subscription_id):
        withdrawal = self.repository.get_withdrawal(withdrawal_id)
        if not withdrawal:
            raise NotFoundError("withdrawal not found: " + withdrawal_id)
        receipt = self.repository.ack_receipt(withdrawal_id, subscription_id)
        if receipt is None:
            raise NotFoundError(
                "receipt not found for subscription %s in withdrawal %s"
                % (subscription_id, withdrawal_id)
            )
        updated = self.repository.get_withdrawal(withdrawal_id)
        self.audit.record(
            withdrawal["event_id"],
            actor,
            "ack_withdrawal",
            "pending",
            updated["status"],
            {
                "withdrawal_id": withdrawal_id,
                "subscription_id": subscription_id,
                "receipt_status": receipt["status"],
            },
        )
        return updated

    def upgrade(self):
        """升级时回填旧数据缺失的投递记录。"""
        return self.repository.backfill_deliveries()

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
