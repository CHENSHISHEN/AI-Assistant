"""时间格式：对用户展示统一用东八区（UTC+8），内部存储一律 UTC。"""
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def hhmm_cst(dt: datetime) -> str:
    return dt.astimezone(CST).strftime("%H:%M")
