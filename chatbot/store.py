from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time
from typing import Iterable, Iterator

from .text import index_terms, match_query


@dataclass(frozen=True)
class Record:
    message_id: int
    guild_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    created_at: float
    edited_at: float
    reply_to: int | None = None

    @property
    def url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    edited_at REAL NOT NULL,
    reply_to INTEGER,
    terms TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_scope_time ON messages(guild_id, channel_id, created_at);
CREATE INDEX IF NOT EXISTS messages_author ON messages(guild_id, author_id);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    terms, content='messages', content_rowid='message_id', tokenize='ascii'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, terms) VALUES (new.message_id, new.terms);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, terms)
    VALUES ('delete', old.message_id, old.terms);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, terms)
    VALUES ('delete', old.message_id, old.terms);
    INSERT INTO messages_fts(rowid, terms) VALUES (new.message_id, new.terms);
END;
CREATE TABLE IF NOT EXISTS optouts (
    guild_id INTEGER NOT NULL, author_id INTEGER NOT NULL,
    PRIMARY KEY(guild_id, author_id)
);
CREATE TABLE IF NOT EXISTS tombstones (
    message_id INTEGER PRIMARY KEY, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    day TEXT PRIMARY KEY, calls INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state (
    guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
    synced_at REAL NOT NULL, oldest_checked_id INTEGER NOT NULL,
    truncated INTEGER NOT NULL,
    PRIMARY KEY(guild_id, channel_id)
);
"""

UPSERT = """
INSERT INTO messages(message_id,guild_id,channel_id,author_id,author_name,content,
                     created_at,edited_at,reply_to,terms)
SELECT :message_id,:guild_id,:channel_id,:author_id,:author_name,:content,
       :created_at,:edited_at,:reply_to,:terms
WHERE NOT EXISTS (SELECT 1 FROM optouts WHERE guild_id=:guild_id AND author_id=:author_id)
  AND NOT EXISTS (SELECT 1 FROM tombstones WHERE message_id=:message_id)
ON CONFLICT(message_id) DO UPDATE SET
    author_name=excluded.author_name, content=excluded.content,
    edited_at=excluded.edited_at, reply_to=excluded.reply_to, terms=excluded.terms
WHERE excluded.edited_at >= messages.edited_at;
"""


class Store:
    """Open a short-lived connection per operation; call through asyncio.to_thread.

    Transactions make index updates, opt-outs and deletion tombstones atomic.
    This is a one-process, one-local-disk MVP, not a shared network-file database.
    """
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA secure_delete=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def record(row: sqlite3.Row) -> Record:
        return Record(**{name: row[name] for name in Record.__dataclass_fields__})

    @staticmethod
    def _upsert(conn: sqlite3.Connection, records: Iterable[Record]) -> None:
        conn.executemany(UPSERT, ({**asdict(r), "terms": index_terms(r.content)} for r in records))

    def upsert(self, records: Iterable[Record]) -> None:
        with self.connection() as conn:
            self._upsert(conn, records)

    def delete(self, ids: Iterable[int], retention_days: int = 30) -> None:
        ids = list(ids)
        expiry = time.time() + retention_days * 86400
        with self.connection() as conn:
            conn.executemany("INSERT OR REPLACE INTO tombstones VALUES (?,?)", ((i, expiry) for i in ids))
            conn.executemany("DELETE FROM messages WHERE message_id=?", ((i,) for i in ids))

    def reconcile(self, guild: int, channel: int, records: list[Record],
                  lower_id: int, upper_id: int, truncated: bool) -> None:
        """Reconcile exactly the REST-checked ID interval; never infer older deletions."""
        if any(r.guild_id != guild or r.channel_id != channel for r in records):
            raise ValueError("채널 범위가 다른 메시지는 동기화할 수 없습니다.")
        with self.connection() as conn:
            conn.execute("CREATE TEMP TABLE seen(id INTEGER PRIMARY KEY)")
            conn.executemany("INSERT INTO seen VALUES (?)", ((r.message_id,) for r in records))
            conn.execute("""DELETE FROM messages WHERE guild_id=? AND channel_id=?
                AND message_id>=? AND message_id<? AND message_id NOT IN (SELECT id FROM seen)""",
                         (guild, channel, lower_id, upper_id))
            self._upsert(conn, records)
            conn.execute("INSERT OR REPLACE INTO sync_state VALUES (?,?,?,?,?)",
                         (guild, channel, time.time(), lower_id, int(truncated)))

    def search(self, guild: int, channels: Iterable[int], query: str,
               since: float, limit: int = 20) -> list[Record]:
        channel_ids = list(channels)
        if not channel_ids:
            return []  # An empty ACL is denial, never 'all channels'.
        expression = match_query(query)
        placeholders = ",".join("?" for _ in channel_ids)
        with self.connection() as conn:
            rows = conn.execute(f"""
                SELECT m.* FROM messages_fts JOIN messages m ON m.message_id=messages_fts.rowid
                WHERE messages_fts MATCH ? AND m.guild_id=?
                  AND m.channel_id IN ({placeholders}) AND m.created_at>=?
                ORDER BY bm25(messages_fts), m.created_at DESC LIMIT ?
                """, [expression, guild, *channel_ids, since, limit]).fetchall()
            return [self.record(r) for r in rows]

    def context(self, hit: Record, since: float, radius: int = 2) -> list[Record]:
        with self.connection() as conn:
            rows = conn.execute("""SELECT * FROM messages WHERE guild_id=? AND channel_id=?
                AND created_at>=? AND created_at BETWEEN ? AND ?
                ORDER BY ABS(created_at-?), message_id LIMIT ?""",
                (hit.guild_id, hit.channel_id, since, hit.created_at - 600,
                 hit.created_at + 600, hit.created_at, radius * 2 + 1)).fetchall()
            result = {r["message_id"]: self.record(r) for r in rows}
            if hit.reply_to:
                parent = conn.execute("""SELECT * FROM messages WHERE message_id=?
                    AND guild_id=? AND channel_id=? AND created_at>=?""",
                    (hit.reply_to, hit.guild_id, hit.channel_id, since)).fetchone()
                if parent:
                    result[parent["message_id"]] = self.record(parent)
            return sorted(result.values(), key=lambda r: r.message_id)

    def optout(self, guild: int, author: int, enabled: bool) -> int:
        with self.connection() as conn:
            if enabled:
                conn.execute("INSERT OR IGNORE INTO optouts VALUES (?,?)", (guild, author))
                return conn.execute("DELETE FROM messages WHERE guild_id=? AND author_id=?",
                                    (guild, author)).rowcount
            conn.execute("DELETE FROM optouts WHERE guild_id=? AND author_id=?", (guild, author))
            return 0

    def filter_eligible(self, records: list[Record]) -> list[Record]:
        if not records:
            return []
        with self.connection() as conn:
            opted = {(r[0], r[1]) for r in conn.execute("SELECT guild_id,author_id FROM optouts")}
            deleted = {r[0] for r in conn.execute("SELECT message_id FROM tombstones")}
        return [r for r in records if (r.guild_id, r.author_id) not in opted and r.message_id not in deleted]

    def unchanged(self, records: list[Record]) -> bool:
        # Reject a generated response when a source changed or was removed mid-request.
        with self.connection() as conn:
            for record in records:
                row = conn.execute("SELECT content,edited_at FROM messages WHERE message_id=?",
                                   (record.message_id,)).fetchone()
                if not row or row["content"] != record.content or row["edited_at"] != record.edited_at:
                    return False
        return True

    def cleanup(self, guild: int, channels: Iterable[int], retention_days: int) -> None:
        channel_ids = list(channels)
        with self.connection() as conn:
            conn.execute("DELETE FROM messages WHERE created_at<?", (time.time() - retention_days * 86400,))
            if channel_ids:
                placeholders = ",".join("?" for _ in channel_ids)
                conn.execute(f"DELETE FROM messages WHERE guild_id<>? OR channel_id NOT IN ({placeholders})",
                             [guild, *channel_ids])
            else:
                conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM tombstones WHERE expires_at<?", (time.time(),))

    def purge_channel(self, guild: int, channel: int) -> None:
        with self.connection() as conn:
            conn.execute("DELETE FROM messages WHERE guild_id=? AND channel_id=?", (guild, channel))
            conn.execute("DELETE FROM sync_state WHERE guild_id=? AND channel_id=?", (guild, channel))

    def reserve_call(self, daily_limit: int) -> None:
        day = datetime.now(timezone.utc).date().isoformat()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO usage VALUES (?,0)", (day,))
            result = conn.execute("UPDATE usage SET calls=calls+1 WHERE day=? AND calls<?", (day, daily_limit))
            if result.rowcount != 1:
                raise ValueError("오늘의 AI API 호출 한도에 도달했습니다. 한도는 UTC 자정에 갱신됩니다.")

    def stats(self, guild: int, channels: Iterable[int]) -> list[dict]:
        channel_ids = list(channels)
        if not channel_ids:
            return []
        placeholders = ",".join("?" for _ in channel_ids)
        with self.connection() as conn:
            rows = conn.execute(f"""SELECT channel_id,COUNT(*) AS messages,
                MIN(created_at) AS oldest,MAX(created_at) AS newest
                FROM messages WHERE guild_id=? AND channel_id IN ({placeholders})
                GROUP BY channel_id""", [guild, *channel_ids]).fetchall()
            return [dict(r) for r in rows]
