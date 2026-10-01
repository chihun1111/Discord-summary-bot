from __future__ import annotations

import base64
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from chatbot.admin import Server


ORIGIN = "https://hermes.tail85b0de.ts.net"
PASSWORD = "test-long-random-password:with-colon"
AUTH = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()


class RemoteAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth_file = Path(self.tmp.name) / "auth.json"
        self.credentials = {"username": "admin", "password_sha256": hashlib.sha256(PASSWORD.encode()).hexdigest()}
        self.write_credentials(self.credentials)
        self.admin = Mock(token="csrf-secret")
        self.admin.state.return_value = {"csrf_token": self.admin.token, "private": "state-content"}
        self.server = Server(0, self.admin, public_origin=ORIGIN, auth_file=self.auth_file)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def write_credentials(self, payload):
        self.auth_file.write_text(json.dumps(payload))
        self.auth_file.chmod(0o600)

    def request(self, method="GET", path="/api/state", *, headers=None):
        supplied = {"Host": ORIGIN.removeprefix("https://"), "Authorization": AUTH,
                    "Origin": ORIGIN, "X-Admin-Token": self.admin.token, "Content-Type": "application/json"}
        supplied.update(headers or {})
        supplied = {k: v for k, v in supplied.items() if v is not None}
        conn = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            conn.request(method, path, body="{}" if method == "POST" else None, headers=supplied)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_authenticated_state_static_and_mutation(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        for path in ("/api/state", "/", "/app.js", "/styles.css", "/favicon.svg"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 200)
        self.assertEqual(self.request("POST", "/api/settings")[0], 200)
        self.admin.save.assert_called_once_with({})

    def test_missing_wrong_and_malformed_auth_never_exposes_state_or_static(self):
        for authorization in (None, "Basic invalid!", "Bearer abc", "Basic YWJj", "Basic /w==",
                              "Basic " + base64.b64encode(b"wrong:wrong").decode(),
                              "Basic " + base64.b64encode(b"admin:wrong").decode(),
                              "Basic " + base64.b64encode(f"wrong:{PASSWORD}".encode()).decode()):
            for method, path in (("GET", "/api/state"), ("GET", "/"), ("GET", "/app.js"),
                                 ("POST", "/api/settings"), ("POST", "/api/bot/start")):
                with self.subTest(authorization=authorization, path=path):
                    status, headers, body = self.request(method, path, headers={"Authorization": authorization})
                    self.assertEqual(status, 401)
                    self.assertIn("Basic", headers["WWW-Authenticate"])
                    self.assertNotIn(b"state-content", body)
                    self.assertNotIn(b"csrf-secret", body)
        self.admin.state.assert_not_called()
        self.admin.save.assert_not_called()
        self.admin.start.assert_not_called()

    def test_loopback_host_does_not_bypass_auth(self):
        for hostname in ("127.0.0.1", "localhost"):
            host = f"{hostname}:{self.server.server_port}"
            for method in ("GET", "POST"):
                with self.subTest(host=host, method=method):
                    self.assertEqual(self.request(method, "/api/settings" if method == "POST" else "/api/state",
                                                 headers={"Host": host, "Origin": f"http://{host}", "Authorization": None})[0], 401)
            self.assertEqual(self.request(headers={"Host": host})[0], 200)
            self.assertEqual(self.request("POST", "/api/settings", headers={"Host": host, "Origin": f"http://{host}"})[0], 200)

    def test_host_origin_and_csrf_spoofs_rejected(self):
        bad_headers = [
            {"Host": "evil.example", "Origin": "http://evil.example"},
            {"Host": "hermes.tail85b0de.ts.net.evil.example"},
            {"Origin": "https://evil.example"},
            {"Origin": "http://hermes.tail85b0de.ts.net"},
            {"Origin": ORIGIN + "/"}, {"Origin": None},
            {"X-Admin-Token": None}, {"X-Admin-Token": "wrong"},
            {"Host": "evil.example", "X-Forwarded-Host": "hermes.tail85b0de.ts.net", "X-Forwarded-Proto": "https"},
            {"Origin": "http://hermes.tail85b0de.ts.net", "X-Forwarded-Proto": "https"},
        ]
        for headers in bad_headers:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/settings", headers=headers)[0], 403)
        self.assertEqual(self.request(headers={"Host": "evil.example", "X-Forwarded-Host": "hermes.tail85b0de.ts.net"})[0], 403)
        self.admin.save.assert_not_called()

    def test_invalid_startup_configuration_fails_closed(self):
        for origin in ("", "http://host", ORIGIN + "/", ORIGIN + "/path", ORIGIN + "?q=1", ORIGIN + "#fragment",
                       "https://admin@host", "https://host:0", "https://host:65536", "https://host:abc",
                       "https://host\\evil", "https://-host", "https://host..example", "https://host\n"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                Server(0, self.admin, public_origin=origin, auth_file=self.auth_file)
        for kwargs in ({"public_origin": ORIGIN}, {"auth_file": self.auth_file},
                       {"public_origin": ORIGIN, "auth_file": self.auth_file.parent / "missing"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Server(0, self.admin, **kwargs)
        for credentials in ({}, [], {**self.credentials, "extra": True}, {**self.credentials, "username": ""},
                            {**self.credentials, "username": "admin:other"}, {**self.credentials, "password_sha256": "A" * 64},
                            {**self.credentials, "password_sha256": 123}):
            self.write_credentials(credentials)
            with self.subTest(credentials=credentials), self.assertRaises(ValueError):
                Server(0, self.admin, public_origin=ORIGIN, auth_file=self.auth_file)
        self.write_credentials(self.credentials)
        for mode in (0o644, 0o640, 0o700):
            self.auth_file.chmod(mode)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                Server(0, self.admin, public_origin=ORIGIN, auth_file=self.auth_file)
        self.auth_file.chmod(0o400)
        private = Server(0, self.admin, public_origin=ORIGIN, auth_file=self.auth_file)
        private.server_close()

    def test_local_defaults_remain_unauthenticated_and_local_only(self):
        self.stop_server()
        self.server = Server(0, self.admin)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        host = f"localhost:{self.server.server_port}"
        headers = {"Host": host, "Origin": f"http://{host}", "Authorization": None}
        self.assertEqual(self.request(headers=headers)[0], 200)
        self.assertEqual(self.request("POST", "/api/settings", headers=headers)[0], 200)
        self.assertEqual(self.request()[0], 403)


if __name__ == "__main__":
    unittest.main()
