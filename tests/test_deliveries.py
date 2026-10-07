import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.reviewer = Actor("rev-1", "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _subscription(self, name, disable=False):
        entity = self.service.create(
            self.admin, "subscription",
            {"subscriber": name, "endpoint": "https://%s.example/feed" % name},
        )
        if disable:
            self.service.transition(self.admin, entity["id"], "disable", {})
        return entity

    def _event(self, title="Event-X"):
        return self.service.create(
            self.admin, "event",
            {
                "title": title,
                "origin_time": "2026-02-01T00:00:00Z",
                "location": "Region-X",
                "reports": [
                    {"station": "STA-1", "time_offset": 1, "distance_km": 0.5},
                    {"station": "STA-2", "time_offset": 2, "distance_km": 1.0},
                ],
            },
        )

    def _publish(self, event):
        for action, data in (
            ("associate", {}),
            ("review", {"reviewer": "R-1", "magnitude": 4.0}),
            ("publish", {"communication_id": "C-1"}),
        ):
            event = self.service.transition(self.admin, event["id"], action, data)
        return event

    def _deliveries(self, event_id, kind="publish"):
        return [
            item for item in self.repo.list_deliveries(event_id=event_id)
            if item["kind"] == kind
        ]

    def _dispatch(self, delivery, outcome):
        return self.service.delivery_action(
            self.admin, delivery["id"], "dispatch", {"outcome": outcome}
        )

    def test_publish_creates_delivery_per_subscription(self):
        self._subscription("agency-a")
        self._subscription("agency-b")
        self._subscription("agency-c", disable=True)
        event = self._publish(self._event())
        items = self._deliveries(event["id"])
        self.assertEqual(sorted(item["subscriber"] for item in items), ["agency-a", "agency-b"])
        for item in items:
            self.assertEqual(item["event_id"], event["id"])
            self.assertEqual(item["event_version"], event["version"])
            self.assertEqual(item["status"], "pending")
            self.assertEqual(item["attempts"], 0)

    def test_failed_dispatch_retries_same_record_without_duplicate(self):
        self._subscription("agency-a")
        event = self._publish(self._event())
        (delivery,) = self._deliveries(event["id"])
        failed = self._dispatch(delivery, "failed")
        self.assertEqual(failed["id"], delivery["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["attempts"], 1)
        delivered = self._dispatch(failed, "delivered")
        self.assertEqual(delivered["id"], delivery["id"])
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["attempts"], 2)
        # 已送达后重试是幂等空操作，不重复收到
        again = self._dispatch(delivered, "delivered")
        self.assertEqual(again["status"], "delivered")
        self.assertEqual(again["attempts"], 2)
        self.assertEqual(len(self._deliveries(event["id"])), 1)

    def test_dispatch_rejects_void_and_bad_outcome(self):
        self._subscription("agency-a")
        event = self._publish(self._event())
        (delivery,) = self._deliveries(event["id"])
        with self.assertRaises(PermissionDenied):
            self.service.delivery_action(
                Actor("sta-1", "station"), delivery["id"], "dispatch", {"outcome": "delivered"}
            )
        with self.assertRaises(Exception):
            self._dispatch(delivery, "bogus")
        event = self.service.transition(
            self.admin, event["id"], "revise", {"reason": "fix", "magnitude": 4.1}
        )
        with self.assertRaises(InvalidTransition):
            self._dispatch(delivery, "delivered")

    def test_withdraw_completes_only_after_all_receipts(self):
        self._subscription("agency-a")
        self._subscription("agency-b")
        event = self._publish(self._event())
        for delivery in self._deliveries(event["id"]):
            self._dispatch(delivery, "delivered")
        event = self.service.transition(
            self.reviewer, event["id"], "withdraw", {"reason": "false alarm"}
        )
        self.assertEqual(event["status"], "withdrawing")
        notices = self._deliveries(event["id"], kind="withdraw")
        self.assertEqual(len(notices), 2)
        # 回执前必须先送出撤回通知
        with self.assertRaises(InvalidTransition):
            self.service.delivery_action(self.admin, notices[0]["id"], "ack", {})
        # 直接结账也被挡回：回执没齐不算撤完
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.admin, event["id"], "settle_withdraw", {})
        first = self._dispatch(notices[0], "sent")
        self.service.delivery_action(self.admin, first["id"], "ack", {"receipt": "ACK-1"})
        self.assertEqual(self.service.get(event["id"])["status"], "withdrawing")
        second = self._dispatch(notices[1], "sent")
        self.service.delivery_action(self.admin, second["id"], "ack", {"receipt": "ACK-2"})
        self.assertEqual(self.service.get(event["id"])["status"], "withdrawn")
        view = self.service.list_deliveries(event["id"])
        self.assertTrue(view["withdraw_complete"])
        self.assertEqual(view["summary"]["withdraw"]["acknowledged"], 2)

    def test_withdraw_voids_undelivered_and_notifies_only_delivered(self):
        self._subscription("agency-a")
        self._subscription("agency-b")
        event = self._publish(self._event())
        deliveries = {item["subscriber"]: item for item in self._deliveries(event["id"])}
        self._dispatch(deliveries["agency-a"], "delivered")
        event = self.service.transition(
            self.admin, event["id"], "withdraw", {"reason": "false alarm"}
        )
        self.assertEqual(event["status"], "withdrawing")
        remaining = {item["subscriber"]: item for item in self._deliveries(event["id"])}
        self.assertEqual(remaining["agency-a"]["status"], "delivered")
        self.assertEqual(remaining["agency-b"]["status"], "void")
        notices = self._deliveries(event["id"], kind="withdraw")
        self.assertEqual([item["subscriber"] for item in notices], ["agency-a"])
        sent = self._dispatch(notices[0], "sent")
        self.service.delivery_action(self.admin, sent["id"], "ack", {})
        self.assertEqual(self.service.get(event["id"])["status"], "withdrawn")

    def test_revise_voids_undelivered_old_deliveries(self):
        self._subscription("agency-a")
        self._subscription("agency-b")
        event = self._publish(self._event())
        old_version = event["version"]
        deliveries = {item["subscriber"]: item for item in self._deliveries(event["id"])}
        self._dispatch(deliveries["agency-a"], "delivered")
        self._dispatch(deliveries["agency-b"], "failed")
        event = self.service.transition(
            self.admin, event["id"], "revise", {"reason": "new data", "magnitude": 4.3}
        )
        old = {
            item["subscriber"]: item
            for item in self._deliveries(event["id"])
            if item["event_version"] == old_version
        }
        self.assertEqual(old["agency-a"]["status"], "delivered")
        self.assertEqual(old["agency-b"]["status"], "void")
        new = [
            item for item in self._deliveries(event["id"])
            if item["event_version"] == event["version"]
        ]
        self.assertEqual(len(new), 2)
        for item in new:
            self.assertEqual(item["status"], "pending")

    def test_concurrent_withdraw_and_revise_late_revise_blocked(self):
        self._subscription("agency-a")
        event = self._publish(self._event())
        for delivery in self._deliveries(event["id"]):
            self._dispatch(delivery, "delivered")
        event = self.service.get(event["id"])
        version = event["version"]
        withdrawn = self.service.transition(
            self.reviewer, event["id"], "withdraw",
            {"reason": "false alarm"}, expected_version=version,
        )
        self.assertEqual(withdrawn["status"], "withdrawing")
        # 晚到的修订被挡回：撤回后状态机不再接受修订（陈旧版本或新版本都一样）
        for expected in (version, withdrawn["version"], None):
            with self.assertRaises(InvalidTransition):
                self.service.transition(
                    self.admin, event["id"], "revise",
                    {"reason": "new data", "magnitude": 4.3}, expected_version=expected,
                )

    def test_concurrent_revises_late_one_conflicts(self):
        self._subscription("agency-a")
        event = self._publish(self._event())
        version = event["version"]
        self.service.transition(
            self.admin, event["id"], "revise",
            {"reason": "first", "magnitude": 4.2}, expected_version=version,
        )
        # 同一版本上晚到的修订：乐观锁冲突
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.reviewer, event["id"], "revise",
                {"reason": "second", "magnitude": 4.3}, expected_version=version,
            )

    def test_station_cannot_withdraw(self):
        self._subscription("agency-a")
        event = self._publish(self._event())
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("sta-1", "station"), event["id"], "withdraw",
                {"reason": "false alarm"},
            )

    def test_backfill_deliveries_for_legacy_events(self):
        # 旧数据：发布和撤回发生在订阅与投递记录存在之前
        legacy_published = self._publish(self._event("Legacy-Published"))
        legacy_withdrawn = self._publish(self._event("Legacy-Withdrawn"))
        legacy_withdrawn = self.service.transition(
            self.admin, legacy_withdrawn["id"], "withdraw", {"reason": "false alarm"}
        )
        self.assertEqual(legacy_withdrawn["status"], "withdrawn")
        candidate = self._event("Legacy-Candidate")
        for event in (legacy_published, legacy_withdrawn, candidate):
            self.assertEqual(self.repo.list_deliveries(event_id=event["id"]), [])
        self._subscription("agency-a")
        self._subscription("agency-b")
        # 升级后新发布的事件走正常投递，不应被回填改动
        fresh = self._publish(self._event("Fresh"))
        result = self.service.backfill_deliveries()
        self.assertFalse(result["skipped"])
        self.assertEqual(result["backfilled"], 6)
        published_items = self._deliveries(legacy_published["id"])
        self.assertEqual(len(published_items), 2)
        for item in published_items:
            self.assertEqual(item["status"], "delivered")
            self.assertEqual(item["event_version"], legacy_published["version"])
            self.assertTrue(item["detail"]["backfilled"])
        withdrawn_publish = self._deliveries(legacy_withdrawn["id"])
        withdrawn_notices = self._deliveries(legacy_withdrawn["id"], kind="withdraw")
        self.assertEqual({item["status"] for item in withdrawn_publish}, {"delivered"})
        self.assertEqual({item["status"] for item in withdrawn_notices}, {"acknowledged"})
        self.assertEqual(self.repo.list_deliveries(event_id=candidate["id"]), [])
        fresh_items = self._deliveries(fresh["id"])
        self.assertEqual({item["status"] for item in fresh_items}, {"pending"})
        self.assertFalse(any(item["detail"].get("backfilled") for item in fresh_items))
        # 回填只跑一次
        again = self.service.backfill_deliveries()
        self.assertTrue(again["skipped"])
        self.assertEqual(again["backfilled"], 0)


if __name__ == "__main__":
    unittest.main()
