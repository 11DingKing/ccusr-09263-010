"""场地时区时间窗口解析（纯函数，可单测）。

禁用窗口由场地管理员按**场地本地挂钟时间**登记（朴素 ``datetime`` + IANA 时区），
本模块负责把本地时间解析为带时区的 UTC 边界。解析必须经场地时区规则，
**绝不允许用服务器本地时间代替**。

夏令时边界语义：
- 春季拨快（墙上时间跳过一段，如纽约 2026-03-08 02:00→03:00）：落在缺口内的
  挂钟时间不存在，夹紧到切换后的第一个有效时刻；
- 秋季拨回（墙上时间重复一段，如纽约 2026-11-01 02:00→01:00）：重复的挂钟时间
  取第一次出现（偏移较大的那一次，EDT）。

跨夏令时切换的窗口据此得到正确物理时长（例如本地 01:30–03:30 跨过春季
拨快点时，物理长度是 1 小时而非 2 小时），与候选时段求交时不会误判。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ValidationError
from .models import VenueBlackoutWindow

# 二分解析时的精度上限：DST 切换均发生在整秒，1 秒足够，且避免无限循环。
_RESOLUTION = timedelta(seconds=1)


def get_zone(tz_name: str) -> ZoneInfo:
    """按名称取 IANA 时区，非法名称报校验错误。"""
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError(f"unknown IANA timezone: {tz_name}", details={"tz": tz_name}) from exc
    return zone


def require_naive_wall_time(value: datetime, field: str) -> datetime:
    """登记禁用窗口只接受朴素挂钟时间：时段按场地时区定义，不接受外带偏移。"""
    if not isinstance(value, datetime):
        raise ValidationError(f"field {field} must be a datetime", details={"field": field})
    if value.tzinfo is not None:
        raise ValidationError(
            f"field {field} must be a naive venue-local wall time; the venue tz supplies the offset",
            details={"field": field},
        )
    return value


def resolve_wall_time(local: datetime, zone: ZoneInfo) -> tuple[datetime, bool]:
    """把场地本地挂钟时间解析为 UTC 时刻。

    返回 ``(utc_instant, gap_adjusted)``：挂钟时间落在春季拨快缺口时
    ``gap_adjusted=True`` 且时刻夹紧到切换后的第一个有效时刻；秋季重复时间
    取第一次出现（``fold=0``）。

    PEP 495：缺口内 ``fold=0`` 按切换前偏移换算（落到切换之后），``fold=1``
    按切换后偏移换算（落到切换之前），两个时刻恰好夹住真实切换点，据此二分
    求“墙上时间首次到达 local”的最早时刻。
    """
    first = local.replace(tzinfo=zone, fold=0)
    instant = first.astimezone(timezone.utc)
    if instant.astimezone(zone).replace(tzinfo=None) == local:
        return instant, False

    # 在春季拨快缺口内：fold=0/1 两个换算时刻分别位于切换点两侧
    other = local.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
    lo, hi = min(instant, other), max(instant, other)
    while hi - lo > _RESOLUTION:
        mid = lo + (hi - lo) / 2
        if mid.astimezone(zone).replace(tzinfo=None) < local:
            lo = mid
        else:
            hi = mid
    return hi, True


def build_blackout(
    *,
    blackout_id: str,
    resource_id: str,
    reason: str,
    tz_name: str,
    start_local: datetime,
    end_local: datetime,
) -> VenueBlackoutWindow:
    """校验并构造禁用窗口：本地挂钟时间 -> UTC 边界。"""
    require_naive_wall_time(start_local, "start_local")
    require_naive_wall_time(end_local, "end_local")
    if end_local <= start_local:
        raise ValidationError(
            "blackout end_local must be after start_local",
            details={"start_local": start_local.isoformat(), "end_local": end_local.isoformat()},
        )
    zone = get_zone(tz_name)
    start_utc, start_gap = resolve_wall_time(start_local, zone)
    end_utc, end_gap = resolve_wall_time(end_local, zone)
    if end_utc <= start_utc:
        raise ValidationError(
            "blackout resolves to an empty instant interval after DST resolution",
            details={"tz": tz_name, "start_utc": start_utc.isoformat(), "end_utc": end_utc.isoformat()},
        )
    return VenueBlackoutWindow(
        blackout_id=blackout_id,
        resource_id=resource_id,
        reason=reason,
        tz=tz_name,
        start_local=start_local,
        end_local=end_local,
        start_utc=start_utc,
        end_utc=end_utc,
        start_gap_adjusted=start_gap,
        end_gap_adjusted=end_gap,
    )
