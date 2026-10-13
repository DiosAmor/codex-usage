from http.client import HTTPConnection
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cli import countdown, render_reset_timing
from backend import DashboardManager
from server import create_server, DemoManager


class TimingTests(unittest.TestCase):
    def test_scheduled_countdown_and_passed_deadline(self):
        feed = {"fetchedAt": 100, "scheduledReset": {"startsAt": 3700, "resetType": "regular"}}
        self.assertIn("1시간 0초 남음", render_reset_timing(feed, now=100))
        passed = render_reset_timing(feed, now=3800)
        self.assertIn("예정 시각 지남 · 진행 확인 대기", passed)
        self.assertNotIn("초기화 완료", passed)

    def test_forecast_bounds_distinct_from_expiry_and_zero_chance(self):
        feed = {"fetchedAt": 100, "activeWatch": {
            "chancePercent": 0, "forecastWindow": "next 24–48 hours", "startsAt": 3700,
            "endsAt": 7300, "forecastStartsAt": 3700, "forecastEndsAt": 7300, "expiresAt": 99999}}
        result = render_reset_timing(feed, now=100)
        self.assertIn("가능성 0%", result)
        self.assertIn("예측 범위 시작까지 1시간 0초", result)
        self.assertIn("예측 범위 끝까지 2시간 0초", result)
        self.assertNotIn("27시간", result)

    def test_unparseable_prediction_does_not_use_expiry_as_reset_time(self):
        feed = {"fetchedAt": 100, "activeWatch": {
            "chancePercent": None, "forecastWindow": "probably soon", "startsAt": None,
            "endsAt": None, "expiresAt": 7300}}
        result = render_reset_timing(feed, now=100)
        self.assertIn("가능성 미제공", result)
        self.assertIn("남은 시간 계산 불가", result)
        self.assertNotIn("끝까지", result)

    def test_expired_prediction_and_unavailable_feed(self):
        expired = render_reset_timing({"fetchedAt": 100, "activeWatch": {
            "forecastWindow": "next24h", "startsAt": 400, "endsAt": 700,
            "expiresAt": 200, "chancePercent": 80}}, now=300)
        self.assertIn("유효기간 종료", expired)
        self.assertNotIn("끝까지", expired)
        unavailable = render_reset_timing({"fetchedAt": None, "error": "unavailable"}, now=100)
        self.assertIn("일정 확인 불가", unavailable)
        self.assertNotIn("제공된 예측 없음", unavailable)
        stale = render_reset_timing({"fetchedAt": 50, "error": "unavailable"}, now=100)
        self.assertIn("이전 조회 정보 표시", stale)

    def test_passed_forecast_window_is_distinct_from_classifier_expiry(self):
        feed = {"fetchedAt": 100, "activeWatch": {
            "expired": True, "windowPassed": True, "validityExpired": False,
            "forecastStartsAt": 100, "startsAt": 100,
            "forecastEndsAt": 200, "endsAt": 200, "expiresAt": 500,
            "forecastWindow": "next 24 hours", "chancePercent": 75}}
        result = render_reset_timing(feed, now=300)
        self.assertIn("예측 시간 범위 지남", result)
        self.assertNotIn("유효기간 종료", result)
        self.assertNotIn("끝까지", result)

    def test_countdown_days_and_unknown_time(self):
        self.assertEqual(countdown(90061, 0), "1일 1시간 1분 1초")
        self.assertEqual(countdown(None, 0), "시각 확인 불가")


class MutableFeed:
    def __init__(self):
        self.id = "initial"
        self.refreshes = 0

    def state(self):
        return {"fetchedAt": 100, "history": [{"id": self.id, "announcedAt": 99}],
                "events": [], "scheduledReset": None, "activeWatch": None}

    def refresh(self):
        self.refreshes += 1


class CountingManager(DemoManager):
    def __init__(self):
        super().__init__()
        self.refreshes = 0

    def refresh(self, *, after_current=False):
        self.refreshes += 1
        return self.state()


class PausedManager(DashboardManager):
    """Exercise real refresh coordination without credentials or Codex calls."""
    def __init__(self):
        self.lock = threading.RLock()
        self.accounts = {"test-account": {"id": "test-account", "usage": None}}
        self.logins = {}
        self.sessions = set()
        self.interval = 300
        self.refreshing = False
        self._refresh_pending = False
        self.closed = threading.Event()
        self.worker = None
        self.scheduler = None
        self.next_refresh = 0
        self.polls = 0
        self.remote_usage = 70
        self.first_started = threading.Event()
        self.release_first = threading.Event()

    def _discover(self):
        pass

    def _poll_one(self, account_id):
        with self.lock:
            self.polls += 1
            count, sampled_usage = self.polls, self.remote_usage
        if count == 1:
            self.first_started.set()
            if not self.release_first.wait(3):
                raise RuntimeError("Paused account fetch was not released")
        with self.lock:
            self.accounts[account_id]["usage"] = sampled_usage


class RefreshCoordinationTests(unittest.TestCase):
    def test_default_refresh_still_drops_duplicate_calls(self):
        manager = PausedManager()
        try:
            manager.refresh()
            self.assertTrue(manager.first_started.wait(1))
            for _ in range(5):
                manager.refresh()
            manager.release_first.set()
            manager.worker.join(timeout=2)
            self.assertEqual(manager.polls, 1)
            self.assertFalse(manager.state()["refreshing"])
        finally:
            manager.release_first.set()
            manager.close()

    def test_close_discards_a_queued_refresh(self):
        manager = PausedManager()
        closer = None
        try:
            manager.refresh()
            self.assertTrue(manager.first_started.wait(1))
            manager.refresh(after_current=True)
            closer = threading.Thread(target=manager.close)
            closer.start()
            self.assertTrue(manager.closed.wait(1))
            manager.release_first.set()
            closer.join(timeout=2)
            self.assertFalse(closer.is_alive())
            self.assertEqual(manager.polls, 1)
            self.assertFalse(manager.state()["refreshing"])
            manager.refresh(after_current=True)
            self.assertEqual(manager.polls, 1)
        finally:
            manager.release_first.set()
            manager.close()
            if closer:
                closer.join(timeout=2)


class CalendarHttpTests(unittest.TestCase):
    def test_new_announcement_during_account_fetch_queues_one_fresh_pass(self):
        manager, feed = PausedManager(), MutableFeed()
        server = create_server(manager, 0, reset_feed=feed)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def get_feed():
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            connection.request("GET", "/api/reset-calendar")
            response = connection.getresponse()
            status = response.status
            response.read()
            connection.close()
            self.assertEqual(status, 200)

        try:
            get_feed()
            manager.refresh()
            self.assertTrue(manager.first_started.wait(1))
            # The in-flight account read has already sampled the old usage.
            manager.remote_usage = 0
            feed.id = "executed-reset-announcement"
            get_feed()
            # More announcement-triggered requests coalesce into the same pass.
            for index in range(5):
                feed.id = f"announcement-{index}"
                get_feed()
            self.assertEqual(manager.polls, 1)
            self.assertTrue(manager.state()["refreshing"])
            manager.release_first.set()
            manager.worker.join(timeout=2)
            self.assertFalse(manager.worker.is_alive())
            self.assertEqual(manager.polls, 2)
            self.assertEqual(manager.state()["accounts"][0]["usage"], 0)
            self.assertFalse(manager.state()["refreshing"])
        finally:
            manager.release_first.set()
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
            manager.close()

    def test_feed_endpoint_and_announcement_change_requeries_accounts(self):
        manager, feed = CountingManager(), MutableFeed()
        server = create_server(manager, 0, reset_feed=feed)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def get(path):
            connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            connection.request("GET", path)
            response = connection.getresponse()
            status, data = response.status, response.read()
            connection.close()
            return status, data
        try:
            self.assertEqual(get("/api/reset-calendar")[0], 200)
            self.assertEqual(manager.refreshes, 0)
            feed.id = "new-announcement"
            self.assertEqual(get("/api/reset-calendar")[0], 200)
            self.assertEqual(manager.refreshes, 1)
            get("/api/reset-calendar")
            self.assertEqual(manager.refreshes, 1)
            self.assertEqual(get("/calendar.js")[0], 200)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == "__main__":
    unittest.main()
