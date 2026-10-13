"""Isolated, read-only public reset announcements and unofficial forecasts.

This module has no account-store dependency. Only the two fixed public URLs
below are requested, with no cookies, account headers, or credentials.
"""

import copy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


SOURCE_URL = "https://codex-resets.com/"
STATUS_URL = SOURCE_URL + "api/v1/status"
HISTORY_URL = SOURCE_URL + "api/v1/resets?limit=100"
REQUEST_TIMEOUT = 8
MAX_RESPONSE_BYTES = 1024 * 1024
_ISO = r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})"
_DURATION = r"(\d+(?:\.\d+)?)"
_UNIT = r"(hours?|hrs?|h|days?|d)"
_PREFIX = r"(?:within(?: the)?(?: next)?|(?:the )?next)\s+"
_RANGE = re.compile(_PREFIX + _DURATION + r"\s*(?:-|–|—|to)\s*" + _DURATION + r"\s*" + _UNIT + r"\.?", re.I)
_WITHIN = re.compile(_PREFIX + _DURATION + r"\s*" + _UNIT + r"\.?", re.I)
_KOREAN = re.compile(r"(?:(?:앞으로|향후)\s*)?" + _DURATION + r"\s*시간\s*이내\.?")


class FeedError(Exception):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Following a provider-supplied URL would broaden the fixed-URL scope.
        return None


def _text(value, limit=12000):
    if not isinstance(value, str):
        return None
    return "".join(c for c in value[:limit] if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) <= 159))


def _url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            return None
        return value
    except ValueError:
        return None


def _timestamp(value):
    """Parse an explicit offset-bearing timestamp, never a local/naive date."""
    if not isinstance(value, str) or not re.fullmatch(_ISO, value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("z", "+00:00").replace("Z", "+00:00"))
        result = parsed.timestamp()
        return result if parsed.tzinfo is not None and math.isfinite(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def forecast_bounds(value, observed_at):
    """Interpret only complete, explicit forecast windows.

    observed_at anchors recognized relative durations. expires_at is deliberately
    not an input: it is the classifier's validity deadline, not its forecast.
    """
    if not isinstance(value, str):
        return None, None, False
    phrase = value.strip()
    absolute = re.fullmatch(r"(?:from\s+)?(" + _ISO + r")(?:\s*/\s*|\s+to\s+|\s+[–—]\s+)(" + _ISO + r")", phrase, re.I)
    if absolute:
        start, end = (_timestamp(part) for part in absolute.groups())
        if start is not None and end is not None and start <= end:
            return start, end, False
        return None, None, False
    point = _timestamp(phrase)
    if point is not None:
        return point, point, False
    if observed_at is None:
        return None, None, False
    ranged = _RANGE.fullmatch(phrase)
    within = _WITHIN.fullmatch(phrase)
    korean = _KOREAN.fullmatch(phrase)
    if ranged:
        lower, upper, unit = ranged.groups()
        lower, upper = float(lower), float(upper)
    elif within:
        upper, unit = within.groups()
        lower, upper = 0, float(upper)
    elif korean:
        lower, upper, unit = 0, float(korean.group(1)), "h"
    else:
        return None, None, False
    multiplier = 86400 if unit.lower().startswith("d") else 3600
    if not (math.isfinite(lower) and math.isfinite(upper) and 0 <= lower <= upper and 0 < upper * multiplier <= 30 * 86400):
        return None, None, False
    return observed_at + lower * multiplier, observed_at + upper * multiplier, True


def _require_object(value):
    if not isinstance(value, dict):
        raise FeedError("공개 초기화 정보의 형식을 확인하지 못했습니다.")
    return value


def _require_keys(value, keys):
    if not all(key in value for key in keys):
        raise FeedError("공개 초기화 정보의 형식을 확인하지 못했습니다.")


def _date(value, nullable=False):
    result = _timestamp(value)
    if result is None and not (nullable and value is None):
        raise FeedError("공개 초기화 정보의 날짜 형식을 확인하지 못했습니다.")
    return result


def _source(value):
    value = _require_object(value)
    if value.get("type") not in ("x_post", "observed"):
        raise FeedError("공개 초기화 정보의 출처 형식을 확인하지 못했습니다.")
    if value["type"] == "x_post" and (not isinstance(value.get("author"), str) or not isinstance(value.get("url"), str)):
        raise FeedError("공개 초기화 정보의 출처 형식을 확인하지 못했습니다.")
    return _url(value.get("url"))


def _event(identifier, kind, title, text, source_url, **values):
    result = {"id": identifier, "kind": kind, "title": title, "text": text,
              "startsAt": None, "endsAt": None, "expiresAt": None,
              "forecastStartsAt": None, "forecastEndsAt": None,
              "chancePercent": None, "level": None, "sourceUrl": source_url,
              "forecastWindow": None, "resetType": None, "announcedAt": None,
              "observedAt": None, "derivedWindow": False, "expired": False,
              "passed": False, "actionable": False}
    result.update(values)
    return result


def _reset(value, scheduled=False):
    value = _require_object(value)
    _require_keys(value, ("id", "reset_type", "announced_at", "text", "source"))
    identifier = value["id"]
    reset_type = value["reset_type"]
    text = _text(value["text"])
    if not isinstance(identifier, str) or not identifier or len(identifier) > 64 or _text(identifier) != identifier or reset_type not in ("regular", "banked") or text is None:
        raise FeedError("공개 초기화 정보의 형식을 확인하지 못했습니다.")
    announced = _date(value["announced_at"])
    source = _source(value["source"])
    if scheduled:
        _require_keys(value, ("status", "scheduled_for"))
        if value["status"] != "scheduled":
            raise FeedError("공개 초기화 예고의 형식을 확인하지 못했습니다.")
        starts = _date(value["scheduled_for"], nullable=True)
        return _event(identifier, "scheduled", "예고된 초기화", text, source,
                      startsAt=starts, resetType=reset_type, announcedAt=announced)
    title = "초기화권 지급 발표" if reset_type == "banked" else "일반 초기화 발표"
    return _event(identifier, reset_type, title, text, source,
                  startsAt=announced, resetType=reset_type, announcedAt=announced)


def _watch(value):
    value = _require_object(value)
    _require_keys(value, ("level", "reset_chance_percent", "forecast_window", "observed_at", "expires_at", "text", "source"))
    chance = value["reset_chance_percent"]
    if (value["level"] not in ("elevated", "strong") or
            (chance is not None and (not isinstance(chance, int) or isinstance(chance, bool) or not 0 <= chance <= 100)) or
            not isinstance(value["forecast_window"], str) or not isinstance(value["text"], str)):
        raise FeedError("공개 초기화 예측의 형식을 확인하지 못했습니다.")
    observed = _date(value["observed_at"])
    expires = _date(value["expires_at"])
    if expires < observed:
        raise FeedError("공개 초기화 예측의 유효 기간을 확인하지 못했습니다.")
    forecast = _text(value["forecast_window"], 500)
    # Interpret the complete phrase before display truncation, so a qualifier
    # beyond the display limit cannot accidentally become an exact deadline.
    start, end, derived = forecast_bounds(value["forecast_window"], observed)
    identifier = "watch-" + hashlib.sha256((value["observed_at"] + "\n" + forecast + "\n" + value["level"]).encode()).hexdigest()[:24]
    return _event(identifier, "prediction", "비공식 초기화 예측", _text(value["text"]), _source(value["source"]),
                  startsAt=start, endsAt=end, forecastStartsAt=start, forecastEndsAt=end,
                  observedAt=observed, expiresAt=expires, chancePercent=chance,
                  level=value["level"], forecastWindow=forecast, derivedWindow=derived)


def _meta(document):
    meta = _require_object(document.get("meta"))
    if meta.get("api_version") != "v1":
        raise FeedError("지원하는 공개 초기화 정보 버전이 아닙니다.")
    return _date(meta.get("generated_at"))


def _stats(value):
    value = _require_object(value)
    _require_keys(value, ("total", "last_reset_at", "days_since_last", "avg_interval_days"))
    total = value["total"]
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise FeedError("공개 초기화 통계의 형식을 확인하지 못했습니다.")
    result = {"total": total, "lastResetAt": _date(value["last_reset_at"], nullable=True)}
    for source, destination in (("days_since_last", "daysSinceLast"), ("avg_interval_days", "avgIntervalDays")):
        number = value[source]
        if number is not None and (not isinstance(number, (int, float)) or isinstance(number, bool) or not math.isfinite(number) or number < 0):
            raise FeedError("공개 초기화 통계의 형식을 확인하지 못했습니다.")
        result[destination] = number
    return result


def _parse_document(document, url):
    document = _require_object(document)
    generated = _meta(document)
    data = document.get("data")
    if url == STATUS_URL:
        data = _require_object(data)
        _require_keys(data, ("latest_reset", "scheduled_reset", "active_watch", "stats"))
        return {"latestReset": _reset(data["latest_reset"]) if data["latest_reset"] is not None else None,
                "scheduledReset": _reset(data["scheduled_reset"], scheduled=True) if data["scheduled_reset"] is not None else None,
                "activeWatch": _watch(data["active_watch"]) if data["active_watch"] is not None else None,
                "stats": _stats(data["stats"])}, generated
    if not isinstance(data, list) or len(data) > 100:
        raise FeedError("공개 초기화 이력의 형식을 확인하지 못했습니다.")
    pagination = _require_object(document.get("pagination"))
    _require_keys(pagination, ("has_more", "next_cursor"))
    if not isinstance(pagination.get("has_more"), bool) or not (pagination.get("next_cursor") is None or isinstance(pagination.get("next_cursor"), str)):
        raise FeedError("공개 초기화 이력의 형식을 확인하지 못했습니다.")
    return [_reset(value) for value in data], generated


def _retry_after(value, now):
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        if re.fullmatch(r"\d+", value.strip()):
            seconds = int(value.strip())
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = date.timestamp() - now
        return min(86400, max(0, seconds))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _etag(value):
    return value if isinstance(value, str) and 0 < len(value) <= 512 and all(32 <= ord(c) < 127 for c in value) else None


def _empty_state(now):
    return {"status": "pending", "refreshing": False, "fetchedAt": None,
            "generatedAt": None, "lastAttemptAt": None, "error": None,
            "events": [], "history": [], "activeWatch": None, "scheduledReset": None,
            "stats": None, "sourceUrl": SOURCE_URL, "nextRefreshAt": now,
            "stale": False, "demo": False}


def _decorate(event, now):
    if event["kind"] == "prediction":
        validity_expired = event["expiresAt"] is not None and event["expiresAt"] <= now
        window_passed = event["endsAt"] is not None and event["endsAt"] < now
        event["validityExpired"] = validity_expired
        event["windowPassed"] = window_passed
        event["expired"] = validity_expired or window_passed
        event["actionable"] = not event["expired"] and event["endsAt"] is not None and event["chancePercent"] != 0
    elif event["kind"] == "scheduled":
        event["passed"] = event["startsAt"] is not None and event["startsAt"] <= now
        event["actionable"] = event["startsAt"] is not None and not event["passed"]


def _snapshot(state, now):
    result = copy.deepcopy(state)
    result["now"] = now
    result["stale"] = result["fetchedAt"] is not None and result["status"] != "ok"
    for event in result["events"] + result["history"] + [e for e in (result["activeWatch"], result["scheduledReset"]) if e]:
        _decorate(event, now)
    return result


class ResetFeedProvider:
    def __init__(self, refresh_interval=300, *, auto_start=True, opener=None, clock=None):
        self.clock = clock or time.time
        self.interval = max(30, float(refresh_interval))
        if not math.isfinite(self.interval):
            raise ValueError("refresh_interval must be finite")
        # Do not inherit environment proxy credentials or any cookie handler.
        self.opener = opener or build_opener(ProxyHandler({}), _NoRedirect())
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.worker = None
        self.scheduler = None
        self._cache = {}
        self._failure_count = 0
        self._retry_at = 0
        self._state = _empty_state(self.clock())
        if auto_start:
            self.scheduler = threading.Thread(target=self._schedule, name="public-reset-feed", daemon=True)
            self.scheduler.start()

    def state(self):
        with self.lock:
            return _snapshot(self._state, self.clock())

    def _begin(self):
        with self.lock:
            if self.closed.is_set() or self._state["refreshing"] or self.clock() < self._retry_at:
                return False
            self._state["refreshing"] = True
            self._state["lastAttemptAt"] = self.clock()
            return True

    def refresh(self):
        """Start one background refresh without waiting for public networking."""
        if not self._begin():
            return False
        with self.lock:
            self.worker = threading.Thread(target=self._run_refresh, name="public-reset-fetch", daemon=True)
            self.worker.start()
        return True

    def refresh_once(self):
        """A bounded blocking refresh, using the same single-flight guard."""
        if self._begin():
            self._run_refresh()
        return self.state()

    def _fetch(self, url):
        if url not in (STATUS_URL, HISTORY_URL):
            raise FeedError("허용된 공개 초기화 조회 주소가 아닙니다.")
        cached = self._cache.get(url)
        headers = {"Accept": "application/json", "User-Agent": "Codex-Reset-Dashboard/1.0"}
        if cached and cached["etag"]:
            headers["If-None-Match"] = cached["etag"]
        request = Request(url, headers=headers, method="GET")
        try:
            open_request = self.opener.open if hasattr(self.opener, "open") else self.opener
            response = open_request(request, timeout=REQUEST_TIMEOUT)
        except HTTPError as error:
            if error.code == 304 and cached:
                error.close()
                return cached["value"], cached["generatedAt"]
            retry = _retry_after(error.headers.get("Retry-After"), self.clock()) if error.code == 429 else None
            error.close()
            if error.code == 429:
                raise FeedError("공개 초기화 정보의 조회 한도에 도달했습니다. 잠시 후 다시 확인합니다.", retry) from None
            raise FeedError("공개 초기화 정보를 가져오지 못했습니다.") from None
        except (URLError, OSError, TimeoutError):
            raise FeedError("공개 초기화 정보에 연결하지 못했습니다.") from None
        try:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status == 304 and cached:
                return cached["value"], cached["generatedAt"]
            if status == 429:
                raise FeedError("공개 초기화 정보의 조회 한도에 도달했습니다. 잠시 후 다시 확인합니다.",
                                _retry_after(response.headers.get("Retry-After"), self.clock()))
            if status != 200:
                raise FeedError("공개 초기화 정보를 가져오지 못했습니다.")
            size = response.headers.get("Content-Length")
            if size is not None:
                try:
                    if int(size) < 0 or int(size) > MAX_RESPONSE_BYTES:
                        raise FeedError("공개 초기화 정보의 응답이 너무 큽니다.")
                except ValueError:
                    raise FeedError("공개 초기화 정보의 응답 형식을 확인하지 못했습니다.") from None
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise FeedError("공개 초기화 정보의 응답이 너무 큽니다.")
            try:
                document = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (UnicodeError, ValueError, RecursionError):
                raise FeedError("공개 초기화 정보의 응답 형식을 확인하지 못했습니다.") from None
            value, generated = _parse_document(document, url)
            # Cache only after the complete document was validated successfully.
            self._cache[url] = {"value": value, "generatedAt": generated, "etag": _etag(response.headers.get("ETag"))}
            return value, generated
        except (URLError, OSError, TimeoutError):
            raise FeedError("공개 초기화 정보에 연결하지 못했습니다.") from None
        finally:
            response.close()

    def _run_refresh(self):
        try:
            status, generated = self._fetch(STATUS_URL)
            if self.closed.is_set():
                return
            history, _ = self._fetch(HISTORY_URL)
            history = list({(event["id"], event["kind"]): event for event in history}.values())
            if status["latestReset"] is not None and not any(event["id"] == status["latestReset"]["id"] for event in history):
                history.append(status["latestReset"])
            history.sort(key=lambda event: event["startsAt"], reverse=True)
            events = history + [event for event in (status["scheduledReset"], status["activeWatch"]) if event is not None]
            with self.lock:
                if self.closed.is_set():
                    return
                now = self.clock()
                self._state.update({"status": "ok", "fetchedAt": now, "generatedAt": generated,
                                    "error": None, "history": history, "events": events,
                                    "activeWatch": status["activeWatch"], "scheduledReset": status["scheduledReset"],
                                    "stats": status["stats"], "nextRefreshAt": now + self.interval})
                self._failure_count = 0
                self._retry_at = 0
        except Exception as error:
            # Never surface remote text, headers, request identifiers, or raw errors.
            message = str(error) if isinstance(error, FeedError) else "공개 초기화 정보를 확인하지 못했습니다."
            with self.lock:
                if not self.closed.is_set():
                    self._failure_count += 1
                    delay = min(3600, self.interval * (2 ** min(self._failure_count - 1, 10)))
                    if isinstance(error, FeedError) and error.retry_after is not None:
                        delay = max(delay, error.retry_after)
                    self._retry_at = self.clock() + delay
                    self._state.update({"status": "error", "error": message, "nextRefreshAt": self._retry_at})
        finally:
            with self.lock:
                self._state["refreshing"] = False

    def _schedule(self):
        while not self.closed.is_set():
            with self.lock:
                due = self._state["nextRefreshAt"]
                refreshing = self._state["refreshing"]
            if not refreshing and self.clock() >= due:
                self.refresh()
            self.closed.wait(0.2 if refreshing else min(60, max(0.2, due - self.clock())))

    def close(self):
        self.closed.set()
        for thread in (self.scheduler, self.worker):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)


class DemoResetFeed:
    """Labeled, network-free calendar fixtures; never account usage evidence."""
    def __init__(self, refresh_interval=300, *, auto_start=True, clock=None, **_):
        self.clock = clock or time.time
        self.interval = refresh_interval
        self.lock = threading.RLock()
        self.closed = False
        now = self.clock()
        local = datetime.fromtimestamp(now, timezone(timedelta(hours=9)))

        def iso(seconds):
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

        def history_time(days):
            return min(now - 3600, local.replace(day=max(1, local.day - days), hour=10, minute=0, second=0, microsecond=0).timestamp())

        regular = _reset({"id": "demo-regular", "reset_type": "regular", "announced_at": iso(history_time(6)),
                          "text": "데모: 일반 초기화 발표 이력입니다.", "source": {"type": "observed", "url": SOURCE_URL}})
        banked = _reset({"id": "demo-banked", "reset_type": "banked", "announced_at": iso(history_time(2)),
                         "text": "데모: 초기화권 지급 발표 이력입니다.", "source": {"type": "observed", "url": SOURCE_URL}})
        scheduled = _reset({"id": "demo-scheduled", "status": "scheduled", "reset_type": "regular", "announced_at": iso(now - 3600),
                            "scheduled_for": iso(now + 3 * 3600), "text": "데모: 3시간 뒤 초기화 예고입니다.",
                            "source": {"type": "observed", "url": SOURCE_URL}}, scheduled=True)
        watch = _watch({"level": "strong", "reset_chance_percent": 75, "forecast_window": "next 24–48 hours",
                        "observed_at": iso(now - 3600), "expires_at": iso(now + 48 * 3600),
                        "text": "데모: 비공식 예측 구간이며 실제 발표가 아닙니다.", "source": {"type": "observed", "url": SOURCE_URL}})
        for event in (regular, banked, scheduled, watch):
            event["title"] = "데모 · " + event["title"]
            event["demo"] = True
        self._state = _empty_state(now)
        self._state.update({"status": "ok", "fetchedAt": now, "generatedAt": now, "lastAttemptAt": now,
                            "events": [regular, banked, scheduled, watch], "history": [banked, regular],
                            "scheduledReset": scheduled, "activeWatch": watch,
                            "stats": {"total": 2, "lastResetAt": banked["startsAt"], "daysSinceLast": 2, "avgIntervalDays": 4},
                            "nextRefreshAt": now + self.interval, "demo": True})

    def state(self):
        with self.lock:
            return _snapshot(self._state, self.clock())

    def refresh(self):
        self.refresh_once()
        return not self.closed

    def refresh_once(self):
        with self.lock:
            if not self.closed:
                now = self.clock()
                self._state.update({"fetchedAt": now, "generatedAt": now, "lastAttemptAt": now, "nextRefreshAt": now + self.interval})
            return self.state()

    def close(self):
        self.closed = True
