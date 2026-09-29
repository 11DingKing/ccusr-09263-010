"""场地时区禁用窗口（venue blockout calendar）。

核心约束：
- 禁用窗口由场地管理员按**场地时区的墙上时钟**登记（朴素本地时间），
  绝不能用服务器本地时间或固定 UTC 偏移代替；
- 夏令时切换日（春季缺口 / 秋季重叠小时）按场地墙上时钟判定；
- 拒绝原因携带管理员登记的原始本地时段；
- 窗口按场地时区计算后写入 SQLite，重启仍然生效。

America/New_York：
- 2027-03-14 02:00 EST -> 03:00 EDT（春令时，本地 02:00-03:00 不存在）；
- 2027-11-07 02:00 EDT -> 01:00 EST（秋令时，本地 01:00-02:00 出现两次）。
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
from datetime import datetime

from service_09252_008.application.booking_service import BookingService
from service_09252_008.application.catalog_service import COLLECTION_BLOCKOUTS, CatalogService
from service_09252_008.application.ports import ManualClock, UuidIdGenerator
from service_09252_008.domain.errors import NotFoundError, ValidationError, VenueBlockedError
from service_09252_008.domain.models import VenueBlockout
from service_09252_008.domain.rules import ensure_slot_not_blocked, venue_wallclock
from service_09252_008.interfaces.http_api import create_server
from service_09252_008.persistence.sqlite_store import SQLiteStore
from tests.helpers import NOW, make_services

NY_TZ = "America/New_York"
SPRING = "2027-03-14"  # 春令时切换日
FALL = "2027-11-07"  # 秋令时切换日


def seed_nyc(catalog: CatalogService, *, day: str) -> dict[str, str]:
    """登记一套纽约场地目录（无材料，2 小时课程），窗口覆盖给定 DST 日。"""
    package = catalog.create_package(
        {
            "name": "NYC workshop",
            "craft": "craft",
            "duration_minutes": 120,
            "max_seats": 20,
            "required_qualifications": ["nyc-craft"],
            "materials": [],
        }
    )
    mentor = catalog.create_mentor(
        {
            "name": "Sam",
            "home_tz": NY_TZ,
            "hourly_fee_cents": 1,
            "qualifications": {"nyc-craft": "2030-01-01T00:00:00+00:00"},
        }
    )
    resource = catalog.create_resource(
        {
            "name": "NYC studio",
            "capacity": 30,
            "safety_rating": 3,
            "mutex_group": None,
            "tz": NY_TZ,
            "hourly_fee_cents": 1,
        }
    )
    # 宽窗口覆盖切换日前后；ISO 偏移是绝对时刻，跨不跨 DST 都正确
    y, m, d = map(int, day.split("-"))
    window = catalog.create_reception_window(
        {
            "institution": "NYC School",
            "tz": NY_TZ,
            "start": f"{day}T00:00:00-05:00",
            "end": f"{day}T23:59:00-04:00",
            "capacity": 10,
            "allowed_safety": 3,
        }
    )
    return {
        "package_id": package["package_id"],
        "mentor_id": mentor["mentor_id"],
        "resource_id": resource["resource_id"],
        "window_id": window["window_id"],
    }


def apply(bookings: BookingService, ids: dict[str, str], key: str, start_utc: str, end_utc: str):
    return bookings.apply(
        {
            "idempotency_key": key,
            "institution": "NYC College",
            "package_id": ids["package_id"],
            "mentor_id": ids["mentor_id"],
            "resource_id": ids["resource_id"],
            "window_id": ids["window_id"],
            "seats": 10,
            "slot_start": start_utc,
            "slot_end": end_utc,
        }
    )


def blockout(records: list[VenueBlockout], local_start: str, local_end: str) -> VenueBlockout:
    return VenueBlockout(
        blockout_id="blk_test",
        resource_id="res_test",
        tz=NY_TZ,
        local_start=datetime.fromisoformat(local_start),
        local_end=datetime.fromisoformat(local_end),
        reason="maintenance",
        created_at=NOW,
    )


class WallClockProjectionTests(unittest.TestCase):
    def test_projection_uses_dst_offset_not_fixed_offset(self) -> None:
        # 春令时 07:00Z 在纽约是 03:00 EDT(-4)，而不是固定 -5 算出的 02:00
        self.assertEqual(
            venue_wallclock(datetime.fromisoformat("2027-03-14T07:00:00+00:00"), NY_TZ),
            datetime(2027, 3, 14, 3, 0),
        )
        # 秋令时 06:30Z 落在回退后的第二次 01:30 EST(-5)
        self.assertEqual(
            venue_wallclock(datetime.fromisoformat("2027-11-07T06:30:00+00:00"), NY_TZ),
            datetime(2027, 11, 7, 1, 30),
        )

    @unittest.skipUnless(hasattr(time, "tzset"), "tzset required to simulate server timezone")
    def test_judgement_independent_of_server_timezone(self) -> None:
        slot_start = datetime.fromisoformat("2027-03-14T07:00:00+00:00")
        slot_end = datetime.fromisoformat("2027-03-14T09:00:00+00:00")
        blk = blockout([], f"{SPRING}T03:00:00", f"{SPRING}T04:00:00")
        verdicts: list[bool] = []
        for server_tz in ("UTC", "Asia/Shanghai", "Pacific/Honolulu", "America/Los_Angeles"):
            old_tz = os.environ.get("TZ")
            os.environ["TZ"] = server_tz
            try:
                time.tzset()
                hit = False
                try:
                    ensure_slot_not_blocked(slot_start, slot_end, [blk])
                except VenueBlockedError:
                    hit = True
                verdicts.append(hit)
            finally:
                if old_tz is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = old_tz
                time.tzset()
        # 无论服务器运行在哪个时区，结论一致：撞上禁用窗口
        self.assertEqual(verdicts, [True, True, True, True])


class SpringForwardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_nyc(self.catalog, day=SPRING)

    def test_blockout_after_spring_gap_rejected_in_venue_wallclock(self) -> None:
        # 本地 03:00-04:00（春令时跳变后，EDT -4）== 07:00-08:00 UTC
        created = self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00",
                "local_end": f"{SPRING}T04:00:00",
                "reason": "elevator maintenance",
            }
        )
        # 持久化的是管理员登记的原始朴素本地时段，不含任何偏移
        self.assertEqual(created["local_start"], f"{SPRING}T03:00:00")
        self.assertEqual(created["tz"], NY_TZ)

        # UTC 07:00-09:00 投影为纽约 03:00-05:00 EDT，与 03:00-04:00 重叠 -> 拒绝
        with self.assertRaises(VenueBlockedError) as ctx:
            apply(
                self.bookings,
                self.ids,
                "k-blk-spring-hit",
                f"{SPRING}T07:00:00+00:00",
                f"{SPRING}T09:00:00+00:00",
            )
        details = ctx.exception.details
        self.assertEqual(details["venue_tz"], NY_TZ)
        # 错误响应带原始时段（朴素墙上时钟，原样回传）
        self.assertEqual(details["blockout_local_start"], f"{SPRING}T03:00:00")
        self.assertEqual(details["blockout_local_end"], f"{SPRING}T04:00:00")
        self.assertEqual(details["blockout_reason"], "elevator maintenance")
        # 同时给出申请时段在场地时区的投影，便于定位
        self.assertEqual(details["slot_venue_local_start"], f"{SPRING}T03:00:00")
        self.assertEqual(details["slot_venue_local_end"], f"{SPRING}T05:00:00")

    def test_fixed_offset_would_wrongly_accept_but_venue_tz_rejects(self) -> None:
        # 管理员停用本地 03:00-04:00；春令时该段为 EDT(-4)，即 UTC 07:00-08:00。
        # 若错用冬季固定偏移 EST(-5)，会把停用段误算成 UTC 08:00-09:00。
        self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00",
                "local_end": f"{SPRING}T04:00:00",
            }
        )
        # UTC 06:00-08:00（物理 2 小时）投影到纽约墙上为 01:00 EST -> 04:00 EDT，
        # 跨越春季缺口，覆盖墙上 [03:00,04:00)，正确按场地时区判定 -> 拒绝；
        # 而固定 -5 的错误算法把停用段放在 UTC [08:00,09:00)，与本时段半开相切，
        # 会错误放行。
        with self.assertRaises(VenueBlockedError) as ctx:
            apply(
                self.bookings,
                self.ids,
                "k-blk-spring-fixed",
                f"{SPRING}T06:00:00+00:00",
                f"{SPRING}T08:00:00+00:00",
            )
        self.assertEqual(ctx.exception.details["slot_venue_local_start"], f"{SPRING}T01:00:00")
        self.assertEqual(ctx.exception.details["slot_venue_local_end"], f"{SPRING}T04:00:00")

    def test_slot_started_at_blockout_end_is_allowed_half_open(self) -> None:
        # 半开区间：本地 04:00 整开始的课与 [03:00,04:00) 相切，放行
        self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00",
                "local_end": f"{SPRING}T04:00:00",
            }
        )
        view = apply(
            self.bookings,
            self.ids,
            "k-blk-spring-touch",
            f"{SPRING}T08:00:00+00:00",  # 本地 04:00 EDT
            f"{SPRING}T10:00:00+00:00",
        )
        self.assertEqual(view["status"], "REQUESTED")

    def test_slot_fully_before_blockout_is_allowed(self) -> None:
        self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00",
                "local_end": f"{SPRING}T04:00:00",
            }
        )
        # UTC 05:00-07:00（物理 2 小时）在纽约墙上投影为 00:00 EST -> 03:00 EDT，
        # 跨过春季缺口，墙上末端恰为停用段起点，半开区间相切 -> 放行
        view = apply(
            self.bookings,
            self.ids,
            "k-blk-spring-before",
            f"{SPRING}T05:00:00+00:00",
            f"{SPRING}T07:00:00+00:00",
        )
        self.assertEqual(view["status"], "REQUESTED")


class FallBackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_nyc(self.catalog, day=FALL)

    def test_repeated_local_hour_both_instances_blocked(self) -> None:
        # 本地 01:30-02:30：秋令时这一墙上时段物理上出现两次。
        self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{FALL}T01:30:00",
                "local_end": f"{FALL}T02:30:00",
                "reason": "power cut",
            }
        )
        # 第一次实例：UTC 05:30-07:30（01:30 EDT -> 02:30 EST），墙上正等于停用段
        with self.assertRaises(VenueBlockedError) as first:
            apply(
                self.bookings, self.ids, "k-blk-fall-1",
                f"{FALL}T05:30:00+00:00", f"{FALL}T07:30:00+00:00",
            )
        self.assertEqual(first.exception.details["blockout_local_start"], f"{FALL}T01:30:00")
        # 第二次实例：UTC 06:30-08:30，墙上 01:30(第二次) -> 03:30，重叠停用段。
        # 若错误使用夏令时固定偏移 -4，会把停用段算成 05:30-06:30 UTC，从而误放本段。
        with self.assertRaises(VenueBlockedError):
            apply(
                self.bookings, self.ids, "k-blk-fall-2",
                f"{FALL}T06:30:00+00:00", f"{FALL}T08:30:00+00:00",
            )

    def test_slot_started_at_blockout_end_allowed(self) -> None:
        self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{FALL}T01:30:00",
                "local_end": f"{FALL}T02:30:00",
            }
        )
        # UTC 07:30-09:30 == 本地 02:30-04:30 EST，起点恰为停用段末端，放行
        view = apply(
            self.bookings, self.ids, "k-blk-fall-after",
            f"{FALL}T07:30:00+00:00", f"{FALL}T09:30:00+00:00",
        )
        self.assertEqual(view["status"], "REQUESTED")


class BlockoutValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_nyc(self.catalog, day=SPRING)

    def test_offset_local_time_rejected(self) -> None:
        # 禁用窗口必须是朴素场地本地时间；携带偏移会掩盖“按场地时区”的语义
        with self.assertRaises(ValidationError):
            self.catalog.create_venue_blockout(
                {
                    "resource_id": self.ids["resource_id"],
                    "local_start": f"{SPRING}T03:00:00-04:00",
                    "local_end": f"{SPRING}T04:00:00-04:00",
                }
            )

    def test_inverted_range_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.catalog.create_venue_blockout(
                {
                    "resource_id": self.ids["resource_id"],
                    "local_start": f"{SPRING}T04:00:00",
                    "local_end": f"{SPRING}T03:00:00",
                }
            )

    def test_unknown_resource_rejected(self) -> None:
        with self.assertRaises(NotFoundError):
            self.catalog.create_venue_blockout(
                {
                    "resource_id": "res_missing",
                    "local_start": f"{SPRING}T03:00:00",
                    "local_end": f"{SPRING}T04:00:00",
                }
            )

    def test_blockout_scoped_per_resource(self) -> None:
        # 停用 A 场地不影响同一时刻使用 B 场地
        self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00",
                "local_end": f"{SPRING}T05:00:00",
            }
        )
        other = self.catalog.create_resource(
            {
                "name": "NYC studio B",
                "capacity": 30,
                "safety_rating": 3,
                "tz": NY_TZ,
                "hourly_fee_cents": 1,
            }
        )
        view = apply(
            self.bookings,
            {**self.ids, "resource_id": other["resource_id"]},
            "k-blk-other-resource",
            f"{SPRING}T07:00:00+00:00",
            f"{SPRING}T09:00:00+00:00",
        )
        self.assertEqual(view["status"], "REQUESTED")

    def test_delete_lifts_blockout(self) -> None:
        created = self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00",
                "local_end": f"{SPRING}T05:00:00",
            }
        )
        with self.assertRaises(VenueBlockedError):
            apply(
                self.bookings, self.ids, "k-blk-del-hit",
                f"{SPRING}T07:00:00+00:00", f"{SPRING}T09:00:00+00:00",
            )
        self.catalog.delete_venue_blockout(created["blockout_id"])
        view = apply(
            self.bookings, self.ids, "k-blk-del-ok",
            f"{SPRING}T07:00:00+00:00", f"{SPRING}T09:00:00+00:00",
        )
        self.assertEqual(view["status"], "REQUESTED")


class RescheduleBlockoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog, self.bookings, self.clock, self.store = make_services()
        self.ids = seed_nyc(self.catalog, day=SPRING)

    def test_reschedule_into_blockout_rejected_with_raw_window(self) -> None:
        applied = apply(
            self.bookings, self.ids, "k-blk-rs-init",
            f"{SPRING}T09:00:00+00:00", f"{SPRING}T11:00:00+00:00",  # 本地 05:00-07:00
        )
        self.bookings.quote(applied["booking_id"])
        self.catalog.create_venue_blockout(
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00",
                "local_end": f"{SPRING}T05:00:00",
                "reason": "fire drill",
            }
        )
        with self.assertRaises(VenueBlockedError) as ctx:
            self.bookings.reschedule(
                applied["booking_id"],
                {
                    "idempotency_key": "k-blk-rs-move",
                    "slot_start": f"{SPRING}T07:00:00+00:00",  # 本地 03:00 EDT
                    "slot_end": f"{SPRING}T09:00:00+00:00",
                },
            )
        self.assertEqual(ctx.exception.details["blockout_reason"], "fire drill")
        self.assertEqual(ctx.exception.details["blockout_local_start"], f"{SPRING}T03:00:00")
        # 被拒后预约保持原状
        self.assertEqual(self.bookings.get_booking(applied["booking_id"])["status"], "QUOTED")


class HttpBlockoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog, cls.bookings, cls.clock, cls.store = make_services()
        cls.ids = seed_nyc(cls.catalog, day=SPRING)
        cls.server = create_server("127.0.0.1", 0, cls.catalog, cls.bookings)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

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

    def test_blockout_rejection_over_http_carries_raw_window(self) -> None:
        status, blk = self._request(
            "POST",
            "/venue-blockouts",
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00",
                "local_end": f"{SPRING}T04:00:00",
                "reason": "elevator inspection",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(blk["tz"], NY_TZ)

        apply_body = {
            "idempotency_key": "http-blk-apply",
            "institution": "NYC College",
            "package_id": self.ids["package_id"],
            "mentor_id": self.ids["mentor_id"],
            "resource_id": self.ids["resource_id"],
            "window_id": self.ids["window_id"],
            "seats": 10,
            "slot_start": f"{SPRING}T07:00:00+00:00",
            "slot_end": f"{SPRING}T09:00:00+00:00",
        }
        status, body = self._request("POST", "/bookings", apply_body)
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "venue_blocked")
        self.assertEqual(body["details"]["blockout_local_start"], f"{SPRING}T03:00:00")
        self.assertEqual(body["details"]["blockout_local_end"], f"{SPRING}T04:00:00")
        self.assertEqual(body["details"]["blockout_reason"], "elevator inspection")
        self.assertEqual(body["details"]["venue_tz"], NY_TZ)

    def test_naive_only_validation_over_http(self) -> None:
        status, body = self._request(
            "POST",
            "/venue-blockouts",
            {
                "resource_id": self.ids["resource_id"],
                "local_start": f"{SPRING}T03:00:00-04:00",
                "local_end": f"{SPRING}T04:00:00-04:00",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "validation_error")


class SQLiteBlockoutPersistenceTests(unittest.TestCase):
    def test_blockout_written_in_venue_time_and_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = f"{tmp}/booking.db"
            clock = ManualClock(NOW)
            store = SQLiteStore(db_path)
            catalog = CatalogService(store, clock, UuidIdGenerator())
            bookings = BookingService(store, clock, UuidIdGenerator())
            ids = seed_nyc(catalog, day=SPRING)
            created = catalog.create_venue_blockout(
                {
                    "resource_id": ids["resource_id"],
                    "local_start": f"{SPRING}T03:00:00",
                    "local_end": f"{SPRING}T04:00:00",
                    "reason": "maintenance",
                }
            )

            # 直接核对 SQLite 中落库的是原始场地本地时段（无偏移）与场地时区
            row = store.get(COLLECTION_BLOCKOUTS, created["blockout_id"])
            assert row is not None
            self.assertEqual(row["local_start"], f"{SPRING}T03:00:00")
            self.assertNotIn("+", row["local_start"])
            self.assertEqual(row["tz"], NY_TZ)
            store.close()

            # 重启：全新服务实例挂载同一数据库，禁用窗口按场地时区仍然生效
            store2 = SQLiteStore(db_path)
            catalog2 = CatalogService(store2, clock, UuidIdGenerator())
            bookings2 = BookingService(store2, clock, UuidIdGenerator())
            self.assertEqual(len(catalog2.list_venue_blockouts()), 1)
            with self.assertRaises(VenueBlockedError) as ctx:
                apply(
                    bookings2, ids, "k-blk-sqlite",
                    f"{SPRING}T07:00:00+00:00", f"{SPRING}T09:00:00+00:00",
                )
            self.assertEqual(ctx.exception.details["blockout_local_start"], f"{SPRING}T03:00:00")
            store2.close()


if __name__ == "__main__":
    unittest.main()
