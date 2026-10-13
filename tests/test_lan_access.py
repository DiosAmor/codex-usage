"""LAN access stays opt-in and retains browser request isolation."""

from contextlib import redirect_stderr
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cli import main as cli_main
from server import create_server, DemoManager, main as server_main


class LanHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager = DemoManager()
        cls.server = create_server(cls.manager, 0, host="0.0.0.0",
                                   allowed_hosts=["192.168.1.20"])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]
        cls.host = f"192.168.1.20:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method="GET", path="/api/state", body=None, headers=()):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers or (("Host", self.host),):
            connection.putheader(key, value)
        encoded = body.encode() if isinstance(body, str) else body
        if encoded is not None:
            connection.putheader("Content-Length", str(len(encoded)))
        connection.endheaders(encoded)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_lan_pages_and_state_work_without_password(self):
        for path in ("/", "/app.js", "/calendar.js", "/app.css",
                     "/api/state", "/api/reset-calendar"):
            with self.subTest(path=path):
                status, body, headers = self.request(path=path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertNotIn("WWW-Authenticate", headers)
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        state = json.loads(self.request()[1])
        self.assertTrue(state["demo"])
        self.assertEqual(state["accessMode"], "lan")
        self.assertIn(f"http://{self.host}", state["accessUrls"])
        self.assertIn(f"http://{self.host}", self.server.access_urls)
        self.assertFalse(any("0.0.0.0" in value for value in self.server.access_urls))

    def test_unlisted_address_dns_and_wildcard_hosts_are_rejected(self):
        for host in (f"192.168.1.21:{self.port}", f"8.8.8.8:{self.port}",
                     f"evil.invalid:{self.port}", f"0.0.0.0:{self.port}",
                     f"192.168.1.20:{self.port + 1}", "192.168.1.20"):
            with self.subTest(host=host):
                status, body, _ = self.request(headers=(("Host", host),))
                self.assertEqual(status, 403)
                self.assertNotIn("accounts", json.loads(body))

    def test_duplicate_host_and_origin_are_rejected(self):
        cases = (
            (("Host", self.host), ("Host", self.host)),
            (("Host", self.host), ("Host", "evil.invalid")),
            (("Host", self.host), ("Origin", f"http://{self.host}"),
             ("Origin", f"http://{self.host}")),
            (("Host", self.host), ("Origin", f"http://{self.host}"),
             ("Origin", "https://evil.invalid")),
        )
        for headers in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)

    def test_origin_and_fetch_site_still_isolate_browser_requests(self):
        good = (("Host", self.host), ("Origin", f"http://{self.host}"),
                ("Sec-Fetch-Site", "same-origin"))
        self.assertEqual(self.request(headers=good)[0], 200)
        for origin in ("https://evil.invalid", f"https://{self.host}",
                       f"http://192.168.1.21:{self.port}", "null"):
            with self.subTest(origin=origin):
                self.assertEqual(self.request(headers=(("Host", self.host),
                                                       ("Origin", origin)))[0], 403)
        for site in ("same-site", "cross-site"):
            with self.subTest(site=site):
                self.assertEqual(self.request(headers=(("Host", self.host),
                                                       ("Sec-Fetch-Site", site)))[0], 403)

    def test_lan_mutations_keep_custom_header_requirement(self):
        plain_headers = (("Host", self.host), ("Content-Type", "application/json"),
                         ("Origin", f"http://{self.host}"))
        with mock.patch.object(self.manager, "refresh", wraps=self.manager.refresh) as refresh:
            self.assertEqual(self.request("POST", "/api/refresh", "{}", plain_headers)[0], 403)
            refresh.assert_not_called()
            valid = plain_headers + (("X-Dashboard-Request", "1"),)
            self.assertEqual(self.request("POST", "/api/refresh", "{}", valid)[0], 202)
            refresh.assert_called_once()
        status, _, _ = self.request("DELETE", "/api/accounts/demo-0", headers=plain_headers)
        self.assertEqual(status, 403)

    def test_static_allowlist_and_security_headers_remain(self):
        for path in ("/../backend.py", "/.private/accounts.json", "/auth.json"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 404)
        headers = self.request(path="/")[2]
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_public_peer_cannot_bypass_lan_scope_with_allowed_host(self):
        original_handler = self.server.RequestHandlerClass

        class PublicPeerHandler(original_handler):
            def __init__(self, request, client_address, server):
                super().__init__(request, ("8.8.8.8", client_address[1]), server)

        self.server.RequestHandlerClass = PublicPeerHandler
        try:
            status, body, _ = self.request(headers=(("Host", self.host),))
            self.assertEqual(status, 403)
            self.assertNotIn("accounts", json.loads(body))
        finally:
            self.server.RequestHandlerClass = original_handler


class LanConfigurationTests(unittest.TestCase):
    def test_local_default_keeps_existing_behavior(self):
        server = create_server(DemoManager(), 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
            for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
                with self.subTest(host=host):
                    connection = HTTPConnection("127.0.0.1", port, timeout=3)
                    connection.request("GET", "/api/state", headers={"Host": host})
                    response = connection.getresponse()
                    state = json.loads(response.read())
                    connection.close()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(state["accessMode"], "local")
            connection = HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/api/state", headers={"Host": f"192.168.1.20:{port}"})
            response = connection.getresponse()
            response.read()
            connection.close()
            self.assertEqual(response.status, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_invalid_bind_host_is_rejected_before_manager_access(self):
        for host in ("8.8.8.8", "192.0.2.20", "example.com", "localhost", "::1", "256.1.2.3"):
            with self.subTest(host=host):
                manager = mock.Mock(spec=DemoManager)
                with self.assertRaises(ValueError):
                    create_server(manager, 0, host=host)
                manager.state.assert_not_called()
                manager.refresh.assert_not_called()

    def test_server_bad_host_flags_do_not_start_account_manager(self):
        for arguments in (("--host", "8.8.8.8"), ("--host", "example.com"),
                          ("--lan", "--host", "127.0.0.1")):
            with self.subTest(arguments=arguments):
                with mock.patch.object(sys, "argv", ["server.py", *arguments]), \
                     mock.patch("backend.DashboardManager") as manager, \
                     redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        server_main()
                    self.assertEqual(caught.exception.code, 2)
                    manager.assert_not_called()

    def test_cli_network_flags_only_apply_to_serve(self):
        for arguments in (("status", "--lan"), ("watch", "--host", "192.168.1.20"),
                          ("list", "--lan"), ("serve", "--lan", "--host", "127.0.0.1")):
            with self.subTest(arguments=arguments):
                with mock.patch("cli.DashboardManager") as manager, \
                     redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        cli_main(list(arguments))
                    self.assertEqual(caught.exception.code, 2)
                    manager.assert_not_called()


if __name__ == "__main__":
    unittest.main()
