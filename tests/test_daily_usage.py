import base64
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import DashboardManager
from daily_usage import DAILY_USAGE_ERROR, MAX_TOKEN_COUNT, normalize_daily_usage, token_count
from protocol import CodexSession, ProtocolError


class DailyNormalizationTests(unittest.TestCase):
    def test_zero_and_int64_maximum_are_exact_decimal_strings(self):
        result = normalize_daily_usage({"dailyUsageBuckets": [
            {"startDate": "2026-10-02", "tokens": MAX_TOKEN_COUNT},
            {"startDate": "2026-10-01", "tokens": 0}]})
        self.assertEqual(result, {"status": "ok", "daily": [
            {"date": "2026-10-01", "tokens": "0"},
            {"date": "2026-10-02", "tokens": "9223372036854775807"}]})
        self.assertIn('"9223372036854775807"', json.dumps(result))

    def test_missing_null_and_empty_have_different_availability(self):
        for raw in ({}, {"dailyUsageBuckets": None}):
            self.assertEqual(normalize_daily_usage(raw), {"status": "unavailable", "daily": []})
        self.assertEqual(normalize_daily_usage({"dailyUsageBuckets": []}), {"status": "ok", "daily": []})

    def test_non_integer_or_out_of_range_tokens_stay_unknown(self):
        for value in (None, True, False, -1, 1.0, float("nan"), "123", MAX_TOKEN_COUNT + 1, {}, []):
            with self.subTest(value=value):
                self.assertIsNone(token_count(value))
                result = normalize_daily_usage({"dailyUsageBuckets": [
                    {"startDate": "2026-10-13", "tokens": value}]})
                self.assertIsNone(result["daily"][0]["tokens"])

    def test_dates_are_calendar_valid_and_not_converted_between_timezones(self):
        for day in ("2026-2-03", "2026-02-29", "2026-13-01", "0000-01-01",
                    "2026-10-13T00:00:00Z", " 2026-10-13", "２０２６-１０-１３", None):
            with self.subTest(day=day), self.assertRaises(ValueError):
                normalize_daily_usage({"dailyUsageBuckets": [{"startDate": day, "tokens": 1}]})
        result = normalize_daily_usage({"dailyUsageBuckets": [{"startDate": "2024-02-29", "tokens": 1}]})
        self.assertEqual(result["daily"][0]["date"], "2024-02-29")

    def test_invalid_response_structure_is_rejected(self):
        for raw in (None, [], "response", {"dailyUsageBuckets": {}},
                    {"dailyUsageBuckets": [None]}, {"dailyUsageBuckets": [{"tokens": 1}]}):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_daily_usage(raw)

    def test_duplicates_do_not_double_count_and_conflicts_stay_unknown(self):
        buckets = [{"startDate": "2026-10-01", "tokens": 3},
                   {"startDate": "2026-10-01", "tokens": 3},
                   {"startDate": "2026-10-02", "tokens": 4},
                   {"startDate": "2026-10-02", "tokens": 7},
                   {"startDate": "2026-10-02", "tokens": 4},
                   {"startDate": "2026-10-03", "tokens": 0},
                   {"startDate": "2026-10-03", "tokens": None}]
        result = normalize_daily_usage({"dailyUsageBuckets": buckets})
        self.assertEqual(result["daily"], [
            {"date": "2026-10-01", "tokens": "3"},
            {"date": "2026-10-02", "tokens": None},
            {"date": "2026-10-03", "tokens": None}])
        reversed_result = normalize_daily_usage({"dailyUsageBuckets": list(reversed(buckets))})
        self.assertEqual(result, reversed_result)

    def test_summary_and_thread_content_are_not_returned(self):
        result = normalize_daily_usage({"dailyUsageBuckets": [{"startDate": "2026-10-13", "tokens": 1}],
                                       "summary": {"lifetimeTokens": 99},
                                       "threadUsage": {"threadId": "PRIVATE_THREAD", "groups": ["PRIVATE_MODEL"]}})
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("summary", result)


def fake_token(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"local.{payload}.test"


class HistorySession:
    quota_error = False
    daily_error = None
    daily_response = {"dailyUsageBuckets": [{"startDate": "2026-10-12", "tokens": 9007199254740993}]}
    mismatch = False
    calls = []
    instances = []

    def __init__(self, home, binary, refresh_tokens=None):
        self.closed = False
        self.home = Path(home)
        self.instances.append(self)

    def request(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        if method == "account/login/start":
            return {"type": "chatgptAuthTokens"}
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": "person@example.invalid", "planType": "plus"}}
        if method == "account/rateLimits/read":
            if self.quota_error:
                raise ProtocolError("quota unavailable")
            return {"accountId": "other-workspace" if self.mismatch else "workspace-one",
                    "rateLimits": {"primary": {"usedPercent": 21}, "secondary": {"usedPercent": 44}}}
        if method == "account/usage/read":
            if self.daily_error:
                raise self.daily_error
            return self.daily_response
        raise AssertionError(method)

    def close(self):
        self.closed = True


class DailyManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        identity = {"sub": "subject-one", "email": "person@example.invalid",
                    "https://api.openai.com/auth": {"chatgpt_account_id": "workspace-one"}}
        self.auth_path = self.source / "auth.json"
        self.auth_path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {
            "account_id": "workspace-one", "id_token": fake_token(identity),
            "access_token": fake_token({"sub": "subject-one", "exp": time.time() + 3600}),
            "refresh_token": "PRIVATE_REFRESH"}}))
        self.auth_before = self.auth_path.read_bytes()
        HistorySession.quota_error = False
        HistorySession.daily_error = None
        HistorySession.daily_response = {"dailyUsageBuckets": [
            {"startDate": "2026-10-12", "tokens": 9007199254740993}]}
        HistorySession.mismatch = False
        HistorySession.calls = []
        HistorySession.instances = []
        self.manager = DashboardManager(self.base / "private", source_home=self.source,
                                        auto_start=False, session_factory=HistorySession)
        self.account_id = self.manager.state()["accounts"][0]["id"]

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def poll(self):
        self.manager._poll_one(self.account_id)
        return self.manager.state()["accounts"][0]

    def test_success_in_verified_session_and_public_state_copies_history(self):
        initial = self.manager.state()["accounts"][0]["tokenUsage"]
        self.assertEqual(initial["status"], "pending")
        self.assertIsNone(initial["fetchedAt"])
        account = self.poll()
        self.assertEqual(account["status"], "ok")
        self.assertEqual(account["tokenUsage"]["status"], "ok")
        self.assertEqual(account["tokenUsage"]["dateBasis"], "server")
        self.assertEqual(account["tokenUsage"]["daily"], [{"date": "2026-10-12", "tokens": "9007199254740993"}])
        self.assertGreaterEqual(account["tokenUsage"]["fetchedAt"], account["tokenUsage"]["lastAttemptAt"])
        self.assertEqual([call[0] for call in HistorySession.calls], [
            "account/login/start", "account/read", "account/rateLimits/read", "account/usage/read"])
        self.assertEqual(HistorySession.calls[-1][1], {})
        self.assertTrue(all(instance.closed for instance in HistorySession.instances))
        self.assertEqual(self.auth_path.read_bytes(), self.auth_before)
        self.assertFalse(list((self.base / "private").rglob("auth.json")))
        self.assertNotIn("PRIVATE_REFRESH", json.dumps(self.manager.state()))
        account["tokenUsage"]["daily"].clear()
        self.assertEqual(len(self.manager.state()["accounts"][0]["tokenUsage"]["daily"]), 1)

    def test_daily_failure_keeps_quota_ok_and_retains_last_success(self):
        before = self.poll()
        HistorySession.daily_error = ProtocolError("https://private.invalid/?secret=PRIVATE_SECRET", code=-32601)
        after = self.poll()
        self.assertEqual(after["status"], "ok")
        self.assertIsNone(after["error"])
        self.assertEqual(after["buckets"], before["buckets"])
        self.assertEqual(after["tokenUsage"]["status"], "error")
        self.assertEqual(after["tokenUsage"]["daily"], before["tokenUsage"]["daily"])
        self.assertEqual(after["tokenUsage"]["fetchedAt"], before["tokenUsage"]["fetchedAt"])
        self.assertGreaterEqual(after["tokenUsage"]["lastAttemptAt"], before["tokenUsage"]["lastAttemptAt"])
        self.assertEqual(after["tokenUsage"]["error"], DAILY_USAGE_ERROR)
        self.assertNotIn("PRIVATE_SECRET", json.dumps(after))
        self.assertTrue(all(instance.closed for instance in HistorySession.instances))

    def test_old_cli_or_fake_session_unsupported_daily_is_optional(self):
        HistorySession.daily_error = AssertionError("unsupported daily method PRIVATE")
        account = self.poll()
        self.assertEqual(account["status"], "ok")
        self.assertEqual(account["tokenUsage"]["status"], "error")
        self.assertIsNone(account["tokenUsage"]["fetchedAt"])
        self.assertNotIn("PRIVATE", json.dumps(account))

    def test_invalid_history_retains_prior_rows_without_damaging_quota(self):
        before = self.poll()
        HistorySession.daily_response = {"dailyUsageBuckets": [{"startDate": "bad date", "tokens": 3}]}
        after = self.poll()
        self.assertEqual(after["status"], "ok")
        self.assertEqual(after["tokenUsage"]["status"], "error")
        self.assertEqual(after["tokenUsage"]["daily"], before["tokenUsage"]["daily"])

    def test_quota_failure_skips_daily_query_and_marks_retained_history_stale(self):
        before = self.poll()
        HistorySession.calls.clear()
        HistorySession.quota_error = True
        after = self.poll()
        self.assertEqual(after["status"], "error")
        self.assertEqual(after["tokenUsage"]["status"], "error")
        self.assertEqual(after["tokenUsage"]["daily"], before["tokenUsage"]["daily"])
        self.assertEqual(after["tokenUsage"]["fetchedAt"], before["tokenUsage"]["fetchedAt"])
        self.assertNotIn("account/usage/read", [call[0] for call in HistorySession.calls])
        self.assertTrue(all(instance.closed for instance in HistorySession.instances))

    def test_mismatched_quota_account_prevents_daily_query(self):
        HistorySession.mismatch = True
        account = self.poll()
        self.assertEqual(account["status"], "error")
        self.assertNotIn("account/usage/read", [call[0] for call in HistorySession.calls])
        self.assertIsNone(account["tokenUsage"]["fetchedAt"])

    def test_unavailable_and_empty_history_differ_and_recover_from_errors(self):
        HistorySession.daily_response = {"dailyUsageBuckets": None}
        unavailable = self.poll()
        self.assertEqual(unavailable["tokenUsage"]["status"], "unavailable")
        self.assertIsNotNone(unavailable["tokenUsage"]["fetchedAt"])
        self.assertIsNone(unavailable["tokenUsage"]["error"])
        HistorySession.daily_response = {"dailyUsageBuckets": []}
        empty = self.poll()
        self.assertEqual(empty["tokenUsage"]["status"], "ok")
        self.assertEqual(empty["tokenUsage"]["daily"], [])
        HistorySession.daily_error = ValueError("server error")
        self.poll()
        HistorySession.daily_error = None
        recovered = self.poll()
        self.assertEqual(recovered["tokenUsage"]["status"], "ok")
        self.assertIsNone(recovered["tokenUsage"]["error"])


class AccountUsageProtocolTests(unittest.TestCase):
    def test_thread_and_extra_parameters_rejected_before_any_send(self):
        session = object.__new__(CodexSession)
        for params in ({"threadId": "thread-123"}, {"threadId": 0}, {"threadId": False},
                       {"threadId": []}, {"model": "private-model"}, [], "thread-123"):
            with self.subTest(params=params), self.assertRaises(ProtocolError):
                session.request("account/usage/read", params)

    def test_allowed_account_query_sends_empty_parameters_and_closes_process(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fake-codex"
            binary.write_text("""#!/usr/bin/env python3
import json,sys
for line in sys.stdin:
    message=json.loads(line)
    if 'id' in message:
        print(json.dumps({'id':message['id'],'result':{'params':message.get('params')}}),flush=True)
""")
            binary.chmod(0o700)
            with CodexSession(directory, str(binary)) as session:
                for params in (None, {}, {"threadId": None}, {"threadId": ""}):
                    self.assertEqual(session.request("account/usage/read", params), {"params": {}})
            self.assertIsNotNone(session.proc.poll())


if __name__ == "__main__":
    unittest.main()
