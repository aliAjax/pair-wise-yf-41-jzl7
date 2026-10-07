import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _event_data():
    return {
        "title": "Event-A",
        "origin_time": "2026-01-01T00:00:00Z",
        "location": "Region-A",
        "reports": [
            {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
            {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
        ],
    }


class DeliveryReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")
        self.station = Actor("sta-1", "station")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_subscriptions(self, *names):
        return [self.service.create_subscription(self.actor, name) for name in names]

    def _make_published_event(self):
        self.service.create(
            self.actor, "station", {"code": "STA-1", "lat": 35.0, "lon": 110.0}
        )
        self.service.create(
            self.actor, "station", {"code": "STA-2", "lat": 36.0, "lon": 111.0}
        )
        event = self.service.create(self.actor, "event", _event_data())
        self.service.transition(self.actor, event["id"], "associate", {})
        self.service.transition(
            self.actor,
            event["id"],
            "review",
            {"reviewer": "R-1", "magnitude": 4.2},
        )
        return self.service.transition(
            self.actor, event["id"], "publish", {"communication_id": "C-1"}
        )

    # 1) 发布时按订阅给每家建投递记录，带事件编号和版本号
    def test_publish_creates_delivery_per_subscription_with_event_and_version(self):
        s1 = self._make_subscriptions("Company-A", "Company-B")
        event = self._make_published_event()
        deliveries = self.service.list_deliveries(event_id=event["id"])
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(
            {d["subscription_id"] for d in deliveries}, {s1[0]["id"], s1[1]["id"]}
        )
        for delivery in deliveries:
            self.assertEqual(delivery["event_id"], event["id"])
            self.assertEqual(delivery["version"], event["version"])
            self.assertEqual(delivery["status"], "pending")

    # 2) 失败后重试沿用同一条，不重复收到
    def test_retry_reuses_same_record_no_duplicate(self):
        self._make_subscriptions("Company-A", "Company-B")
        event = self._make_published_event()
        delivery = self.service.list_deliveries(event_id=event["id"])[0]
        # simulate a failed attempt
        self.repo.mark_delivery(delivery["id"], "failed")
        self.assertEqual(len(self.service.list_deliveries(event_id=event["id"])), 2)

        updated = self.service.retry_delivery(self.actor, delivery["id"])
        self.assertEqual(updated["id"], delivery["id"])
        self.assertEqual(updated["status"], "delivered")
        self.assertEqual(updated["attempts"], 2)
        # still one record per subscription, no duplicate
        self.assertEqual(len(self.service.list_deliveries(event_id=event["id"])), 2)

        # idempotent: a delivered delivery is not re-sent
        again = self.service.retry_delivery(self.actor, delivery["id"])
        self.assertEqual(again["id"], delivery["id"])
        self.assertEqual(again["attempts"], 2)

    def test_retry_voided_delivery_rejected(self):
        self._make_subscriptions("Company-A")
        event = self._make_published_event()
        delivery = self.service.list_deliveries(event_id=event["id"])[0]
        self.repo.void_undelivered_deliveries(event["id"], event["version"] + 1)
        with self.assertRaises(ConflictError):
            self.service.retry_delivery(self.actor, delivery["id"])

    # 3) 撤回按每家回执分别算，回执齐了才算撤完
    def test_withdrawal_complete_only_after_all_receipts(self):
        subs = self._make_subscriptions("Company-A", "Company-B")
        event = self._make_published_event()
        withdrawn = self.service.transition(
            self.actor, event["id"], "withdraw", {"reason": "false alarm"}
        )
        self.assertEqual(withdrawn["status"], "withdrawn")

        withdrawals = self.service.list_withdrawals(event_id=event["id"])
        self.assertEqual(len(withdrawals), 1)
        withdrawal = withdrawals[0]
        self.assertEqual(withdrawal["status"], "pending")

        receipts = self.service.list_receipts(withdrawal["id"])
        self.assertEqual(len(receipts), 2)

        # first receipt acked: still pending
        self.service.ack_withdrawal(self.actor, withdrawal["id"], subs[0]["id"])
        withdrawal = self.service.list_withdrawals(event_id=event["id"])[0]
        self.assertEqual(withdrawal["status"], "pending")

        # second receipt acked: now complete
        self.service.ack_withdrawal(self.actor, withdrawal["id"], subs[1]["id"])
        withdrawal = self.service.list_withdrawals(event_id=event["id"])[0]
        self.assertEqual(withdrawal["status"], "complete")

    def test_ack_unknown_receipt_rejected(self):
        self._make_subscriptions("Company-A")
        event = self._make_published_event()
        self.service.transition(
            self.actor, event["id"], "withdraw", {"reason": "false alarm"}
        )
        withdrawal = self.service.list_withdrawals(event_id=event["id"])[0]
        with self.assertRaises(NotFoundError):
            self.service.ack_withdrawal(self.actor, withdrawal["id"], "sub-unknown")

    # 4) 版本更新后没送出的旧投递立刻作废
    def test_revise_voids_undelivered_old_deliveries(self):
        self._make_subscriptions("Company-A", "Company-B")
        event = self._make_published_event()
        deliveries = self.service.list_deliveries(event_id=event["id"])
        # mark one delivered, leave the other pending
        self.repo.mark_delivery(deliveries[0]["id"], "delivered")
        old_version = event["version"]

        revised = self.service.transition(
            self.actor,
            event["id"],
            "revise",
            {"reason": "new station data", "magnitude": 4.3},
        )
        self.assertEqual(revised["status"], "revised")
        self.assertEqual(revised["version"], old_version + 1)

        after = self.service.list_deliveries(event_id=event["id"])
        old_deliveries = [d for d in after if d["version"] == old_version]
        new_deliveries = [d for d in after if d["version"] == revised["version"]]
        self.assertEqual(len(old_deliveries), 2)
        self.assertEqual(
            {d["status"] for d in old_deliveries}, {"delivered", "voided"}
        )
        self.assertEqual(len(new_deliveries), 2)
        for delivery in new_deliveries:
            self.assertEqual(delivery["status"], "pending")

    # 5) 两人同时提交撤回和修订，晚到的修订挡回（乐观锁）
    def test_late_revise_blocked_by_optimistic_lock(self):
        self._make_subscriptions("Company-A")
        event = self._make_published_event()
        version = event["version"]
        first = self.service.transition(
            self.actor,
            event["id"],
            "revise",
            {"reason": "first revision", "magnitude": 4.3},
            expected_version=version,
        )
        self.assertEqual(first["status"], "revised")
        # late revise still carrying the old version loses the race
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.actor,
                event["id"],
                "revise",
                {"reason": "late revision", "magnitude": 4.4},
                expected_version=version,
            )

    def test_withdraw_blocks_further_revise(self):
        self._make_subscriptions("Company-A")
        event = self._make_published_event()
        withdrawn = self.service.transition(
            self.actor, event["id"], "withdraw", {"reason": "false alarm"}
        )
        self.assertEqual(withdrawn["status"], "withdrawn")
        with self.assertRaises((ConflictError, InvalidTransition)):
            self.service.transition(
                self.actor,
                event["id"],
                "revise",
                {"reason": "too late", "magnitude": 4.3},
            )

    # 6) 台站越权撤回要被拒绝
    def test_station_cannot_withdraw(self):
        self._make_subscriptions("Company-A")
        event = self._make_published_event()
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.station, event["id"], "withdraw", {"reason": "false alarm"}
            )

    def test_reviewer_can_withdraw(self):
        self._make_subscriptions("Company-A")
        event = self._make_published_event()
        reviewer = Actor("rev-1", "reviewer")
        withdrawn = self.service.transition(
            reviewer, event["id"], "withdraw", {"reason": "false alarm"}
        )
        self.assertEqual(withdrawn["status"], "withdrawn")

    # 7) 旧数据没投递记录，升级时按已送达版本回填
    def test_backfill_legacy_deliveries_as_delivered(self):
        self._make_subscriptions("Company-A", "Company-B")
        event = self.service.create(self.actor, "event", _event_data())
        self.service.transition(self.actor, event["id"], "associate", {})
        reviewed = self.service.transition(
            self.actor,
            event["id"],
            "review",
            {"reviewer": "R-1", "magnitude": 4.0},
        )
        # Simulate legacy data: published at version 5 with no delivery records.
        self.repo.update_entity(
            reviewed["id"], reviewed["version"], "published", dict(reviewed["data"])
        )
        self.repo.update_entity(
            reviewed["id"],
            reviewed["version"] + 1,
            "published",
            dict(reviewed["data"]),
        )
        legacy = self.service.get(event["id"])
        self.assertEqual(legacy["status"], "published")
        self.assertEqual(legacy["version"], 5)
        self.assertEqual(self.service.list_deliveries(event_id=event["id"]), [])

        created = self.service.upgrade()
        self.assertEqual(created, 2)
        deliveries = self.service.list_deliveries(event_id=event["id"])
        self.assertEqual(len(deliveries), 2)
        for delivery in deliveries:
            self.assertEqual(delivery["version"], 5)
            self.assertEqual(delivery["status"], "delivered")

        # upgrade is idempotent
        self.service.upgrade()
        self.assertEqual(len(self.service.list_deliveries(event_id=event["id"])), 2)

    def test_subscription_requires_name(self):
        with self.assertRaises(ValidationError):
            self.service.create_subscription(self.actor, "  ")


if __name__ == "__main__":
    unittest.main()
