from __future__ import annotations

from apscheduler.triggers.cron import CronTrigger


def build_trigger(schedule: str, timezone):
    normalized = (schedule or "manual").strip().lower()
    if normalized == "manual":
        return None
    if normalized == "hourly":
        return CronTrigger(minute=0, timezone=timezone)
    if normalized == "daily":
        return CronTrigger(hour=2, minute=0, timezone=timezone)
    if normalized == "weekly":
        return CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=timezone)
    if normalized.startswith("cron:"):
        return CronTrigger.from_crontab(normalized.split(":", 1)[1].strip(), timezone=timezone)
    return CronTrigger.from_crontab(normalized, timezone=timezone)


def describe_schedule(schedule: str) -> str:
    normalized = (schedule or "manual").strip().lower()
    presets = {
        "manual": "Только вручную",
        "hourly": "Каждый час в 00 минут",
        "daily": "Каждый день в 02:00",
        "weekly": "Каждое воскресенье в 03:00",
    }
    if normalized in presets:
        return presets[normalized]
    if normalized.startswith("cron:"):
        return f"Cron: {normalized.split(':', 1)[1].strip()}"
    return f"Cron: {normalized}"
