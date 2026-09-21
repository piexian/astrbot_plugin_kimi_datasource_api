"""人工绑定的月刷新日、时间精度与日历推算。"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import ToolInputError

DEFAULT_TIMEZONE = "Asia/Shanghai"
SKIP_INPUTS = {"skip", "跳过", "略过"}


def reset_timezone(name: str):
    if re.fullmatch(r"[+-]\d{2}:\d{2}", name):
        hours, minutes = map(int, name[1:].split(":"))
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise ToolInputError("时区偏移无效。")
        return timezone(timedelta(minutes=(hours * 60 + minutes) * (1 if name[0] == "+" else -1)))
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        if name == DEFAULT_TIMEZONE:
            return timezone(timedelta(hours=8))
        raise ToolInputError("月度绑定的时区不可用。") from None


def month_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def next_month(value: date, day: int) -> date:
    year, month = (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)
    return month_date(year, month, day)


def parse_monthly_reset(value: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    text = value.strip()
    if not text or text.lower() in SKIP_INPUTS:
        return None
    if len(text) > 80:
        raise ToolInputError("月重置时间过长，请输入几号或日期时间。")
    zone_name = DEFAULT_TIMEZONE
    zone = reset_timezone(zone_name)
    current = (now or datetime.now(timezone.utc)).astimezone(zone)
    clock: time | None = None
    match = re.fullmatch(r"(?:每月\s*)?(\d{1,2})[日号]?(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?", text)
    try:
        if match:
            day = int(match[1])
            if not 1 <= day <= 31:
                raise ValueError()
            if match[2] is not None:
                clock = time(int(match[2]), int(match[3]), int(match[4] or 0))
            reference = month_date(current.year, current.month, day)
            if reference < current.date() or (
                clock is not None and datetime.combine(reference, clock, zone) <= current
            ):
                reference = next_month(reference, day)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            reference = date.fromisoformat(text)
            day = reference.day
        else:
            normalized = re.sub(r"\s+(?=[+-]\d{2}:\d{2}$)", "", text).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if "T" not in text and " " not in text:
                raise ValueError()
            if parsed.tzinfo is not None:
                offset = parsed.utcoffset()
                assert offset is not None
                minutes = int(offset.total_seconds() // 60)
                zone_name = f"{'+' if minutes >= 0 else '-'}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
                zone = reset_timezone(zone_name)
            reference, day, clock = parsed.date(), parsed.day, parsed.time().replace(microsecond=0, tzinfo=None)
        if not 1970 <= reference.year < 3000:
            raise ValueError()
    except (ValueError, OverflowError):
        raise ToolInputError("月重置时间格式无效：可填 22、22日、22 14:26 或 2026-09-22 14:26:58 +08:00。") from None
    return {
        "day": day,
        "time": clock.isoformat() if clock is not None else None,
        "timezone": zone_name,
        "next_date": reference.isoformat(),
        "precision": "second" if clock is not None else "date",
        "source": "manual",
        "recurring": True,
    }


def validate_monthly_reset(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ToolInputError("月度绑定必须是对象。")
    day = value.get("day")
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
        raise ToolInputError("月刷新日必须为 1–31。")
    zone = value.get("timezone", DEFAULT_TIMEZONE)
    if not isinstance(zone, str):
        raise ToolInputError("月度绑定时区无效。")
    reset_timezone(zone)
    try:
        reference = date.fromisoformat(value["next_date"])
        if not 1970 <= reference.year < 3000:
            raise ValueError()
        if reference.day != min(day, calendar.monthrange(reference.year, reference.month)[1]):
            raise ValueError()
        raw_clock = value.get("time")
        if raw_clock is None:
            clock = None
        else:
            if not isinstance(raw_clock, str) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", raw_clock):
                raise ValueError()
            clock = time.fromisoformat(raw_clock)
    except (KeyError, TypeError, ValueError):
        raise ToolInputError("月度绑定的日期或时间无效。") from None
    return {
        "day": day, "time": clock.isoformat() if clock else None, "timezone": zone,
        "next_date": reference.isoformat(), "precision": "second" if clock else "date",
        "source": "manual", "recurring": True,
    }


def reset_probe_boundary(value: dict[str, Any] | None) -> float | None:
    rule = validate_monthly_reset(value)
    if rule is None:
        return None
    clock = time.fromisoformat(rule["time"]) if rule["time"] else time.min
    # 仅日期时，零点只是最早复核边界，不是已知的额度恢复时刻。
    return datetime.combine(date.fromisoformat(rule["next_date"]), clock, reset_timezone(rule["timezone"])).timestamp()


def advance_monthly_reset(value: dict[str, Any] | None, *, now: float) -> dict[str, Any] | None:
    rule = validate_monthly_reset(value)
    if rule is None or reset_probe_boundary(rule) > now:
        return rule
    current = datetime.fromtimestamp(now, reset_timezone(rule["timezone"]))
    reference = month_date(current.year, current.month, rule["day"])
    clock = time.fromisoformat(rule["time"]) if rule["time"] else time.min
    if datetime.combine(reference, clock, current.tzinfo) <= current:
        reference = next_month(reference, rule["day"])
    return {**rule, "next_date": reference.isoformat()}


def describe_monthly_reset(value: dict[str, Any] | None) -> str:
    rule = validate_monthly_reset(value)
    if rule is None:
        return "月度重置时间：未绑定"
    if rule["time"] is None:
        return f"月度绑定：每月 {rule['day']} 日，具体时刻未知；下次预计日期 {rule['next_date']}（{rule['timezone']}，人工绑定）"
    return f"月度绑定：每月 {rule['day']} 日 {rule['time']}；下次预计 {rule['next_date']} {rule['time']}（{rule['timezone']}，人工绑定）"


def split_account_reset_args(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    return (parts[0], parts[1] if len(parts) > 1 else "") if parts else ("", "")
