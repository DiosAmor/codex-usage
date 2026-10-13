"""Normalize account daily totals without dates or large integers losing precision."""

from datetime import date
import re


MAX_TOKEN_COUNT = 2 ** 63 - 1
DAILY_USAGE_ERROR = "일별 토큰 사용량을 조회하지 못했습니다. Codex CLI 버전과 로그인·네트워크를 확인해 주세요."
INVALID_DAILY_USAGE = "일별 토큰 기록의 형식을 확인할 수 없습니다."


def pending_token_usage():
    return {"status": "pending", "daily": [], "fetchedAt": None,
            "lastAttemptAt": None, "error": None, "dateBasis": "server"}


def token_count(value):
    """Keep unknown values unknown and preserve signed int64 totals for JavaScript."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_TOKEN_COUNT:
        return str(value)
    return None


def normalize_daily_usage(data):
    """Return only account daily buckets; summary and thread details are not retained."""
    if not isinstance(data, dict):
        raise ValueError(INVALID_DAILY_USAGE)
    raw = data.get("dailyUsageBuckets")
    if raw is None:
        return {"status": "unavailable", "daily": []}
    if not isinstance(raw, list):
        raise ValueError(INVALID_DAILY_USAGE)
    values = {}
    conflicting = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(INVALID_DAILY_USAGE)
        day = item.get("startDate")
        if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
            raise ValueError(INVALID_DAILY_USAGE)
        try:
            date.fromisoformat(day)
        except ValueError:
            raise ValueError(INVALID_DAILY_USAGE) from None
        value = token_count(item.get("tokens"))
        if day in values and values[day] != value:
            conflicting.add(day)
        values[day] = value
    return {"status": "ok", "daily": [
        {"date": day, "tokens": None if day in conflicting else values[day]}
        for day in sorted(values)]}
