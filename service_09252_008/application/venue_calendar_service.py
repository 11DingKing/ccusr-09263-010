"""场地日历服务：场地管理员维护“场地时区禁用窗口”。

禁用窗口的时间按**场地本地挂钟时间**登记（朴素 ISO 字符串，不带偏移），
时区以场地（``WorkshopResource.tz``）登记的 IANA 时区为准——服务器本地时间
不参与任何判断。登记时即用该时区（含夏令时规则）把边界解析为 UTC 一并持久化。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.errors import NotFoundError, ValidationError
from ..domain.models import VenueBlackoutWindow, WorkshopResource
from ..domain.venue_calendar import build_blackout
from ..persistence.store import Store
from .catalog_service import COLLECTION_RESOURCES
from .ports import Clock, IdGenerator

COLLECTION_BLACKOUTS = "venue_blackouts"


def _parse_naive_wall(value: Any, field: str) -> datetime:
    """解析禁用窗口边界：必须是朴素 ISO-8601 挂钟时间（禁止携带偏移）。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"field {field} must be a naive ISO-8601 local datetime string")
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(f"field {field} must be an ISO-8601 datetime: {exc}") from exc
    # 形如 2026-03-08T01:30:00-05:00 / ...+00:00 / ...Z 一律拒绝：
    # 禁用窗口的偏移只能由场地时区决定，不允许调用方自带。
    if parsed.tzinfo is not None:
        raise ValidationError(
            f"field {field} must not carry a UTC offset; specify venue-local wall time "
            "and let the venue tz resolve it",
            details={"field": field},
        )
    return parsed


class VenueCalendarService:
    """维护各场地的禁用窗口日历。"""

    def __init__(self, store: Store, clock: Clock, ids: IdGenerator) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids

    def _load_resource(self, resource_id: str) -> WorkshopResource:
        record = self._store.get(COLLECTION_RESOURCES, resource_id)
        if record is None:
            raise NotFoundError(f"resource not found: {resource_id}", details={"resource_id": resource_id})
        return WorkshopResource.from_dict(record)

    def create_blackout(self, resource_id: str, request: dict[str, Any]) -> dict[str, Any]:
        resource = self._load_resource(resource_id)
        reason = request.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError("field reason must be a non-empty string", details={"field": "reason"})
        start_local = _parse_naive_wall(request.get("start_local"), "start_local")
        end_local = _parse_naive_wall(request.get("end_local"), "end_local")
        # 时区以场地登记为准；若载荷显式给出 tz，必须与场地一致，防止管理员挂错日历。
        tz_name = resource.tz
        provided_tz = request.get("tz")
        if provided_tz is not None and provided_tz != tz_name:
            raise ValidationError(
                "tz does not match the venue timezone",
                details={"resource_id": resource_id, "venue_tz": tz_name, "provided_tz": provided_tz},
            )
        blackout = build_blackout(
            blackout_id=self._ids.new_id("blk"),
            resource_id=resource.resource_id,
            reason=reason.strip(),
            tz_name=tz_name,
            start_local=start_local,
            end_local=end_local,
        )
        with self._store.transaction():
            self._store.put(COLLECTION_BLACKOUTS, blackout.blackout_id, blackout.to_dict())
        return blackout.to_dict()

    def list_blackouts(self, resource_id: str | None = None) -> list[dict[str, Any]]:
        records = self._store.query(COLLECTION_BLACKOUTS)
        if resource_id is not None:
            records = [r for r in records if r["resource_id"] == resource_id]
        records.sort(key=lambda r: (r["resource_id"], r["start_utc"], r["blackout_id"]))
        return records

    def delete_blackout(self, blackout_id: str) -> dict[str, Any]:
        record = self._store.get(COLLECTION_BLACKOUTS, blackout_id)
        if record is None:
            raise NotFoundError(f"blackout not found: {blackout_id}", details={"blackout_id": blackout_id})
        with self._store.transaction():
            self._store.delete(COLLECTION_BLACKOUTS, blackout_id)
        return {"deleted": blackout_id}
