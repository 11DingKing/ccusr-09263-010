"""场地时区禁用窗口：本地日历登记、跨夏令时边界判定、硬拒绝原始时段回带。

覆盖：
- 禁用窗口按场地本地挂钟时间登记，偏移型输入被拒绝（不能用服务器本地时间代替）；
- 时段与禁用窗口相交 -> 硬拒绝（BusinessRuleError，不进候补），错误详情回带
  管理员登记的原始本地时段、场地时区、原因与解析后的 UTC 边界；
- 跨夏令时切换：春季拨快缺口夹紧、秋季拨回取第一次出现，物理时长正确；
- SQLite 写入与重启往返；HTTP 422 错误体；改期同样受限。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone

from service_09252_008.application.venue_calendar_service import COLLECTION_BLACKOUTS
from service_09252_008.domain.errors import BusinessRuleError, ValidationError
from service_09252_008.interfaces.http_api import create_server
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import (
    NOW,
    apply_payload,
    make_calendar,
    make_services,
    seed_catalog,
)

NY_TZ = "America/New_York"


def _ny_window_kwargs() -> dict:
    """覆盖 2026-03-07~09 的纽约接待窗口（显式偏移，跨 DST 也无歧义）。"""
    return {
        "resource_tz": NY_TZ,
        "window_tz": NY_TZ,
        "window_start": "2026-03-07T00:00:00-05:00",
        "window_end": "2026-03-09T00:00:00-04:00",
    }


class BlackoutRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.calendar = make_calendar(self.store, self.clock)
        self.ids = seed_catalog(self.catalog)

    def test_offset_bearing_input_rejected_wall_time_required(self) -> None:
        # 禁用窗口必须按场地本地挂钟时间登记；自带偏移会绕过场地时区，一律拒绝
        for bad in ("2026-10-01T09:00:00+08:00", "2026-10-01T01:00:00Z", "2026-10-01T09:00:00+00:00"):
            with self.assertRaises(ValidationError):
                self.calendar.create_blackout(
                    self.ids["resource_id"],
                    {"reason": "设备检修", "start_local": bad, "end_local": "2026-10-01T12:00:00"},
                )

    def test_wall_time_resolved_with_resource_tz_and_persisted(self) -> None:
        view = self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "场地维护", "start_local": "2026-10-01T09:00:00", "end_local": "2026-10-01T12:00:00"},
        )
        self.assertEqual(view["tz"], "Asia/Shanghai")
        self.assertEqual(view["start_local"], "2026-10-01T09:00:00")
        self.assertEqual(view["end_local"], "2026-10-01T12:00:00")
        self.assertEqual(view["start_utc"], "2026-10-01T01:00:00+00:00")
        self.assertEqual(view["end_utc"], "2026-10-01T04:00:00+00:00")
        self.assertFalse(view["start_gap_adjusted"])

    def test_explicit_tz_must_match_venue(self) -> None:
        with self.assertRaises(ValidationError):
            self.calendar.create_blackout(
                self.ids["resource_id"],
                {
                    "reason": "x",
                    "tz": "Europe/Paris",
                    "start_local": "2026-10-01T09:00:00",
                    "end_local": "2026-10-01T12:00:00",
                },
            )

    def test_unknown_resource_404(self) -> None:
        from service_09252_008.domain.errors import NotFoundError

        with self.assertRaises(NotFoundError):
            self.calendar.create_blackout(
                "res_nope",
                {"reason": "x", "start_local": "2026-10-01T09:00:00", "end_local": "2026-10-01T12:00:00"},
            )


class BlackoutEnforcementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.calendar = make_calendar(self.store, self.clock)
        self.ids = seed_catalog(self.catalog)

    def test_overlapping_slot_hard_rejected_with_original_local_period(self) -> None:
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "消防演练", "start_local": "2026-10-01T09:00:00", "end_local": "2026-10-01T11:00:00"},
        )
        # 标准时段 10:00-12:00 上海（02:00-04:00 UTC）与禁用窗口相交
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(apply_payload(self.ids, "k-blk-1"))
        err = ctx.exception
        self.assertIn("blackout", err.message)
        details = err.details
        # 原始时段（场地本地挂钟时间）必须原样回带
        self.assertEqual(details["blackout_start_local"], "2026-10-01T09:00:00")
        self.assertEqual(details["blackout_end_local"], "2026-10-01T11:00:00")
        self.assertEqual(details["venue_tz"], "Asia/Shanghai")
        self.assertEqual(details["blackout_reason"], "消防演练")
        self.assertEqual(details["blackout_start_utc"], "2026-10-01T01:00:00+00:00")
        self.assertEqual(details["blackout_end_utc"], "2026-10-01T03:00:00+00:00")
        self.assertEqual(details["slot_start"], "2026-10-01T02:00:00+00:00")
        # 硬拒绝：没有产生候补预约
        self.assertEqual(self.bookings.list_bookings(), [])

    def test_half_open_boundaries_do_not_overlap(self) -> None:
        # 禁用窗口 12:00-14:00 本地：标准课程 10:00-12:00 恰好结束于窗口起点，不相交
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "午间封闭", "start_local": "2026-10-01T12:00:00", "end_local": "2026-10-01T14:00:00"},
        )
        view = self.bookings.apply(apply_payload(self.ids, "k-blk-boundary"))
        self.assertEqual(view["status"], "REQUESTED")

    def test_other_resource_unaffected(self) -> None:
        other = self.catalog.create_resource(
            {"name": "染整工坊B", "capacity": 30, "safety_rating": 2, "tz": "Asia/Shanghai", "hourly_fee_cents": 0}
        )
        self.calendar.create_blackout(
            other["resource_id"],
            {"reason": "B 馆检修", "start_local": "2026-10-01T09:00:00", "end_local": "2026-10-01T12:00:00"},
        )
        view = self.bookings.apply(apply_payload(self.ids, "k-blk-other-resource"))
        self.assertEqual(view["status"], "REQUESTED")

    def test_blackout_registered_after_quote_blocks_lock(self) -> None:
        # 禁用窗口在报价之后才登记：锁定时必须复查并硬拒绝
        applied = self.bookings.apply(apply_payload(self.ids, "k-blk-late-1"))
        self.bookings.quote(applied["booking_id"])
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "临时消防检查", "start_local": "2026-10-01T10:00:00", "end_local": "2026-10-01T12:00:00"},
        )
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.lock(applied["booking_id"], {"idempotency_key": "k-blk-late-lock"})
        self.assertEqual(ctx.exception.details["blackout_reason"], "临时消防检查")
        # 预约仍停留在 QUOTED，未被扣减库存
        self.assertEqual(self.bookings.get_booking(applied["booking_id"])["status"], "QUOTED")

    def test_reschedule_into_blackout_rejected(self) -> None:
        applied = self.bookings.apply(apply_payload(self.ids, "k-blk-rs-1"))
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "临时征用", "start_local": "2026-10-01T14:00:00", "end_local": "2026-10-01T17:00:00"},
        )
        with self.assertRaises(BusinessRuleError):
            self.bookings.reschedule(
                applied["booking_id"],
                {
                    "idempotency_key": "k-blk-rs-move",
                    "slot_start": "2026-10-01T06:00:00+00:00",  # 14:00 上海
                    "slot_end": "2026-10-01T08:00:00+00:00",
                },
            )

    def test_resolution_independent_of_server_timezone(self) -> None:
        # 切换进程本地时区，场地时区解析结果必须完全一致（禁用窗口不读服务器时区）
        if not hasattr(time, "tzset"):
            self.skipTest("tzset unavailable on this platform")
        old_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Pacific/Honolulu"
            time.tzset()
            view = self.calendar.create_blackout(
                self.ids["resource_id"],
                {"reason": "维护", "start_local": "2026-10-01T09:00:00", "end_local": "2026-10-01T12:00:00"},
            )
            self.assertEqual(view["start_utc"], "2026-10-01T01:00:00+00:00")
            self.assertEqual(view["end_utc"], "2026-10-01T04:00:00+00:00")
            with self.assertRaises(BusinessRuleError):
                self.bookings.apply(apply_payload(self.ids, "k-blk-honolulu"))
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()


class SpringForwardDstTests(unittest.TestCase):
    """纽约 2026-03-08 02:00 EST -> 03:00 EDT（春季拨快）。"""

    def setUp(self) -> None:
        before_dst = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.catalog, self.bookings, self.clock, self.store = make_services(now=before_dst)
        self.calendar = make_calendar(self.store, self.clock)
        self.ids = seed_catalog(self.catalog, **_ny_window_kwargs())

    def test_blackout_crossing_spring_forward_has_correct_physical_span(self) -> None:
        # 本地 01:30-03:30：墙上差 2 小时，但 02:00-03:00 这一小时被跳过，
        # 物理跨度应为 [06:30Z, 07:30Z)（1 小时）。
        view = self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "DST 切换封馆", "start_local": "2026-03-08T01:30:00", "end_local": "2026-03-08T03:30:00"},
        )
        self.assertEqual(view["start_utc"], "2026-03-08T06:30:00+00:00")
        self.assertEqual(view["end_utc"], "2026-03-08T07:30:00+00:00")

    def test_slot_inside_physical_span_rejected(self) -> None:
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "DST 切换封馆", "start_local": "2026-03-08T01:30:00", "end_local": "2026-03-08T03:30:00"},
        )
        # [05:30Z, 07:30Z)：本地墙上 00:30(EST) -> 03:30(EDT)，物理上覆盖整个封馆小时
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(
                apply_payload(
                    self.ids,
                    "k-spring-in",
                    slot_start="2026-03-08T05:30:00+00:00",
                    slot_end="2026-03-08T07:30:00+00:00",
                )
            )
        self.assertEqual(ctx.exception.details["venue_tz"], NY_TZ)
        self.assertEqual(ctx.exception.details["blackout_start_local"], "2026-03-08T01:30:00")

    def test_slots_ending_or_starting_at_physical_boundaries_accepted(self) -> None:
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "DST 切换封馆", "start_local": "2026-03-08T01:30:00", "end_local": "2026-03-08T03:30:00"},
        )
        # 课前边界：[04:30Z, 06:30Z) 恰好在 06:30Z 封馆开始时结束
        before = self.bookings.apply(
            apply_payload(
                self.ids,
                "k-spring-before",
                slot_start="2026-03-08T04:30:00+00:00",
                slot_end="2026-03-08T06:30:00+00:00",
            )
        )
        self.assertEqual(before["status"], "REQUESTED")
        self.bookings.cancel(before["booking_id"])
        # 课后边界：[07:30Z, 09:30Z) 恰好在封馆结束时开始
        after = self.bookings.apply(
            apply_payload(
                self.ids,
                "k-spring-after",
                slot_start="2026-03-08T07:30:00+00:00",
                slot_end="2026-03-08T09:30:00+00:00",
            )
        )
        self.assertEqual(after["status"], "REQUESTED")

    def test_boundary_in_skipped_gap_snaps_forward(self) -> None:
        # 起点 02:30 本地不存在（缺口 02:00-03:00）：夹紧到 07:00Z 并标记
        view = self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "缺口起点", "start_local": "2026-03-08T02:30:00", "end_local": "2026-03-08T04:00:00"},
        )
        self.assertTrue(view["start_gap_adjusted"])
        self.assertEqual(view["start_utc"], "2026-03-08T07:00:00+00:00")
        self.assertEqual(view["end_utc"], "2026-03-08T08:00:00+00:00")
        # [05:00Z, 07:00Z)：物理上在切换前结束，虽然覆盖墙上 02:00 之前的时间，
        # 但禁用窗口真实起点是 07:00Z，必须放行（朴素挂钟相减会误判）
        ok = self.bookings.apply(
            apply_payload(
                self.ids,
                "k-spring-gap-ok",
                slot_start="2026-03-08T05:00:00+00:00",
                slot_end="2026-03-08T07:00:00+00:00",
            )
        )
        self.assertEqual(ok["status"], "REQUESTED")
        # [07:00Z, 09:00Z) 从夹紧后的真实起点开始 -> 拒绝，错误体带缺口说明
        with self.assertRaises(BusinessRuleError) as ctx:
            self.bookings.apply(
                apply_payload(
                    self.ids,
                    "k-spring-gap-blocked",
                    slot_start="2026-03-08T07:00:00+00:00",
                    slot_end="2026-03-08T09:00:00+00:00",
                )
            )
        self.assertIn("spring-forward", ctx.exception.details["note"])


class FallBackDstTests(unittest.TestCase):
    """纽约 2026-11-01 02:00 EDT -> 01:00 EST（秋季拨回，01:00-02:00 重复）。"""

    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services(now=NOW)
        self.calendar = make_calendar(self.store, self.clock)
        self.ids = seed_catalog(
            self.catalog,
            resource_tz=NY_TZ,
            window_tz=NY_TZ,
            window_start="2026-10-31T00:00:00-04:00",
            window_end="2026-11-02T00:00:00-04:00",
        )

    def test_repeated_hour_resolved_first_occurrence(self) -> None:
        # 本地 01:00-03:00：01 点重复一次，物理跨度 3 小时 [05:00Z, 08:00Z)；
        # 起点取第一次出现（01:00 EDT = 05:00Z），而非第二次（01:00 EST = 06:00Z）。
        view = self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "拨回夜封馆", "start_local": "2026-11-01T01:00:00", "end_local": "2026-11-01T03:00:00"},
        )
        self.assertFalse(view["start_gap_adjusted"])
        self.assertEqual(view["start_utc"], "2026-11-01T05:00:00+00:00")
        self.assertEqual(view["end_utc"], "2026-11-01T08:00:00+00:00")

    def test_slot_during_first_occurrence_blocked(self) -> None:
        # 若错误地把重复时刻解析为第二次出现（起点 06:00Z），下面这个
        # [04:00Z, 06:00Z) 的课（墙上 00:00 EDT 走到第一次的 02:00 EDT）
        # 就会被漏判；正确语义：它覆盖第一次出现的 01:00-02:00，必须拒绝。
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "拨回夜封馆", "start_local": "2026-11-01T01:00:00", "end_local": "2026-11-01T03:00:00"},
        )
        with self.assertRaises(BusinessRuleError):
            self.bookings.apply(
                apply_payload(
                    self.ids,
                    "k-fall-first",
                    slot_start="2026-11-01T04:00:00+00:00",
                    slot_end="2026-11-01T06:00:00+00:00",
                )
            )

    def test_slot_during_second_occurrence_also_blocked(self) -> None:
        # [06:30Z, 08:30Z)：墙上是第二次出现的 01:30(EST) -> 03:30
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "拨回夜封馆", "start_local": "2026-11-01T01:00:00", "end_local": "2026-11-01T03:00:00"},
        )
        with self.assertRaises(BusinessRuleError):
            self.bookings.apply(
                apply_payload(
                    self.ids,
                    "k-fall-second",
                    slot_start="2026-11-01T06:30:00+00:00",
                    slot_end="2026-11-01T08:30:00+00:00",
                )
            )

    def test_slot_before_first_occurrence_accepted(self) -> None:
        self.calendar.create_blackout(
            self.ids["resource_id"],
            {"reason": "拨回夜封馆", "start_local": "2026-11-01T01:00:00", "end_local": "2026-11-01T03:00:00"},
        )
        # [03:00Z, 05:00Z)：墙上 23:00 -> 01:00（第一次），半开边界不相交
        ok = self.bookings.apply(
            apply_payload(
                self.ids,
                "k-fall-before",
                slot_start="2026-11-01T03:00:00+00:00",
                slot_end="2026-11-01T05:00:00+00:00",
            )
        )
        self.assertEqual(ok["status"], "REQUESTED")


class BlackoutSqlitePersistenceTests(unittest.TestCase):
    def test_blackout_written_to_sqlite_and_enforced_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            store = SQLiteStore(db_path)
            catalog, bookings, clock, _ = make_services(store)
            calendar = make_calendar(store, clock)
            ids = seed_catalog(catalog)
            created = calendar.create_blackout(
                ids["resource_id"],
                {"reason": "寒暑假闭馆", "start_local": "2026-10-01T09:00:00", "end_local": "2026-10-01T12:00:00"},
            )
            store.close()

            # 直接检查 SQLite 里的原始行：本地挂钟时段与 UTC 边界都已落盘
            store_ro = SQLiteStore(db_path)
            row = store_ro.get(COLLECTION_BLACKOUTS, created["blackout_id"])
            self.assertIsNotNone(row)
            self.assertEqual(row["start_local"], "2026-10-01T09:00:00")
            self.assertEqual(row["end_local"], "2026-10-01T12:00:00")
            self.assertEqual(row["start_utc"], "2026-10-01T01:00:00+00:00")
            self.assertEqual(row["tz"], "Asia/Shanghai")

            # 模拟重启：全新服务实例挂载同一数据库，禁用窗口仍然生效
            from service_09252_008.application.booking_service import BookingService
            from service_09252_008.application.catalog_service import CatalogService
            from service_09252_008.application.ports import UuidIdGenerator

            catalog2 = CatalogService(store_ro, clock, UuidIdGenerator())
            bookings2 = BookingService(store_ro, clock, UuidIdGenerator())
            with self.assertRaises(BusinessRuleError) as ctx:
                bookings2.apply(apply_payload(ids, "k-blk-sqlite"))
            self.assertEqual(ctx.exception.details["blackout_start_local"], "2026-10-01T09:00:00")
            self.assertEqual(ctx.exception.details["blackout_reason"], "寒暑假闭馆")
            store_ro.close()


class BlackoutHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        catalog, bookings, clock, store = make_services()
        self.calendar = make_calendar(store, clock)
        self.ids = seed_catalog(catalog)
        self.server = create_server("127.0.0.1", 0, catalog, bookings, self.calendar)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_blackout_lifecycle_and_422_carries_original_period(self) -> None:
        payload = {
            "reason": "国庆设备检修",
            "start_local": "2026-10-01T09:00:00",
            "end_local": "2026-10-01T12:00:00",
        }
        status, created = self._request("POST", f"/resources/{self.ids['resource_id']}/blackouts", payload)
        self.assertEqual(status, 200)
        self.assertEqual(created["start_utc"], "2026-10-01T01:00:00+00:00")
        blackout_id = created["blackout_id"]

        status, listing = self._request("GET", f"/resources/{self.ids['resource_id']}/blackouts")
        self.assertEqual(status, 200)
        self.assertEqual([item["blackout_id"] for item in listing["items"]], [blackout_id])

        # 相交时段 -> 422，错误体带原始本地时段
        booking_body = {
            "idempotency_key": "http-blk-apply",
            "institution": "港城理工学院",
            "package_id": self.ids["package_id"],
            "mentor_id": self.ids["mentor_id"],
            "resource_id": self.ids["resource_id"],
            "window_id": self.ids["window_id"],
            "seats": 6,
            "slot_start": "2026-10-01T02:00:00+00:00",
            "slot_end": "2026-10-01T04:00:00+00:00",
        }
        status, body = self._request("POST", "/bookings", booking_body)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "business_rule_violation")
        self.assertEqual(body["details"]["blackout_start_local"], "2026-10-01T09:00:00")
        self.assertEqual(body["details"]["blackout_end_local"], "2026-10-01T12:00:00")
        self.assertEqual(body["details"]["venue_tz"], "Asia/Shanghai")

        # 删除禁用窗口后申请成功
        status, _ = self._request("DELETE", f"/venue-blackouts/{blackout_id}")
        self.assertEqual(status, 200)
        status, accepted = self._request("POST", "/bookings", booking_body)
        self.assertEqual(status, 201)
        self.assertEqual(accepted["status"], "REQUESTED")

    def test_offset_bearing_blackout_payload_is_400(self) -> None:
        status, body = self._request(
            "POST",
            f"/resources/{self.ids['resource_id']}/blackouts",
            {
                "reason": "x",
                "start_local": "2026-10-01T09:00:00+08:00",
                "end_local": "2026-10-01T12:00:00",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")


if __name__ == "__main__":
    unittest.main()
