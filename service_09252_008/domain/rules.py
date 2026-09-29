"""纯业务规则：前置培训、容量、安全等级、互斥资源、材料分配与运输周期。

本模块不触碰持久化，全部为可单测的纯函数。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .errors import BusinessRuleError, ValidationError
from .models import (
    QTY_EPS,
    Booking,
    CoursePackage,
    MaterialBatch,
    MaterialSafety,
    Mentor,
    PlannedAllocation,
    ReceptionWindow,
    VenueBlackoutWindow,
    WorkshopResource,
)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """半开区间重叠判断。"""
    return a_start < b_end and b_start < a_end


def ensure_slot_shape(package: CoursePackage, slot_start: datetime, slot_end: datetime) -> None:
    """时段必须完整落在课程包时长上且方向正确。"""
    if slot_end <= slot_start:
        raise ValidationError("slot_end must be after slot_start")
    minutes = (slot_end - slot_start).total_seconds() / 60
    if int(minutes) != package.duration_minutes:
        raise ValidationError(
            "slot duration must equal package duration",
            details={"expected_minutes": package.duration_minutes, "actual_minutes": int(minutes)},
        )


def ensure_mentor_qualified(mentor: Mentor, package: CoursePackage, slot_end: datetime) -> None:
    """前置培训：导师须持有课程要求的全部资格，且有效期覆盖课程结束时刻。"""
    missing: list[str] = []
    expired: list[str] = []
    for code in package.required_qualifications:
        until = mentor.qualifications.get(code)
        if until is None:
            missing.append(code)
        elif until < slot_end:
            expired.append(code)
    if missing or expired:
        raise BusinessRuleError(
            "mentor lacks required prerequisite training",
            details={"mentor_id": mentor.mentor_id, "missing": missing, "expired": expired},
        )


def ensure_window_fit(window: ReceptionWindow, slot_start: datetime, slot_end: datetime) -> None:
    if not window.contains(slot_start, slot_end):
        raise BusinessRuleError(
            "slot is outside the reception window",
            details={
                "window_id": window.window_id,
                "window_start": window.start.isoformat(),
                "window_end": window.end.isoformat(),
            },
        )


def ensure_resource_fit(resource: WorkshopResource, seats: int) -> None:
    """场地容量校验。"""
    if seats > resource.capacity:
        raise BusinessRuleError(
            "seats exceed workshop capacity",
            details={"resource_id": resource.resource_id, "capacity": resource.capacity, "seats": seats},
        )


def safety_ceiling(resource: WorkshopResource, window: ReceptionWindow) -> MaterialSafety:
    """场地与接待窗口共同决定的材料安全上限。"""
    return MaterialSafety(min(int(resource.safety_rating), int(window.allowed_safety)))


def plan_material_allocation(
    package: CoursePackage,
    seats: int,
    batches: list[MaterialBatch],
    *,
    now: datetime,
    slot_start: datetime,
    max_safety: MaterialSafety,
) -> list[PlannedAllocation]:
    """生成材料分配计划。

    规则：
    - 批次安全等级不得超过场地/窗口允许上限（材料安全）；
    - 批次须在开课前可送达：``now + lead_time <= slot_start``（跨境运输周期）；
    - 同种材料优先使用国内、运输周期短的批次；
    - 库存不足则抛出 ``BusinessRuleError``。
    """
    plan: list[PlannedAllocation] = []
    for req in package.materials:
        needed = req.quantity_per_seat * seats
        if needed <= 0:
            continue
        candidates = [
            b
            for b in batches
            if b.material_id == req.material_id
            and b.available_quantity > QTY_EPS
            and b.safety <= max_safety
            and now + timedelta(seconds=b.lead_time_seconds) <= slot_start
        ]
        if not candidates and any(b.material_id == req.material_id for b in batches):
            # 有该材料但全部被安全等级或运输周期排除，给出明确原因
            unsafe = [b for b in batches if b.material_id == req.material_id and b.safety > max_safety]
            late = [
                b
                for b in batches
                if b.material_id == req.material_id
                and b.safety <= max_safety
                and now + timedelta(seconds=b.lead_time_seconds) > slot_start
            ]
            if unsafe and not late:
                raise BusinessRuleError(
                    "material safety level exceeds venue allowance",
                    details={"material_id": req.material_id, "venue_safety_ceiling": int(max_safety)},
                )
            if late:
                earliest = min(
                    (now + timedelta(seconds=b.lead_time_seconds) for b in late),
                    default=None,
                )
                raise BusinessRuleError(
                    "shipping lead time exceeds time remaining before the slot",
                    details={
                        "material_id": req.material_id,
                        "slot_start": slot_start.isoformat(),
                        "earliest_arrival": earliest.isoformat() if earliest else None,
                    },
                )
        candidates.sort(key=lambda b: (b.cross_border, b.lead_time_seconds, b.batch_id))
        remaining = needed
        for batch in candidates:
            if remaining <= QTY_EPS:
                break
            take = min(batch.available_quantity, remaining)
            if take <= QTY_EPS:
                continue
            plan.append(PlannedAllocation(batch_id=batch.batch_id, material_id=req.material_id, quantity=take))
            remaining -= take
        if remaining > QTY_EPS:
            raise BusinessRuleError(
                "insufficient material stock",
                details={
                    "material_id": req.material_id,
                    "needed": needed,
                    "shortfall": round(remaining, 6),
                },
            )
    return plan


def resources_conflict(
    resource_a: WorkshopResource,
    resource_b: WorkshopResource,
) -> bool:
    """两个资源是否互斥：同一资源，或同处一个非空互斥组。"""
    if resource_a.resource_id == resource_b.resource_id:
        return True
    group = resource_a.mutex_group
    return group is not None and group == resource_b.mutex_group


def find_resource_conflict(
    candidate: Booking,
    resource: WorkshopResource,
    others: list[tuple[Booking, WorkshopResource]],
    holding_statuses: frozenset[BookingStatus] | set[BookingStatus],
) -> Booking | None:
    """在既有预约中寻找与候选预约时段重叠且资源互斥者。"""
    for other, other_resource in others:
        if other.booking_id == candidate.booking_id:
            continue
        if other.status not in holding_statuses:
            continue
        if not resources_conflict(resource, other_resource):
            continue
        if overlaps(candidate.slot_start, candidate.slot_end, other.slot_start, other.slot_end):
            return other
    return None


def find_blackout_conflict(
    slot_start: datetime,
    slot_end: datetime,
    blackouts: list[VenueBlackoutWindow],
) -> VenueBlackoutWindow | None:
    """找出与候选时段（半开区间、UTC）相交的场地禁用窗口。

    禁用窗口的 UTC 边界由场地本地挂钟时间解析得到，跨夏令时切换时物理时长
    已经正确，因此这里直接做半开区间求交，不依赖服务器本地时间。
    """
    for blackout in blackouts:
        if overlaps(slot_start, slot_end, blackout.start_utc, blackout.end_utc):
            return blackout
    return None


def ensure_not_blacked_out(
    slot_start: datetime,
    slot_end: datetime,
    blackouts: list[VenueBlackoutWindow],
) -> None:
    """候选时段不得与任何场地禁用窗口相交；冲突时硬拒绝并回带原始本地时段。"""
    blackout = find_blackout_conflict(slot_start, slot_end, blackouts)
    if blackout is None:
        return
    details = {
        "resource_id": blackout.resource_id,
        "blackout_id": blackout.blackout_id,
        "venue_tz": blackout.tz,
        "blackout_start_local": blackout.start_local.isoformat(),
        "blackout_end_local": blackout.end_local.isoformat(),
        "blackout_start_utc": blackout.start_utc.isoformat(),
        "blackout_end_utc": blackout.end_utc.isoformat(),
        "blackout_reason": blackout.reason,
        "slot_start": slot_start.isoformat(),
        "slot_end": slot_end.isoformat(),
    }
    if blackout.start_gap_adjusted or blackout.end_gap_adjusted:
        # 边界落在春季拨快缺口内被夹紧时显式告知，避免管理员误读原始时段。
        details["note"] = "blackout boundary falls in a spring-forward gap; UTC bounds were snapped forward"
    raise BusinessRuleError(
        "slot is blocked by a venue blackout window defined in the venue timezone",
        details=details,
    )
