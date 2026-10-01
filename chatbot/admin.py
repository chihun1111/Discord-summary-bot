"""Loopback management UI with optional authenticated HTTPS proxy access."""
from __future__ import annotations

import argparse
import base64
import binascii
from collections import deque
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values, set_key
from .config import DEFAULT_MODEL

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "admin_static"
FIELDS = {
    "discord_token": "DISCORD_TOKEN", "guild_id": "DISCORD_GUILD_ID",
    "channel_ids": "INDEX_CHANNEL_IDS", "gemini_api_key": "GEMINI_API_KEY",
    "question_channel_ids": "QUESTION_CHANNEL_IDS",
    "model": "GEMINI_MODEL", "allow_external_llm": "ALLOW_EXTERNAL_LLM",
    "retention_days": "RETENTION_DAYS", "daily_limit": "LLM_DAILY_CALL_LIMIT",
    "sync_limit": "SYNC_MESSAGE_LIMIT", "timezone": "TIMEZONE",
}
DEFAULTS = {"DISCORD_TOKEN": "", "DISCORD_GUILD_ID": "", "INDEX_CHANNEL_IDS": "",
            "GEMINI_API_KEY": "", "GEMINI_MODEL": DEFAULT_MODEL,
            "ALLOW_EXTERNAL_LLM": "false", "RETENTION_DAYS": "30",
            "LLM_DAILY_CALL_LIMIT": "50", "SYNC_MESSAGE_LIMIT": "500",
            "TIMEZONE": "Asia/Seoul", "DATABASE_PATH": "data/chat.db"}
DEFAULTS["QUESTION_CHANNEL_IDS"] = ""
SECRET_FIELDS = {"discord_token", "gemini_api_key"}


class AdminError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def valid_id(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9]{1,19}", value)) and 0 < int(value) < 2**63


class Admin:
    def __init__(self, root: Path = ROOT):
        self.root = root.resolve()
        self.env_path = self.root / ".env"
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.child: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.connected = False
        self.closing = False
        self.local_only = True
        self.events: deque[dict] = deque(maxlen=100)
        self.add_event("info", "관리 화면이 준비되었습니다.")

    def add_event(self, level: str, message: str) -> None:
        with self.lock:
            self.events.appendleft({"time": time.time(), "level": level, "message": message})

    def values(self) -> dict[str, str]:
        # Saved local settings take precedence for children managed by this UI.
        result = {key: os.environ.get(key, value) for key, value in DEFAULTS.items()}
        if self.env_path.exists():
            saved = dotenv_values(self.env_path, interpolate=False)
            result.update({k: v for k, v in saved.items() if k in DEFAULTS and v is not None})
        result = {k: v.strip() for k, v in result.items()}
        if valid_id(result["DISCORD_GUILD_ID"]):
            result["DISCORD_GUILD_ID"] = str(int(result["DISCORD_GUILD_ID"]))
        for key in ("INDEX_CHANNEL_IDS", "QUESTION_CHANNEL_IDS"):
            channels = [v.strip() for v in result[key].split(",") if v.strip()]
            result[key] = ",".join(dict.fromkeys(str(int(v)) if valid_id(v) else v for v in channels))
        return result

    @staticmethod
    def public_settings(values: dict[str, str]) -> dict:
        result = {field: values[key] for field, key in FIELDS.items() if field not in SECRET_FIELDS}
        result["channel_ids"] = [s.strip() for s in values["INDEX_CHANNEL_IDS"].split(",") if s.strip()]
        result["question_channel_ids"] = [s.strip() for s in values["QUESTION_CHANNEL_IDS"].split(",") if s.strip()]
        result["allow_external_llm"] = values["ALLOW_EXTERNAL_LLM"].lower() == "true"
        result["model"] = result["model"] or DEFAULT_MODEL
        result["secrets"] = {field: bool(values[FIELDS[field]]) for field in SECRET_FIELDS}
        result["database_path"] = values["DATABASE_PATH"]
        return result

    def status(self) -> dict:
        with self.lock:
            code = self.child.poll() if self.child else None
            alive = self.child is not None and code is None
            state = ("connected" if self.connected else "starting") if alive else (
                "failed" if code not in (None, 0) else "stopped")
            return {"state": state, "pid": self.child.pid if alive else None,
                    "started_at": self.started_at if alive else None, "exit_code": code}

    def checks(self, values: dict[str, str]) -> list[dict]:
        channels = [s.strip() for s in values["INDEX_CHANNEL_IDS"].split(",") if s.strip()]
        questions = [s.strip() for s in values["QUESTION_CHANNEL_IDS"].split(",") if s.strip()]
        missing = [n for n in ("discord", "openai", "dotenv") if importlib.util.find_spec(n) is None]
        ai = values["ALLOW_EXTERNAL_LLM"].lower() == "true"
        return [
            {"id": "runtime", "label": "실행 환경", "ok": not missing,
             "detail": "필수 패키지 준비 완료" if not missing else "필수 패키지 설치가 필요합니다"},
            {"id": "discord", "label": "Discord 토큰", "ok": bool(values["DISCORD_TOKEN"]),
             "detail": "저장됨 · 연결 시 유효성 확인" if values["DISCORD_TOKEN"] else "봇 토큰을 입력해 주세요"},
            {"id": "guild", "label": "서버와 수집 채널", "ok": valid_id(values["DISCORD_GUILD_ID"]) and bool(channels) and all(valid_id(c) for c in channels),
             "detail": f"수집 채널 {len(channels)}개" if channels else "서버 ID와 채널 ID를 설정해 주세요"},
            {"id": "gemini", "label": "Gemini 요약", "ok": not ai or bool(values["GEMINI_API_KEY"]),
             "detail": ("키 저장됨 · 연결 검증 전" if values["GEMINI_API_KEY"] else "API 키를 입력해 주세요") if ai else "AI 비활성 · 검색만 사용 가능"},
            {"id": "questions", "label": "질문 채널", "ok": all(valid_id(c) for c in questions) and not set(channels).intersection(questions),
             "detail": f"{len(questions)}개 · 새 질문에 스레드로 답변" if questions else "미설정 · 자동 스레드 답변 비활성"},
        ]

    def save(self, payload: dict) -> None:
        with self.lock:
            if self.child and self.child.poll() is None:
                raise AdminError("설정을 변경하려면 먼저 봇을 중지해 주세요.", 409)
            if not isinstance(payload, dict) or set(payload) - set(FIELDS):
                raise AdminError("지원하지 않는 설정입니다.")
            updates: dict[str, str] = {}
            for field, value in payload.items():
                if field == "allow_external_llm":
                    if not isinstance(value, bool):
                        raise AdminError("AI 활성화 값이 올바르지 않습니다.")
                    value = "true" if value else "false"
                elif field in ("channel_ids", "question_channel_ids"):
                    if not isinstance(value, list) or len(value) > 100 or not all(isinstance(v, str) for v in value):
                        raise AdminError("채널 ID는 최대 100개의 문자열 목록으로 입력하세요.")
                    value = ",".join(dict.fromkeys(v.strip() for v in value if v.strip()))
                elif isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise AdminError("설정 값의 형식을 확인하세요.")
                value = str(value).strip()
                if len(value) > 4096 or any(ord(c) < 32 for c in value):
                    raise AdminError("설정 값에 허용되지 않는 문자나 길이가 있습니다.")
                if field in SECRET_FIELDS and not value:
                    continue  # Blank secret fields never erase an existing key.
                if field == "guild_id" and value and not valid_id(value):
                    raise AdminError("서버 ID는 올바른 숫자로 입력하세요.")
                if field == "guild_id" and value:
                    value = str(int(value))
                if field in ("channel_ids", "question_channel_ids") and value and not all(valid_id(v) for v in value.split(",")):
                    raise AdminError("채널 ID는 올바른 숫자로 입력하세요.")
                if field in ("channel_ids", "question_channel_ids") and value:
                    value = ",".join(dict.fromkeys(str(int(v)) for v in value.split(",")))
                if field in ("retention_days", "daily_limit", "sync_limit"):
                    limits = {"retention_days": (1, 365), "daily_limit": (1, 10000), "sync_limit": (100, 1000)}
                    low, high = limits[field]
                    if not value.isascii() or not value.isdigit() or not low <= int(value) <= high:
                        raise AdminError(f"{field} 값은 {low}~{high} 범위여야 합니다.")
                if field == "model":
                    value = value or DEFAULT_MODEL
                    if not re.fullmatch(r"[a-zA-Z0-9._/-]{1,120}", value):
                        raise AdminError("모델 이름 형식을 확인하세요.")
                if field == "timezone":
                    try:
                        ZoneInfo(value)
                    except (ValueError, ZoneInfoNotFoundError):
                        raise AdminError("올바른 시간대를 입력하세요.") from None
                updates[FIELDS[field]] = value
            merged = {**self.values(), **updates}
            if (set(merged["INDEX_CHANNEL_IDS"].split(",")) - {""}) & (set(merged["QUESTION_CHANNEL_IDS"].split(",")) - {""}):
                raise AdminError("수집 채널과 질문 채널은 서로 다르게 설정하세요.")
            # Preserve unrelated/multiline dotenv entries; publish all edits atomically.
            fd, temporary = tempfile.mkstemp(prefix=".admin-env-", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(self.env_path.read_text() if self.env_path.exists() else "# Local bot settings\n")
                os.chmod(temporary, 0o600)
                for key, value in updates.items():
                    set_key(temporary, key, value, quote_mode="always")
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.env_path)
            finally:
                Path(temporary).unlink(missing_ok=True)
            self.add_event("success", "설정을 저장했습니다. 다음 봇 시작 시 적용됩니다.")

    def start(self) -> None:
        with self.lock:
            if self.closing:
                raise AdminError("관리 서버가 종료 중입니다.", 409)
            if self.child and self.child.poll() is None:
                raise AdminError("이미 이 관리 화면에서 봇을 실행 중입니다.", 409)
            values = self.values()
            if any(not c["ok"] for c in self.checks(values)):
                raise AdminError("실행 준비 항목을 확인하고 설정을 완료해 주세요.")
            # Validate remaining settings with the same Config loader in the child.
            env = os.environ.copy()
            env.update(values)
            env["PYTHONUNBUFFERED"] = "1"
            child = subprocess.Popen([sys.executable, "-m", "chatbot.bot"], cwd=self.root,
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace", start_new_session=True)
            self.child, self.started_at, self.connected = child, time.time(), False
            self.add_event("info", "봇을 시작했습니다. Discord 연결을 기다리는 중입니다.")
            threading.Thread(target=self._watch, args=(child,), daemon=True).start()

    def _watch(self, child: subprocess.Popen) -> None:
        assert child.stdout is not None
        # No raw logs are retained: provider/Discord errors can contain sensitive data.
        for line in iter(child.stdout.readline, ""):
            if "Bot connected; configured_channels=" in line:
                with self.lock:
                    if self.child is child:
                        self.connected = True
                        self.add_event("success", "Discord에 연결되었습니다. 명령어를 사용할 수 있습니다.")
            elif "Bot disconnected from Discord" in line:
                with self.lock:
                    if self.child is child:
                        self.connected = False
                        self.add_event("warning", "Discord 연결이 끊겼습니다. 다시 연결을 기다립니다.")
            elif "Bot resumed Discord session" in line:
                with self.lock:
                    if self.child is child:
                        self.connected = True
                        self.add_event("success", "Discord 연결이 복구되었습니다.")
            elif "Command failed:" in line:
                self.add_event("warning", "봇 명령 처리에 실패했습니다. API 설정과 권한을 확인하세요.")
        child.stdout.close()
        code = child.wait()
        with self.lock:
            if self.child is child:
                self.connected = False
                self.add_event("warning" if code else "info", "봇 프로세스가 종료되었습니다." + (" 설정·토큰·권한을 확인하세요." if code else ""))

    def stop(self) -> None:
        with self.lock:
            child = self.child
            if not child or child.poll() is not None:
                raise AdminError("이 관리 화면에서 실행 중인 봇이 없습니다.", 409)
            self.add_event("info", "봇을 중지하는 중입니다.")
            try:
                child.send_signal(signal.SIGINT)
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            self.child = None
            self.connected = False
            self.add_event("success", "봇이 중지되었습니다.")

    def close(self) -> None:
        with self.lock:
            self.closing = True
            if self.child and self.child.poll() is None:
                self.stop()

    def stats(self, values: dict[str, str]) -> dict:
        now = datetime.now(timezone.utc)
        days = [(now - timedelta(days=n)).date().isoformat() for n in range(6, -1, -1)]
        ids = list(dict.fromkeys(str(int(s.strip())) for s in values["INDEX_CHANNEL_IDS"].split(",") if valid_id(s.strip())))
        channels = {i: {"id": i, "messages": 0, "last_message_at": None, "last_synced_at": None, "truncated": False} for i in ids}
        path = Path(values["DATABASE_PATH"])
        if not path.is_absolute():
            path = self.root / path
        result = {"database_exists": path.is_file(), "database_error": False,
                  "message_count": 0, "last_message_at": None, "utc_calls": 0,
                  "series": [{"date": d, "count": 0} for d in days], "channels": list(channels.values())}
        if not path.is_file():
            return result
        try:
            conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute("SELECT calls FROM usage WHERE day=?", (now.date().isoformat(),)).fetchone()
                result["utc_calls"] = row["calls"] if row else 0
                if ids and valid_id(values["DISCORD_GUILD_ID"]):
                    guild = int(values["DISCORD_GUILD_ID"])
                    placeholders = ",".join("?" for _ in ids)
                    where = f"guild_id=? AND channel_id IN ({placeholders})"
                    params = [guild, *map(int, ids)]
                    rows = conn.execute(f"SELECT channel_id,COUNT(*) AS n,MAX(created_at) AS last FROM messages WHERE {where} GROUP BY channel_id", params)
                    for r in rows:
                        item = channels[str(r["channel_id"])]
                        item.update(messages=r["n"], last_message_at=r["last"])
                        result["message_count"] += r["n"]
                        result["last_message_at"] = max(result["last_message_at"] or 0, r["last"])
                    series = conn.execute(f"SELECT date(created_at,'unixepoch') AS day,COUNT(*) AS n FROM messages WHERE {where} AND created_at>=? GROUP BY day", [*params, datetime.fromisoformat(days[0]).replace(tzinfo=timezone.utc).timestamp()])
                    counts = {r["day"]: r["n"] for r in series}
                    result["series"] = [{"date": d, "count": counts.get(d, 0)} for d in days]
                    for r in conn.execute(f"SELECT channel_id,synced_at,truncated FROM sync_state WHERE {where}", params):
                        channels[str(r["channel_id"])].update(last_synced_at=r["synced_at"], truncated=bool(r["truncated"]))
            finally:
                conn.close()
        except (sqlite3.Error, OSError, ValueError):
            result["database_error"] = True
        return result

    def state(self) -> dict:
        with self.lock:
            values = self.values()
            return {"csrf_token": self.token, "bot": self.status(),
                    "settings": self.public_settings(values), "stats": self.stats(values),
                    "checks": self.checks(values), "events": list(self.events),
                    "server": {"provider": "Gemini", "local_only": self.local_only}}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, port: int, admin: Admin, *, public_origin: str | None = None,
                 auth_file: Path | str | None = None):
        self.admin = admin
        self.public_origin = public_origin
        self.public_host = None
        self.credentials = None
        if (public_origin is None) != (auth_file is None):
            raise ValueError("--public-origin and --auth-file must be supplied together")
        if public_origin is not None:
            # Accept only a literal HTTPS origin. Forwarded headers never grant trust.
            if not re.fullmatch(r"https://[a-zA-Z0-9.-]+(?::[0-9]+)?", public_origin):
                raise ValueError("public origin must be an HTTPS origin without a path")
            parsed = urlsplit(public_origin)
            if (not parsed.hostname or any(not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?", label)
                                           for label in parsed.hostname.split("."))
                    or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError("invalid public origin host or port")
            self.public_host = parsed.netloc
            try:
                with Path(auth_file).open(encoding="utf-8") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & ~0o600:
                        raise ValueError("auth file must be a private regular file (0600 or stricter)")
                    credentials = json.load(stream)
                if (not isinstance(credentials, dict) or set(credentials) != {"username", "password_sha256"}
                        or not isinstance(credentials["username"], str)
                        or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", credentials["username"])
                        or not isinstance(credentials["password_sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", credentials["password_sha256"])):
                    raise ValueError("auth file must contain a username and lowercase SHA-256 password digest")
                self.credentials = credentials
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError("cannot load valid private admin auth configuration") from exc
        super().__init__(("127.0.0.1", port), Handler)
        self.admin.local_only = public_origin is None


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "BotAdmin"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format: str, *args) -> None:
        pass

    def send_data(self, status: int, data: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if status == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="Bot Admin", charset="UTF-8"')
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def json(self, status: int, data: dict) -> None:
        self.send_data(status, json.dumps(data, ensure_ascii=False).encode())

    def trusted_host(self) -> bool:
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.server.public_host:
            hosts.add(self.server.public_host)
        return len(self.headers.get_all("Host", [])) == 1 and self.headers.get("Host") in hosts

    def authenticated(self) -> bool:
        credentials = self.server.credentials
        if credentials is None:
            return True
        try:
            headers = self.headers.get_all("Authorization", [])
            if len(headers) != 1:
                raise ValueError("one authorization header required")
            scheme, encoded = headers[0].split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError("Basic authentication required")
            username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
            username_ok = secrets.compare_digest(username.encode("utf-8"), credentials["username"].encode("utf-8"))
            password_ok = secrets.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(), credentials["password_sha256"])
            if username_ok & password_ok:
                return True
        except (ValueError, UnicodeError, binascii.Error):
            pass
        self.json(401, {"error": "Authentication required"})
        return False

    def do_GET(self) -> None:
        if not self.authenticated():
            return
        if not self.trusted_host():
            self.json(403, {"error": "허용된 관리 화면 주소로 접속해 주세요."})
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            try:
                self.json(200, self.server.admin.state())
            except Exception:
                self.json(500, {"error": "상태를 읽을 수 없습니다. 로컬 설정을 확인하세요."})
            return
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/styles.css": ("styles.css", "text/css; charset=utf-8"),
                 "/favicon.svg": ("favicon.svg", "image/svg+xml")}
        if path not in files:
            self.json(404, {"error": "페이지를 찾을 수 없습니다."})
            return
        filename, kind = files[path]
        try:
            self.send_data(200, (STATIC / filename).read_bytes(), kind)
        except OSError:
            self.json(404, {"error": "화면 파일을 찾을 수 없습니다."})

    def do_POST(self) -> None:
        if not self.authenticated():
            return
        expected_origin = (self.server.public_origin if self.server.public_host is not None
                           and self.headers.get("Host") == self.server.public_host
                           else f"http://{self.headers.get('Host')}")
        if (not self.trusted_host() or self.headers.get("Origin") != expected_origin
                or len(self.headers.get_all("Origin", [])) != 1
                or not secrets.compare_digest(self.headers.get("X-Admin-Token", "").encode(), self.server.admin.token.encode())):
            self.json(403, {"error": "페이지를 새로고침한 뒤 다시 시도해 주세요."})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self.json(415, {"error": "JSON 요청이 필요합니다."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 16384 or self.headers.get("Transfer-Encoding"):
                raise AdminError("요청 크기가 올바르지 않습니다.", 413)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise AdminError("JSON 객체가 필요합니다.")
            path = urlsplit(self.path).path
            if path == "/api/settings":
                self.server.admin.save(payload)
            elif path == "/api/bot/start":
                self.server.admin.start()
            elif path == "/api/bot/stop":
                self.server.admin.stop()
            else:
                raise AdminError("지원하지 않는 작업입니다.", 404)
            self.json(200, {"ok": True, "state": self.server.admin.state()})
        except AdminError as exc:
            self.json(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeError):
            self.json(400, {"error": "요청 값을 확인하세요."})
        except Exception:
            self.json(500, {"error": "작업을 완료하지 못했습니다. 로컬 파일 권한과 설정을 확인하세요."})


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot administration (binds to 127.0.0.1; optional authenticated HTTPS proxy)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--public-origin", help="HTTPS origin used by the reverse proxy")
    parser.add_argument("--auth-file", type=Path, help="private JSON file with username and password_sha256")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1–65535")
    os.umask(0o077)
    # One admin per checkout, even when an alternate port is requested.
    lock_path = ROOT / ".admin.lock"
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "이 프로젝트의 관리 화면이 이미 실행 중입니다.\n")
        admin = Admin()
        try:
            server = Server(args.port, admin, public_origin=args.public_origin, auth_file=args.auth_file)
        except ValueError as exc:
            parser.error(str(exc))
        except OSError:
            parser.exit(1, "관리 화면 포트를 사용할 수 없습니다. --port로 다른 포트를 지정하세요.\n")
        print(f"Bot admin ready: http://127.0.0.1:{args.port}", flush=True)
        def terminate(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
        try:
            server.serve_forever(poll_interval=0.3)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            admin.close()


if __name__ == "__main__":
    main()
