from __future__ import annotations

from datetime import datetime, timezone
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from dotenv import dotenv_values
from chatbot.admin import Admin, AdminError, Server
from chatbot.store import Record, Store


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env_patch = patch.dict(os.environ, {}, clear=True)
        self.env_patch.start()
        self.admin = Admin(self.root)

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def settings(self):
        return {"discord_token": "test-discord-secret", "guild_id": "123", "channel_ids": ["456"],
                "gemini_api_key": "test-gemini-secret", "allow_external_llm": True,
                "model": "gemini-test", "retention_days": 30, "daily_limit": 50,
                "sync_limit": 500, "timezone": "Asia/Seoul"}

    def test_save_redacts_secrets_and_preserves_existing_fields(self):
        path = self.root / ".env"
        path.write_text("# Keep this comment\nUNRELATED='multiline\nvalue'\nGEMINI_API_KEY='old-key'\n")
        self.admin.save(self.settings())
        state = self.admin.state()
        public = json.dumps(state)
        self.assertNotIn("test-discord-secret", public)
        self.assertNotIn("test-gemini-secret", public)
        self.assertTrue(state["settings"]["secrets"]["gemini_api_key"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(dotenv_values(path)["UNRELATED"], "multiline\nvalue")
        self.assertIn("# Keep this comment", path.read_text())
        self.admin.save({"gemini_api_key": "", "discord_token": "", "daily_limit": 70})
        self.assertEqual(self.admin.values()["GEMINI_API_KEY"], "test-gemini-secret")

    def test_invalid_values_do_not_write(self):
        for payload in ({"retention_days":0}, {"daily_limit":10001}, {"sync_limit":10},
                        {"guild_id":"-1"}, {"guild_id":str(2**63)}, {"channel_ids":["oops"]},
                        {"timezone":"Invalid/Zone"}, {"model":"x\nINJECT=value"},
                        {"database_path":"elsewhere.db"}, {"allow_external_llm":"true"},
                        {"channel_ids":"123"}, {"daily_limit":True}, {"model":None}):
            with self.subTest(payload=payload), self.assertRaises(AdminError):
                self.admin.save(payload)
            self.assertFalse((self.root / ".env").exists())

    def test_empty_database_is_read_only_and_honest(self):
        state = self.admin.state()
        self.assertEqual(state["stats"]["message_count"], 0)
        self.assertFalse(state["stats"]["database_exists"])
        self.assertFalse((self.root / "data").exists())
        self.assertEqual(state["bot"]["state"], "stopped")

    def test_question_channels_are_separate_and_overlap_fails_atomically(self):
        self.admin.save({"channel_ids":["456"], "question_channel_ids":["00789","789"]})
        self.assertEqual(self.admin.state()["settings"]["question_channel_ids"], ["789"])
        self.assertEqual([c["id"] for c in self.admin.state()["stats"]["channels"]], ["456"])
        before = self.admin.env_path.read_text()
        for payload in ({"question_channel_ids":["00456"]}, {"channel_ids":["789"]}, {"question_channel_ids":["oops"]}):
            with self.subTest(payload=payload), self.assertRaises(AdminError):
                self.admin.save(payload)
            self.assertEqual(self.admin.env_path.read_text(), before)
        self.admin.save({"question_channel_ids":[]})
        self.assertEqual(self.admin.state()["settings"]["question_channel_ids"], [])

    def test_statistics_scope_guild_and_allowlist(self):
        self.admin.save(self.settings())
        store = Store(self.root / "data/chat.db")
        now = time.time()
        store.upsert([Record(1,123,456,1,"one","PRIVATE BODY",now,now),
                      Record(2,123,999,1,"hidden","SECRET HIDDEN",now,now),
                      Record(3,777,456,1,"other","OTHER GUILD",now,now)])
        store.reserve_call(50)
        state = self.admin.state()
        self.assertEqual(state["stats"]["message_count"], 1)
        self.assertEqual(state["stats"]["utc_calls"], 1)
        self.assertEqual(state["stats"]["channels"][0]["messages"], 1)
        self.assertEqual(sum(d["count"] for d in state["stats"]["series"]), 1)
        self.assertNotIn("PRIVATE BODY", json.dumps(state))
        self.assertNotIn("999", json.dumps(state["stats"]["channels"]))

    def test_zero_prefixed_ids_are_canonicalized(self):
        self.admin.save({"guild_id":"00123","channel_ids":["00456","456"]})
        self.assertEqual(self.admin.values()["INDEX_CHANNEL_IDS"], "456")
        # Existing dotenv aliases must also be safe, before any settings save.
        (self.root/".env").write_text("DISCORD_GUILD_ID=00123\nINDEX_CHANNEL_IDS=00456,456\n")
        store = Store(self.root/"data/chat.db")
        now = time.time()
        store.upsert([Record(1,123,456,1,"test","body",now,now)])
        state = self.admin.state()
        self.assertEqual(state["settings"]["channel_ids"], ["456"])
        self.assertEqual(state["stats"]["message_count"], 1)

    def test_close_waits_for_pending_start_and_rejects_new_start(self):
        self.admin.save(self.settings())
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        child = MagicMock()
        child.poll.return_value = None
        child.wait.return_value = 0
        def launch(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return child
        def close():
            self.admin.close()
            closed.set()
        with patch("chatbot.admin.subprocess.Popen",side_effect=launch), patch.object(self.admin,"_watch"):
            starter=threading.Thread(target=self.admin.start)
            starter.start()
            self.assertTrue(entered.wait(3))
            closer=threading.Thread(target=close)
            closer.start()
            self.assertFalse(closed.wait(0.05))
            release.set()
            starter.join(3); closer.join(3)
            self.assertTrue(closed.is_set())
        self.assertIsNone(self.admin.child)
        child.send_signal.assert_called_once()
        with self.assertRaises(AdminError):self.admin.start()

    def test_local_env_overrides_inherited_values(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY":"inherited", "GEMINI_MODEL":"inherited"}):
            self.admin.save({"gemini_api_key":"local", "model":"local-model"})
            self.assertEqual(self.admin.values()["GEMINI_API_KEY"], "local")
            self.assertEqual(self.admin.values()["GEMINI_MODEL"], "local-model")

    def test_only_owned_process_is_stopped(self):
        with patch("chatbot.admin.subprocess.Popen") as popen:
            with self.assertRaises(AdminError):
                self.admin.stop()
            popen.assert_not_called()
        child = MagicMock()
        child.poll.return_value = None
        child.wait.return_value = 0
        self.admin.child = child
        self.admin.stop()
        child.send_signal.assert_called_once()
        child.wait.assert_called_once()
        self.assertIsNone(self.admin.child)

    def test_start_validates_and_does_not_auto_start(self):
        with patch("chatbot.admin.subprocess.Popen") as popen:
            with self.assertRaises(AdminError):
                self.admin.start()
            popen.assert_not_called()

    def test_start_env_and_concurrent_start_settings_guard(self):
        self.admin.save(self.settings())
        child = MagicMock()
        child.poll.return_value = None
        child.pid = 1234
        with patch("chatbot.admin.subprocess.Popen",return_value=child) as popen, patch.object(self.admin,"_watch"):
            self.admin.start()
            args = popen.call_args
            self.assertEqual(args.args[0][-2:], ["-m","chatbot.bot"])
            self.assertNotIn("shell",args.kwargs)
            self.assertEqual(args.kwargs["env"]["GEMINI_API_KEY"],"test-gemini-secret")
            self.assertEqual(self.admin.status()["state"],"starting")
            with self.assertRaises(AdminError):self.admin.start()
            with self.assertRaises(AdminError):self.admin.save({"model":"new"})
            self.assertEqual(popen.call_count,1)
        self.admin.child = None

    def test_output_not_persisted_and_connected_event_is_explicit(self):
        child = MagicMock()
        child.stdout = io.StringIO("SECRET KEY raw error body\nBot connected; configured_channels=1\n")
        child.wait.return_value = 0
        child.poll.return_value = 0
        self.admin.child = child
        self.admin._watch(child)
        events = json.dumps(list(self.admin.events), ensure_ascii=False)
        self.assertNotIn("SECRET KEY", events)
        self.assertIn("Discord에 연결",events)
        self.assertFalse(self.admin.connected)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.admin = Admin(Path(cls.tmp.name))
        cls.server = Server(0, cls.admin)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(2)
        cls.tmp.cleanup()

    def request(self, method, path, data=None, headers=None):
        conn = HTTPConnection("127.0.0.1",self.port, timeout=5)
        body = json.dumps(data) if data is not None else None
        conn.request(method,path,body=body,headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        conn.close()
        return result

    def auth(self):
        return {"Origin":f"http://127.0.0.1:{self.port}","X-Admin-Token":self.admin.token,"Content-Type":"application/json"}

    def test_page_and_state_headers(self):
        status,headers,raw = self.request("GET","/")
        self.assertEqual(status,200)
        self.assertIn('봇 관리'.encode(),raw)
        self.assertEqual(headers['X-Frame-Options'],'DENY')
        status,headers,raw = self.request("GET","/api/state")
        self.assertEqual(status,200)
        self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.assertIn('csrf_token',json.loads(raw))

    def test_dns_rebinding_host_and_traversal_denied(self):
        self.assertEqual(self.request("GET","/api/state",headers={"Host":"evil.example"})[0],403)
        self.assertEqual(self.request("GET","/../.env")[0],404)
        self.assertEqual(self.request("GET","/.env")[0],404)

    def test_csrf_missing_and_cross_origin_denied(self):
        for headers in ({}, {**self.auth(),"Origin":"https://evil.example"},
                        {**self.auth(),"X-Admin-Token":"bad"}):
            self.assertEqual(self.request("POST","/api/settings",{},headers)[0],403)

    def test_post_json_and_validation(self):
        self.assertEqual(self.request("POST","/api/settings",{}, {**self.auth(),"Content-Type":"text/plain"})[0],415)
        self.assertEqual(self.request("POST","/api/settings", {"daily_limit":0}, self.auth())[0],400)
        status,_,raw=self.request("POST","/api/settings", {"gemini_api_key":"PRIVATE_SENTINEL"}, self.auth())
        self.assertEqual(status,200)
        self.assertNotIn(b"PRIVATE_SENTINEL",raw)
        self.assertEqual(self.request("POST","/not-an-action",{},self.auth())[0],404)

    def test_large_request_denied(self):
        self.assertEqual(self.request("POST","/api/settings", {"model":"a"*17000},self.auth())[0],413)



if __name__ == '__main__':
    unittest.main()
