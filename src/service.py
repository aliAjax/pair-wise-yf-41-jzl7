from uuid import uuid4

from .audit import AuditTrail
from .domain import Actor, ConflictError, InvalidTransition, NotFoundError
from .repository import utcnow
from .rules import RuleEngine

DELIVERY_BACKFILL_KEY = "deliveries_backfill_v1"


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
        if entity["kind"] == "event" and action == "settle_withdraw":
            self._ensure_withdraw_complete(entity_id)
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
        return self._after_transition(actor, updated, action)

    def _after_transition(self, actor, entity, action):
        if entity["kind"] != "event":
            return entity
        if action in ("publish", "revise"):
            if action == "revise":
                # 版本更新后没送出的旧投递立刻作废
                voided = self.repository.void_deliveries(
                    entity["id"], kind="publish", before_version=entity["version"]
                )
                if voided:
                    self.audit.record(
                        entity["id"], actor, "deliveries_voided", None, entity["status"],
                        {"voided": voided, "reason": "revise"},
                    )
            # 发布时按订阅给每家建投递记录，带事件编号和版本号
            created = self._create_publish_deliveries(entity)
            if created:
                self.audit.record(
                    entity["id"], actor, "deliveries_created", None, entity["status"],
                    {
                        "created": len(created),
                        "event_version": entity["version"],
                        "subscribers": [item["subscriber"] for item in created],
                    },
                )
        elif action == "withdraw":
            voided = self.repository.void_deliveries(entity["id"], kind="publish")
            notices = self._create_withdraw_notices(entity)
            self.audit.record(
                entity["id"], actor, "withdraw_notices", None, entity["status"],
                {
                    "voided": voided,
                    "notices": len(notices),
                    "subscribers": [item["subscriber"] for item in notices],
                },
            )
            if self._withdraw_complete(entity["id"]):
                entity = self.transition(actor, entity["id"], "settle_withdraw", {}, None)
        return entity

    def _active_subscribers(self):
        subscribers = set()
        for entity in self.repository.list_entities(kind="subscription", status="active"):
            name = entity["data"].get("subscriber")
            if name:
                subscribers.add(name)
        return sorted(subscribers)

    def _create_publish_deliveries(self, event):
        records = []
        for subscriber in self._active_subscribers():
            records.append(
                self.repository.create_delivery(
                    str(uuid4()),
                    event["id"],
                    event["version"],
                    subscriber,
                    "publish",
                    status="pending",
                    detail={"communication_id": event["data"].get("communication_id")},
                )
            )
        return records

    def _create_withdraw_notices(self, event):
        delivered = self.repository.list_deliveries(
            event_id=event["id"], kind="publish", status="delivered"
        )
        records = []
        for subscriber in sorted({item["subscriber"] for item in delivered}):
            records.append(
                self.repository.create_delivery(
                    str(uuid4()),
                    event["id"],
                    event["version"],
                    subscriber,
                    "withdraw",
                    status="pending",
                    detail={"reason": event["data"].get("reason")},
                )
            )
        return records

    def _withdraw_complete(self, event_id):
        notices = self.repository.list_deliveries(event_id=event_id, kind="withdraw")
        return all(item["status"] == "acknowledged" for item in notices)

    def _ensure_withdraw_complete(self, event_id):
        if not self._withdraw_complete(event_id):
            raise InvalidTransition("withdraw receipts incomplete")

    def get_delivery(self, delivery_id):
        delivery = self.repository.get_delivery(delivery_id)
        if not delivery:
            raise NotFoundError("delivery not found: " + delivery_id)
        return delivery

    def list_deliveries(self, event_id):
        entity = self.repository.get_entity(event_id)
        if not entity:
            raise NotFoundError("entity not found: " + event_id)
        items = self.repository.list_deliveries(event_id=event_id)
        summary = {}
        for item in items:
            bucket = summary.setdefault(item["kind"], {})
            bucket[item["status"]] = bucket.get(item["status"], 0) + 1
        return {
            "event_id": entity["id"],
            "event_status": entity["status"],
            "event_version": entity["version"],
            "items": items,
            "summary": summary,
            "withdraw_complete": self._withdraw_complete(event_id),
        }

    def delivery_action(self, actor, delivery_id, action, data=None):
        if action == "dispatch":
            return self.dispatch_delivery(actor, delivery_id, (data or {}).get("outcome"), data)
        if action == "ack":
            return self.acknowledge_delivery(actor, delivery_id, data)
        raise InvalidTransition("unknown delivery action: " + str(action))

    def dispatch_delivery(self, actor, delivery_id, outcome, data=None):
        delivery = self.get_delivery(delivery_id)
        self.rules.validate_delivery_action(actor, "dispatch")
        target = self.rules.delivery_dispatch_target(delivery, outcome)
        if target is None:
            # 已送达的记录重试是幂等空操作，下游不重复收到
            return delivery
        detail = dict(delivery["detail"])
        detail["last_outcome"] = outcome
        if isinstance(data, dict) and data.get("note"):
            detail["note"] = data["note"]
        updated = self.repository.update_delivery(
            delivery_id, delivery["status"], target, delivery["attempts"] + 1, detail
        )
        self.audit.record(
            delivery["event_id"], actor, "delivery_dispatch", delivery["status"], target,
            {
                "delivery_id": delivery_id,
                "subscriber": delivery["subscriber"],
                "kind": delivery["kind"],
                "event_version": delivery["event_version"],
            },
        )
        return updated

    def acknowledge_delivery(self, actor, delivery_id, data=None):
        delivery = self.get_delivery(delivery_id)
        self.rules.validate_delivery_action(actor, "ack")
        target = self.rules.delivery_ack_target(delivery)
        if target is None:
            return delivery
        detail = dict(delivery["detail"])
        if isinstance(data, dict) and data.get("receipt"):
            detail["receipt"] = data["receipt"]
        updated = self.repository.update_delivery(
            delivery_id, delivery["status"], target, delivery["attempts"], detail
        )
        self.audit.record(
            delivery["event_id"], actor, "delivery_ack", delivery["status"], target,
            {"delivery_id": delivery_id, "subscriber": delivery["subscriber"]},
        )
        # 回执齐了才算撤完
        if self._withdraw_complete(delivery["event_id"]):
            event = self.repository.get_entity(delivery["event_id"])
            if event and event["status"] == "withdrawing":
                self.transition(actor, event["id"], "settle_withdraw", {}, None)
        return updated

    def backfill_deliveries(self):
        # 旧数据没投递记录，升级时按已送达版本回填；只跑一次
        if self.repository.get_meta(DELIVERY_BACKFILL_KEY):
            return {"backfilled": 0, "skipped": True}
        subscribers = self._active_subscribers()
        backfilled = 0
        system = Actor("system", "admin")
        for event in self.repository.list_entities(kind="event"):
            if event["status"] not in ("published", "revised", "withdrawn"):
                continue
            if self.repository.list_deliveries(event_id=event["id"]):
                continue
            for subscriber in subscribers:
                self.repository.create_delivery(
                    str(uuid4()), event["id"], event["version"], subscriber, "publish",
                    status="delivered", detail={"backfilled": True},
                )
                backfilled += 1
            if event["status"] == "withdrawn":
                for subscriber in subscribers:
                    self.repository.create_delivery(
                        str(uuid4()), event["id"], event["version"], subscriber, "withdraw",
                        status="acknowledged", detail={"backfilled": True},
                    )
                    backfilled += 1
            self.audit.record(
                event["id"], system, "deliveries_backfilled", None, event["status"],
                {"event_version": event["version"], "subscribers": subscribers},
            )
        self.repository.set_meta(DELIVERY_BACKFILL_KEY, utcnow())
        return {"backfilled": backfilled, "skipped": False}

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
