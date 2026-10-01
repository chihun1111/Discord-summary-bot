from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo
import os

from dotenv import load_dotenv

DEFAULT_MODEL = "gemini-3.5-flash-lite"


@dataclass(frozen=True)
class Config:
    token: str
    guild_id: int
    channel_ids: frozenset[int]
    database: Path
    retention_days: int
    timezone: str
    api_key: str
    model: str
    allow_external_llm: bool
    llm_daily_calls: int
    sync_limit: int
    question_channel_ids: frozenset[int] = frozenset()

    @classmethod
    def load(cls) -> "Config":
        load_dotenv()
        token = os.getenv("DISCORD_TOKEN", "").strip()
        try:
            guild = int(os.getenv("DISCORD_GUILD_ID", "0"))
            channels = frozenset(int(x.strip()) for x in os.getenv("INDEX_CHANNEL_IDS", "").split(",") if x.strip())
            questions = frozenset(int(x.strip()) for x in os.getenv("QUESTION_CHANNEL_IDS", "").split(",") if x.strip())
            retention = int(os.getenv("RETENTION_DAYS", "30"))
            daily = int(os.getenv("LLM_DAILY_CALL_LIMIT", "50"))
            sync_limit = int(os.getenv("SYNC_MESSAGE_LIMIT", "500"))
        except ValueError as exc:
            raise ValueError("서버/채널 ID 및 숫자 설정을 확인하세요.") from exc
        if not token or guild <= 0 or not channels or any(x <= 0 for x in channels):
            raise ValueError("DISCORD_TOKEN, DISCORD_GUILD_ID, INDEX_CHANNEL_IDS를 .env에 설정하세요.")
        if any(x <= 0 or x >= 2**63 for x in questions) or len(questions) > 100:
            raise ValueError("QUESTION_CHANNEL_IDS에는 올바른 채널 ID를 최대 100개 입력하세요.")
        if channels & questions:
            raise ValueError("수집 채널과 질문 채널은 서로 다르게 설정하세요.")
        if not 1 <= retention <= 365 or not 1 <= daily <= 10000 or not 100 <= sync_limit <= 1000:
            raise ValueError("보관일 1~365, 일일 API 호출 1~10000, 동기화 메시지 100~1000 범위로 설정하세요.")
        tz = os.getenv("TIMEZONE", "Asia/Seoul")
        ZoneInfo(tz)  # Validate at startup, not during a command.
        return cls(token, guild, channels, Path(os.getenv("DATABASE_PATH", "data/chat.db")),
                   retention, tz, os.getenv("GEMINI_API_KEY", "").strip(),
                   os.getenv("GEMINI_MODEL", "").strip() or DEFAULT_MODEL,
                   os.getenv("ALLOW_EXTERNAL_LLM", "false").lower() == "true",
                   daily, sync_limit, questions)
