from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def beijing_today() -> date:
    return datetime.now(BEIJING_TIMEZONE).date()


def beijing_day_utc_range(value: date) -> tuple[datetime, datetime]:
    """Return naive UTC bounds matching the project's existing UTC timestamps."""
    local_start = datetime.combine(value, time.min, tzinfo=BEIJING_TIMEZONE)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = local_end.astimezone(timezone.utc).replace(tzinfo=None)
    return utc_start, utc_end


def beijing_date_span_utc_range(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    utc_start, _ = beijing_day_utc_range(start_date)
    _, utc_end = beijing_day_utc_range(end_date)
    return utc_start, utc_end
