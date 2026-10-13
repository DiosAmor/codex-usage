from datetime import datetime, timezone
from email.message import Message
from email.utils import format_datetime
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reset_feed import (DemoResetFeed, HISTORY_URL, MAX_RESPONSE_BYTES,
                        REQUEST_TIMEOUT, ResetFeedProvider, SOURCE_URL,
                        STATUS_URL, forecast_bounds)


NOW = datetime(2026, 10, 13, 6, tzinfo=timezone.utc).timestamp()


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def reset(identifier="regular-one", kind="regular", when=NOW - 86400):
    return {"id": identifier, "reset_type": kind, "announced_at": stamp(when),
            "text": "Reset announcement", "source": {"type": "x_post", "author": "thsottiaux",
                                                        "url": "https://x.com/thsottiaux/status/123"}}


def status_document(*, forecast="next 24–48 hours", chance=75, expiry=NOW + 86400,
                    watch=True, scheduled=True):
    announcement = reset("scheduled-one", when=NOW - 3600)
    announcement.update({"status": "scheduled", "scheduled_for": stamp(NOW + 3600)})
    prediction = {"level": "strong", "reset_chance_percent": chance, "forecast_window": forecast,
                  "observed_at": stamp(NOW - 3600), "expires_at": stamp(expiry),
                  "text": "Classifier forecast", "source": {"type": "observed"}}
    return {"data": {"latest_reset": reset(), "scheduled_reset": announcement if scheduled else None,
                     "active_watch": prediction if watch else None,
                     "stats": {"total": 2, "last_reset_at": stamp(NOW - 86400),
                               "days_since_last": 1, "avg_interval_days": None}},
            "meta": {"api_version": "v1", "generated_at": stamp(NOW - 60)}}


def history_document():
    return {"data": [reset(), reset("banked-one", "banked", NOW - 4 * 86400)],
            "pagination": {"has_more": False, "next_cursor": None},
            "meta": {"api_version": "v1", "generated_at": stamp(NOW - 120)}}


class Response:
    def __init__(self, document=None, *, raw=None, status=200, headers=None):
        self.status = status
        self.headers = {"Content-Type": "application/json", **(headers or {})}
        self.body = io.BytesIO(raw if raw is not None else json.dumps(document).encode())
        self.read_limits = []
        self.closed = False

    def read(self, size):
        self.read_limits.append(size)
        return self.body.read(size)

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def http_error(code, headers=None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return HTTPError(STATUS_URL, code, "PRIVATE_REMOTE_DETAIL", message, io.BytesIO())


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.providers = []

    def tearDown(self):
        for provider in self.providers:
            provider.close()

    def provider(self, *responses):
        opener = Opener(*responses)
        provider = ResetFeedProvider(300, auto_start=False, opener=opener, clock=self.clock)
        self.providers.append(provider)
        return provider, opener

    def test_no_network_at_construction_and_private_data_is_not_sent(self):
        provider, opener = self.provider(Response(status_document()), Response(history_document()))
        self.assertEqual(provider.state()["status"], "pending")
        self.assertFalse(opener.requests)
        with mock.patch("builtins.open", side_effect=AssertionError("No local account reads allowed")):
            result = provider.refresh_once()
        self.assertEqual(result["status"], "ok")
        self.assertEqual([request.full_url for request, _ in opener.requests], [STATUS_URL, HISTORY_URL])
        for request, timeout in opener.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(timeout, REQUEST_TIMEOUT)
            self.assertEqual(set(key.lower() for key in request.headers), {"accept", "user-agent"})
            self.assertFalse(request.has_header("Authorization"))
            self.assertFalse(request.has_header("Cookie"))
        self.assertEqual(result["sourceUrl"], SOURCE_URL)
        self.assertEqual([event["kind"] for event in result["history"]], ["regular", "banked"])
        self.assertEqual(len(result["events"]), 4)
        self.assertEqual(result["history"][0]["startsAt"], result["history"][0]["announcedAt"])

    def test_null_and_zero_probabilities_stay_distinct(self):
        for chance in (None, 0):
            with self.subTest(chance=chance):
                provider, _ = self.provider(Response(status_document(chance=chance)), Response(history_document()))
                result = provider.refresh_once()
                self.assertEqual(result["activeWatch"]["chancePercent"], chance)
                self.assertIsNone(result["scheduledReset"]["chancePercent"])
                if chance == 0:
                    self.assertFalse(result["activeWatch"]["actionable"])

    def test_unsupported_forecast_does_not_turn_validity_into_forecast_dates(self):
        provider, _ = self.provider(Response(status_document(forecast="Reset vibes are rising; watch this space")), Response(history_document()))
        watch = provider.refresh_once()["activeWatch"]
        self.assertEqual(watch["observedAt"], NOW - 3600)
        self.assertEqual(watch["expiresAt"], NOW + 86400)
        for key in ("startsAt", "endsAt", "forecastStartsAt", "forecastEndsAt"):
            self.assertIsNone(watch[key])
        self.assertFalse(watch["derivedWindow"])
        self.assertFalse(watch["actionable"])
        self.assertIn("watch this space", watch["forecastWindow"])

    def test_display_truncation_does_not_remove_forecast_qualifiers(self):
        phrase = "next 24 hours" + " " * 600 + "if a rollout succeeds"
        provider, _ = self.provider(Response(status_document(forecast=phrase)), Response(history_document()))
        watch = provider.refresh_once()["activeWatch"]
        self.assertIsNone(watch["forecastStartsAt"])
        self.assertIsNone(watch["forecastEndsAt"])

    def test_history_requires_complete_pagination_shape(self):
        history = history_document()
        del history["pagination"]["next_cursor"]
        provider, _ = self.provider(Response(status_document()), Response(history))
        self.assertEqual(provider.refresh_once()["status"], "error")

    def test_relative_window_and_expiry_are_separate(self):
        provider, _ = self.provider(Response(status_document(expiry=NOW + 3600)), Response(history_document()))
        first = provider.refresh_once()
        watch = first["activeWatch"]
        self.assertEqual(watch["forecastStartsAt"], NOW + 23 * 3600)
        self.assertEqual(watch["forecastEndsAt"], NOW + 47 * 3600)
        self.assertTrue(watch["derivedWindow"])
        self.clock.now += 3601
        expired = provider.state()["activeWatch"]
        self.assertTrue(expired["expired"])
        self.assertTrue(expired["validityExpired"])
        self.assertFalse(expired["actionable"])
        self.assertEqual(expired["forecastEndsAt"], watch["forecastEndsAt"])

    def test_passed_schedule_does_not_become_completed_history(self):
        provider, _ = self.provider(Response(status_document()), Response(history_document()))
        before = provider.refresh_once()
        self.clock.now += 3601
        after = provider.state()
        self.assertEqual(after["scheduledReset"]["kind"], "scheduled")
        self.assertTrue(after["scheduledReset"]["passed"])
        self.assertFalse(after["scheduledReset"]["actionable"])
        self.assertEqual(after["history"], before["history"])
        self.assertNotIn("completed", json.dumps(after))

    def test_nullable_scheduled_date_is_not_the_announcement_date(self):
        document = status_document()
        document["data"]["scheduled_reset"]["scheduled_for"] = None
        provider, _ = self.provider(Response(document), Response(history_document()))
        event = provider.refresh_once()["scheduledReset"]
        self.assertIsNone(event["startsAt"])
        self.assertEqual(event["announcedAt"], NOW - 3600)
        self.assertFalse(event["actionable"])

    def test_successful_etags_support_304_without_discarding_data(self):
        provider, opener = self.provider(Response(status_document(), headers={"ETag": '"status-v1"'}),
                                         Response(history_document(), headers={"ETag": '"history-v1"'}),
                                         http_error(304), Response(status=304))
        before = provider.refresh_once()
        self.clock.now += 300
        after = provider.refresh_once()
        self.assertEqual(after["status"], "ok")
        self.assertEqual(after["fetchedAt"], self.clock.now)
        self.assertEqual(after["generatedAt"], before["generatedAt"])
        self.assertEqual(after["history"], before["history"])
        self.assertEqual(opener.requests[2][0].get_header("If-none-match"), '"status-v1"')
        self.assertEqual(opener.requests[3][0].get_header("If-none-match"), '"history-v1"')

    def test_304_before_a_good_document_is_an_error(self):
        provider, opener = self.provider(http_error(304))
        state = provider.refresh_once()
        self.assertEqual(state["status"], "error")
        self.assertIsNone(state["fetchedAt"])
        self.assertEqual(state["history"], [])
        self.assertFalse(opener.requests[0][0].has_header("If-none-match"))

    def test_failure_retains_stale_data_and_backs_off(self):
        provider, opener = self.provider(Response(status_document()), Response(history_document()),
                                         URLError("PRIVATE_ACCOUNT_SECRET"), URLError("PRIVATE_ACCOUNT_SECRET"))
        before = provider.refresh_once()
        self.clock.now += 300
        failed = provider.refresh_once()
        self.assertEqual(failed["status"], "error")
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["fetchedAt"], before["fetchedAt"])
        self.assertEqual(failed["generatedAt"], before["generatedAt"])
        self.assertEqual(failed["history"], before["history"])
        self.assertNotIn("PRIVATE", json.dumps(failed))
        provider.refresh_once()
        self.assertEqual(len(opener.requests), 3)
        self.clock.now = failed["nextRefreshAt"]
        failed_again = provider.refresh_once()
        self.assertEqual(failed_again["nextRefreshAt"], self.clock.now + 600)

    def test_429_honors_retry_after_without_early_manual_retry(self):
        provider, opener = self.provider(http_error(429, {"Retry-After": "1200"}),
                                         Response(status_document()), Response(history_document()))
        failed = provider.refresh_once()
        self.assertEqual(failed["nextRefreshAt"], NOW + 1200)
        self.assertFalse(provider.refresh())
        self.clock.now += 1199
        provider.refresh_once()
        self.assertEqual(len(opener.requests), 1)
        self.clock.now += 1
        self.assertEqual(provider.refresh_once()["status"], "ok")
        self.assertEqual(len(opener.requests), 3)

    def test_429_http_date_is_supported(self):
        retry_date = format_datetime(datetime.fromtimestamp(NOW + 900, timezone.utc), usegmt=True)
        provider, _ = self.provider(Response(status=429, headers={"Retry-After": retry_date}))
        self.assertEqual(provider.refresh_once()["nextRefreshAt"], NOW + 900)

    def test_invalid_response_does_not_cache_etag_or_replace_last_good(self):
        bad = status_document()
        bad["data"]["active_watch"]["reset_chance_percent"] = True
        provider, opener = self.provider(Response(bad, headers={"ETag": '"invalid"'}),
                                         Response(status_document()), Response(history_document()))
        failure = provider.refresh_once()
        self.assertEqual(failure["status"], "error")
        self.clock.now = failure["nextRefreshAt"]
        self.assertEqual(provider.refresh_once()["status"], "ok")
        self.assertFalse(opener.requests[1][0].has_header("If-none-match"))

    def test_partial_refresh_does_not_publish_mixed_new_history(self):
        newer = status_document()
        newer["data"]["scheduled_reset"]["scheduled_for"] = stamp(NOW + 7 * 3600)
        provider, _ = self.provider(Response(status_document()), Response(history_document()),
                                    Response(newer), Response(raw=b"<html>PRIVATE_SERVER_ERROR</html>"))
        before = provider.refresh_once()
        after = provider.refresh_once()
        self.assertEqual(after["status"], "error")
        self.assertEqual(after["scheduledReset"], before["scheduledReset"])
        self.assertEqual(after["events"], before["events"])

    def test_bounded_size_and_rejection_of_non_json_numbers(self):
        for response in (Response(raw=b"x" * (MAX_RESPONSE_BYTES + 1)),
                         Response(raw=b"{}", headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}),
                         Response(raw=b'{"data":NaN}')):
            with self.subTest(response=response):
                provider, opener = self.provider(response)
                state = provider.refresh_once()
                self.assertEqual(state["status"], "error")
                self.assertEqual(len(opener.requests), 1)
                self.assertTrue(response.closed)
                self.assertTrue(all(size == MAX_RESPONSE_BYTES + 1 for size in response.read_limits))

    def test_remote_control_characters_and_unsafe_source_url_are_removed(self):
        document = status_document()
        document["data"]["latest_reset"]["text"] = "Hello\x1b[31m\x00\x7f"
        document["data"]["latest_reset"]["source"]["url"] = "javascript:alert(1)"
        history = history_document()
        history["data"] = []
        provider, _ = self.provider(Response(document), Response(history))
        event = provider.refresh_once()["history"][0]
        self.assertNotIn("\x1b", event["text"])
        self.assertNotIn("\x00", event["text"])
        self.assertIsNone(event["sourceUrl"])

    def test_state_is_a_detached_snapshot(self):
        provider, _ = self.provider(Response(status_document()), Response(history_document()))
        first = provider.refresh_once()
        first["history"][0]["title"] = "Modified"
        first["activeWatch"]["chancePercent"] = 100
        self.assertNotEqual(provider.state()["history"][0]["title"], "Modified")
        self.assertEqual(provider.state()["activeWatch"]["chancePercent"], 75)

    def test_async_refresh_is_nonblocking_and_single_flight(self):
        entered = threading.Event()
        release = threading.Event()

        def opener(request, timeout):
            if request.full_url == STATUS_URL:
                entered.set()
                if not release.wait(2):
                    raise TimeoutError()
                return Response(status_document())
            return Response(history_document())

        provider = ResetFeedProvider(300, auto_start=False, opener=opener, clock=self.clock)
        self.providers.append(provider)
        try:
            self.assertTrue(provider.refresh())
            self.assertTrue(entered.wait(1))
            self.assertTrue(provider.state()["refreshing"])
            self.assertFalse(provider.refresh())
            self.assertEqual(provider.refresh_once()["status"], "pending")
        finally:
            release.set()
            provider.worker.join(timeout=2)
        self.assertEqual(provider.state()["status"], "ok")
        provider.close()
        self.assertFalse(provider.refresh())


class ForecastTests(unittest.TestCase):
    def test_relative_phrases_are_anchored_to_observed_time(self):
        for phrase in ("within the next 24 hours", "next 24h", "within 24 hours", "24시간 이내"):
            with self.subTest(phrase=phrase):
                self.assertEqual(forecast_bounds(phrase, NOW), (NOW, NOW + 86400, True))
        self.assertEqual(forecast_bounds("next 24–48 hours", NOW), (NOW + 86400, NOW + 2 * 86400, True))

    def test_absolute_iso_window_keeps_korean_offset(self):
        start, end, derived = forecast_bounds("2026-10-13T15:00:00+09:00 / 2026-10-14T15:00:00+09:00", NOW)
        self.assertEqual((start, end), (NOW, NOW + 86400))
        self.assertFalse(derived)
        self.assertEqual(forecast_bounds("from 2026-10-13T06:00:00Z to 2026-10-13T07:00:00Z", None), (NOW, NOW + 3600, False))

    def test_free_text_naive_dates_and_unbounded_durations_are_not_guessed(self):
        for phrase in ("maybe within the next 24 hours, maybe not", "next 24 hours if a rollout succeeds",
                       "2026-10-13", "2026-10-13T15:00:00", "next 0 hours", "next 9000 hours",
                       "next 48-24 hours", "forecast expires tomorrow"):
            with self.subTest(phrase=phrase):
                self.assertEqual(forecast_bounds(phrase, NOW), (None, None, False))


class DemoTests(unittest.TestCase):
    def test_network_free_labeled_calendar_and_prediction_fixture(self):
        clock = Clock()
        with mock.patch("reset_feed.build_opener", side_effect=AssertionError("No network in demo")):
            provider = DemoResetFeed(300, clock=clock)
            state = provider.state()
        self.assertTrue(state["demo"])
        self.assertEqual({event["kind"] for event in state["events"]}, {"regular", "banked", "scheduled", "prediction"})
        self.assertTrue(all("데모" in event["title"] and event["demo"] for event in state["events"]))
        dates = {datetime.fromtimestamp(event["startsAt"], timezone.utc).date() for event in state["history"]}
        self.assertEqual(len(dates), 2)
        self.assertTrue(all(date.month == 10 for date in dates))
        self.assertGreater(state["scheduledReset"]["startsAt"], NOW)
        self.assertGreater(state["activeWatch"]["forecastStartsAt"], NOW)
        self.assertGreater(state["activeWatch"]["forecastEndsAt"], state["activeWatch"]["forecastStartsAt"])
        clock.now += 300
        self.assertEqual(provider.refresh_once()["fetchedAt"], clock.now)
        provider.close()
        self.assertFalse(provider.refresh())


if __name__ == "__main__":
    unittest.main()
