import base64
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import DashboardManager, normalize_usage
from cli import render
from protocol import CodexSession, ProtocolError
from server import create_server, DemoManager


def token(payload):
    part = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"test.{part}.signature"


def auth(workspace="account-one", subject="subject-one", expiry=None):
    return {"auth_mode": "chatgpt", "tokens": {
        "account_id": workspace, "refresh_token": "PRIVATE_REFRESH_SECRET",
        "access_token": token({"sub": subject, "exp": expiry if expiry is not None else time.time() + 3600}),
        "id_token": token({"sub": subject, "email": "person@example.invalid",
                           "https://api.openai.com/auth": {"chatgpt_account_id": workspace, "chatgpt_plan_type": "plus"}})}}


USAGE = {"rateLimits": {"limitId": "legacy", "primary": {"usedPercent": 99}},
         "rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 18, "windowDurationMins": 300, "resetsAt": 2000000000},
                                           "secondary": {"usedPercent": 25, "windowDurationMins": 10080}},
                                 "other": {"primary": None, "secondary": None}},
         "rateLimitResetCredits": {"availableCount": 3, "credits": [{"expiresAt": 2000000000}]}}


class FakeSession:
    fail = False
    called = []

    def __init__(self, home, binary, refresh_tokens=None):
        self.home = Path(home)
        self.identity = "account-one"

    def request(self, method, params=None, timeout=30):
        self.called.append(method)
        if method == "account/login/start":
            if params["type"] == "chatgptDeviceCode":
                return {"loginId": "server-login", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "123-TEST"}
            self.identity = params["chatgptAccountId"]
            return {"type": "chatgptAuthTokens"}
        if method == "account/read":
            return {"account": {"type": "chatgpt", "email": "person@example.invalid", "planType": "plus"}}
        if method == "account/rateLimits/read":
            if self.fail:
                raise ProtocolError("테스트 조회 실패")
            return {**USAGE, "accountId": self.identity}
        raise AssertionError(method)

    def wait_login(self, login_id, timeout=900):
        (self.home / "auth.json").write_text(json.dumps(auth()))

    def close(self):
        pass


class UsageTests(unittest.TestCase):
    def test_multi_buckets_authoritative_count_and_arbitrary_duration(self):
        result = normalize_usage(USAGE)
        self.assertEqual([b["id"] for b in result["buckets"]], ["codex", "other"])
        self.assertEqual(result["buckets"][0]["primary"]["remainingPercent"], 82)
        self.assertEqual(result["resetCredits"]["availableCount"], 3)
        self.assertEqual(len(result["resetCredits"]["credits"]), 1)

    def test_missing_values_are_never_zero_or_unlimited(self):
        result = normalize_usage({"rateLimits": {"primary": {"usedPercent": None}}})
        self.assertIsNone(result["buckets"][0]["primary"]["remainingPercent"])
        self.assertIsNone(result["buckets"][0]["secondary"])
        self.assertIsNone(result["resetCredits"])
        zero = normalize_usage({"rateLimitResetCredits": {"availableCount": 0, "credits": []}})
        self.assertEqual(zero["resetCredits"], {"availableCount": 0, "credits": []})
        self.assertIsNone(normalize_usage({"rateLimitResetCredits": {"availableCount": None}})["resetCredits"]["availableCount"])

    def test_bad_numbers_and_percent_clamping(self):
        result = normalize_usage({"rateLimits": {"primary": {"usedPercent": float("nan")}, "secondary": {"usedPercent": 105}}})
        self.assertIsNone(result["buckets"][0]["primary"]["remainingPercent"])
        self.assertEqual(result["buckets"][0]["secondary"]["remainingPercent"], 0)

    def test_cli_missing_and_control_char_output(self):
        state = {"accounts": [{"label": "Name\x1b[31m", "buckets": [], "resetCredits": None}], "now": time.time()}
        output = render(state)
        self.assertIn("초기화권: 확인 불가", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("초기화권: 0개", output)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sources = self.base / "source"
        self.sources.mkdir()
        self.store = self.sources / "workspace-logins"
        self.store.mkdir()
        (self.sources / "auth.json").write_text(json.dumps(auth()))
        (self.store / "personal.json").write_text(json.dumps(auth(expiry=1)))
        FakeSession.fail = False
        FakeSession.called = []
        self.manager = DashboardManager(self.base / "private", source_home=self.sources,
                                        auto_start=False, session_factory=FakeSession)

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def test_deduplicate_freshest_read_only_source_and_redaction(self):
        before = {p: p.read_bytes() for p in self.sources.rglob("*.json")}
        self.assertEqual(len(self.manager.state()["accounts"]), 1)
        account_id = self.manager.state()["accounts"][0]["id"]
        self.manager._poll_one(account_id)
        account = self.manager.state()["accounts"][0]
        self.assertEqual(account["status"], "ok")
        self.assertEqual(account["buckets"][0]["primary"]["remainingPercent"], 82)
        self.assertEqual(before, {p: p.read_bytes() for p in self.sources.rglob("*.json")})
        output = json.dumps(self.manager.state())
        self.assertNotIn("PRIVATE_REFRESH_SECRET", output)
        self.assertNotIn("accessToken", output)
        self.assertNotIn("_sources", output)
        self.assertFalse(any((self.base / "private").rglob("auth.json")))
        self.assertNotIn("account/rateLimitResetCredit/consume", FakeSession.called)

    def test_stale_success_retained_after_failure(self):
        account_id = self.manager.state()["accounts"][0]["id"]
        self.manager._poll_one(account_id)
        before = self.manager.state()["accounts"][0]
        FakeSession.fail = True
        self.manager._poll_one(account_id)
        after = self.manager.state()["accounts"][0]
        self.assertEqual(after["status"], "error")
        self.assertEqual(after["buckets"], before["buckets"])
        self.assertEqual(after["lastSuccessAt"], before["lastSuccessAt"])

    def test_expired_account_independent_and_removal_persists(self):
        (self.store / "business.json").write_text(json.dumps(auth("other-workspace", expiry=1)))
        self.manager._discover()
        self.assertEqual(len(self.manager.state()["accounts"]), 2)
        for account in self.manager.state()["accounts"]:
            self.manager._poll_one(account["id"])
        self.assertEqual(sorted(a["status"] for a in self.manager.state()["accounts"]), ["error", "ok"])
        bad = next(a for a in self.manager.state()["accounts"] if a["status"] == "error")
        self.manager.remove_account(bad["id"])
        self.manager._discover()
        self.assertEqual(len(self.manager.state()["accounts"]), 1)
        self.assertTrue((self.store / "business.json").exists())

    def test_owned_login_persisted_isolated_and_private(self):
        before = (self.sources / "auth.json").read_bytes()
        login = self.manager.add_account("Independent account")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            entry = self.manager.state()["logins"][0]
            if entry["status"] != "pending":
                break
            time.sleep(0.01)
        self.assertEqual(entry["status"], "complete")
        self.assertEqual((self.sources / "auth.json").read_bytes(), before)
        account = self.manager.state()["accounts"][0]
        self.assertEqual(account["source"], "dashboard")
        private_auth = next((self.base / "private/accounts").rglob("auth.json"))
        self.assertEqual(private_auth.stat().st_mode & 0o777, 0o600)
        self.assertEqual(private_auth.parent.stat().st_mode & 0o777, 0o700)
        manifest = (self.base / "private/accounts.json")
        self.assertEqual(manifest.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("PRIVATE_REFRESH_SECRET", manifest.read_text())

    def test_storage_cannot_be_main_auth_root(self):
        with self.assertRaises(ValueError):
            DashboardManager(self.sources, source_home=self.sources, auto_start=False)

    def test_failed_manifest_write_keeps_existing_login(self):
        with mock.patch("backend.private_json", side_effect=OSError("disk unavailable")):
            self.manager.add_account("Replacement")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                login = self.manager.state()["logins"][0]
                if login["status"] != "pending":
                    break
                time.sleep(0.01)
            self.assertEqual(login["status"], "failed")
            self.assertEqual(self.manager.state()["accounts"][0]["source"], "existing")


class ProtocolTests(unittest.TestCase):
    def test_jsonl_interleaving_error_redaction_and_forbidden_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake-codex"
            path.write_text("""#!/usr/bin/env python3
import json,sys
for line in sys.stdin:
    m=json.loads(line)
    if 'id' not in m: continue
    print(json.dumps({'method':'unrelated/notification','params':{}}), flush=True)
    result={'id':m['id'],'result':{}}
    if m['method']=='account/read': result={'id':m['id'],'error':{'code':-32603,'message':'PRIVATE_SECRET'}}
    print(json.dumps(result),flush=True)
""")
            path.chmod(0o700)
            with CodexSession(directory, str(path)) as session:
                with self.assertRaises(ProtocolError) as caught:
                    session.request("account/read")
                self.assertNotIn("PRIVATE_SECRET", str(caught.exception))
                self.assertEqual(caught.exception.code, -32603)
                with self.assertRaises(ProtocolError):
                    session.request("account/rateLimitResetCredit/consume")
            self.assertIsNotNone(session.proc.poll())


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.manager = DemoManager()
        self.server = create_server(self.manager, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        status = response.status
        data = response.read()
        connection.close()
        return status, data

    def test_state_public_static_allowlist_and_csp(self):
        status, data = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(data)["demo"])
        self.assertEqual(self.request("GET", "/../backend.py")[0], 404)
        self.assertEqual(self.request("GET", "/.private/accounts.json")[0], 404)
        self.assertEqual(self.request("GET", "/")[0], 200)

    def test_cross_origin_and_rebinding_requests_rejected(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host": "attacker.invalid"})[0], 403)
        self.assertEqual(self.request("GET", "/api/state", headers={"Origin": "https://attacker.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "/api/refresh", "{}", {"Content-Type": "application/json"})[0], 403)
        headers = {"Content-Type": "application/json", "X-Dashboard-Request": "1"}
        self.assertEqual(self.request("POST", "/api/refresh", "{}", headers)[0], 202)
        self.assertEqual(self.request("POST", "/api/refresh", "[]", headers)[0], 400)


if __name__ == "__main__":
    unittest.main()
