from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
import os
from typing import Literal

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .llm import Generation, LLM
from .store import Record, Store
from .text import match_query, safe_chunks
from .thread_qa import ThreadQA

log = logging.getLogger("chat-index")
UTC = timezone.utc


def clean(text: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


class ChatBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents, max_messages=None, allowed_mentions=discord.AllowedMentions.none())
        self.config = config
        self.store = Store(config.database)
        self.llm = LLM(config, self.store)
        self.tree = app_commands.CommandTree(self)
        self.channel_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.ai_lock = asyncio.Lock()
        self.thread_qa = ThreadQA(self)
        self.register_commands()
        self.tree.on_error = self.command_error

    async def setup_hook(self) -> None:
        await asyncio.to_thread(self.store.cleanup, self.config.guild_id, self.config.channel_ids, self.config.retention_days)
        guild = discord.Object(id=self.config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.reconcile_recent.start()

    async def close(self) -> None:
        self.reconcile_recent.cancel()
        await self.llm.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Bot connected; configured_channels=%d", len(self.config.channel_ids))

    async def on_disconnect(self) -> None:
        log.info("Bot disconnected from Discord")

    async def on_resumed(self) -> None:
        log.info("Bot resumed Discord session")

    async def command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, ValueError):
            message = str(original)
        elif isinstance(original, app_commands.CommandOnCooldown):
            message = f"호출이 너무 잦습니다. {original.retry_after:.0f}초 후 다시 사용하세요."
        elif isinstance(original, app_commands.CheckFailure):
            message = "이 명령을 실행할 권한이 없습니다."
        elif isinstance(original, (discord.Forbidden, discord.NotFound)):
            message = "서버/채널/원문 접근을 확인할 수 없습니다. 권한과 삭제 여부를 확인하세요."
        elif isinstance(original, (TimeoutError, asyncio.TimeoutError)):
            message = "처리 제한시간을 넘었습니다. 조회 범위를 줄여 다시 실행하세요."
        else:
            # Do not log prompts, message bodies, API keys or raw provider exceptions.
            log.error("Command failed: %s", type(original).__name__)
            message = "처리 중 오류가 발생했습니다. 운영자가 권한, 모델 접근, API 한도와 연결 상태를 확인해야 합니다."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.send_message(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Could not deliver command error")

    async def send(self, interaction: discord.Interaction, text: str) -> None:
        for chunk in safe_chunks(text):
            await interaction.followup.send(chunk, ephemeral=True, suppress_embeds=True,
                                            allowed_mentions=discord.AllowedMentions.none())

    def make_record(self, message: discord.Message) -> Record | None:
        if (message.guild is None or message.guild.id != self.config.guild_id
                or not isinstance(message.channel, discord.TextChannel)
                or message.channel.id not in self.config.channel_ids
                or message.channel.is_nsfw()
                or message.author.bot or message.webhook_id is not None
                or not message.content.strip()
                or message.created_at < datetime.now(UTC) - timedelta(days=self.config.retention_days)):
            return None
        return Record(message.id, message.guild.id, message.channel.id, message.author.id,
                      message.author.display_name, message.content, message.created_at.timestamp(),
                      (message.edited_at or message.created_at).timestamp(),
                      message.reference.message_id if message.reference else None)

    async def on_message(self, message: discord.Message) -> None:
        record = self.make_record(message)
        if record is not None:
            async with self.channel_locks[record.channel_id]:
                await asyncio.to_thread(self.store.upsert, [record])
        await self.thread_qa.handle(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.guild_id != self.config.guild_id or payload.channel_id not in self.config.channel_ids or "content" not in payload.data:
            return
        async with self.channel_locks[payload.channel_id]:
            try:
                channel = await self.fetch_channel(payload.channel_id)
                if not isinstance(channel, discord.TextChannel):
                    return
                message = await channel.fetch_message(payload.message_id)
                record = self.make_record(message)
                if record:
                    await asyncio.to_thread(self.store.upsert, [record])
                else:
                    # An empty body may later be edited back: do not tombstone it.
                    await asyncio.to_thread(self._remove_body_only, payload.message_id)
            except discord.NotFound:
                await asyncio.to_thread(self.store.delete, [payload.message_id], self.config.retention_days)
            except discord.HTTPException:
                log.warning("Edit refresh unavailable; periodic sync will retry")

    def _remove_body_only(self, message_id: int) -> None:
        with self.store.connection() as conn:
            conn.execute("DELETE FROM messages WHERE message_id=?", (message_id,))

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id == self.config.guild_id and payload.channel_id in self.config.channel_ids:
            async with self.channel_locks[payload.channel_id]:
                await asyncio.to_thread(self.store.delete, [payload.message_id], self.config.retention_days)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id == self.config.guild_id and payload.channel_id in self.config.channel_ids:
            async with self.channel_locks[payload.channel_id]:
                await asyncio.to_thread(self.store.delete, payload.message_ids, self.config.retention_days)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.guild.id == self.config.guild_id:
            async with self.channel_locks[channel.id]:
                await asyncio.to_thread(self.store.purge_channel, channel.guild.id, channel.id)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if guild.id == self.config.guild_id:
            await asyncio.to_thread(self.store.cleanup, guild.id, [], self.config.retention_days)

    async def allowed_channels(self, interaction: discord.Interaction,
                               requested: discord.TextChannel | None = None) -> dict[int, discord.TextChannel]:
        guild = interaction.guild
        if guild is None or guild.id != self.config.guild_id or self.user is None:
            raise ValueError("설정된 서버에서만 사용할 수 있습니다.")
        # REST refreshes membership and channel overwrites; guild role definitions also
        # receive Gateway updates. Threads/DMs/forum posts are deliberately unsupported.
        member, bot_member, channels = await asyncio.gather(
            guild.fetch_member(interaction.user.id), guild.fetch_member(self.user.id), guild.fetch_channels())
        result = {}
        for channel in channels:
            if not isinstance(channel, discord.TextChannel) or channel.id not in self.config.channel_ids or channel.is_nsfw():
                continue
            if requested is not None and channel.id != requested.id:
                continue
            user_permissions = channel.permissions_for(member)
            bot_permissions = channel.permissions_for(bot_member)
            if (user_permissions.view_channel and user_permissions.read_message_history
                    and bot_permissions.view_channel and bot_permissions.read_message_history):
                result[channel.id] = channel
        if not result:
            raise ValueError("읽기 권한이 있는 인덱싱 대상 일반 텍스트 채널이 없습니다.")
        return result

    async def verify_records(self, records: list[Record], channels: dict[int, discord.TextChannel]) -> list[Record]:
        """Re-fetch a bounded set of search hits before exposing stored chat content."""
        result = []
        for record in records:
            channel = channels.get(record.channel_id)
            if channel is None:
                continue
            async with self.channel_locks[channel.id]:
                try:
                    current = self.make_record(await channel.fetch_message(record.message_id))
                except discord.NotFound:
                    await asyncio.to_thread(self.store.delete, [record.message_id], self.config.retention_days)
                    continue
                # Other HTTP errors fail closed; never fall back to stored content.
                if current:
                    await asyncio.to_thread(self.store.upsert, [current])
                    result.append(current)
                else:
                    await asyncio.to_thread(self._remove_body_only, record.message_id)
        return await asyncio.to_thread(self.store.filter_eligible, result)

    async def scan(self, channel: discord.TextChannel, since: datetime, limit: int,
                   before_id: int | None = None) -> tuple[list[Record], bool, int | None, int]:
        async with self.channel_locks[channel.id]:
            upper = before_id or discord.utils.time_snowflake(datetime.now(UTC))
            raw = [m async for m in channel.history(limit=limit + 1, after=since,
                    before=discord.Object(id=upper), oldest_first=False)]
            truncated = len(raw) > limit
            page = raw[:limit]
            lower = page[-1].id if truncated else discord.utils.time_snowflake(since, high=True) + 1
            records = [r for m in page if (r := self.make_record(m)) is not None]
            await asyncio.to_thread(self.store.reconcile, self.config.guild_id, channel.id,
                                    records, lower, upper, truncated)
            records = await asyncio.to_thread(self.store.filter_eligible, records)
            return sorted(records, key=lambda r: r.message_id), truncated, page[-1].id if page else None, len(page)

    async def scan_window(self, channel: discord.TextChannel, since: datetime,
                          *, until: datetime | None = None) -> tuple[list[Record], int]:
        """Refresh every page in a fixed time window, including offline edits/deletes.

        Each page reconciles only its checked interval. A failed page leaves the
        preceding page marked truncated, and the next sync starts from the top.
        discord.py handles REST pagination and rate limits within each page.
        """
        records: list[Record] = []
        raw_count = 0
        cursor = discord.utils.time_snowflake(until) if until is not None else None
        scan_since = since - timedelta(milliseconds=1) if until is not None else since
        while True:
            page, more, next_before, count = await asyncio.wait_for(
                self.scan(channel, scan_since, self.config.sync_limit, cursor), timeout=180)
            records.extend(page)
            raw_count += count
            if not more:
                break
            if next_before is None or (cursor is not None and next_before >= cursor):
                raise ValueError("과거 기록 조회 커서가 진행되지 않아 동기화를 중단했습니다.")
            cursor = next_before
        records = await asyncio.to_thread(self.store.filter_eligible, records)
        if until is not None:
            records = [r for r in records if since.timestamp() <= r.created_at < until.timestamp()]
        return sorted(records, key=lambda record: record.message_id), raw_count

    @tasks.loop(minutes=15)
    async def reconcile_recent(self) -> None:
        try:
            await asyncio.to_thread(self.store.cleanup, self.config.guild_id, self.config.channel_ids, self.config.retention_days)
            for channel_id in sorted(self.config.channel_ids):
                try:
                    channel = await self.fetch_channel(channel_id)
                    if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.config.guild_id or channel.is_nsfw():
                        continue
                    perms = channel.permissions_for(channel.guild.me) if channel.guild.me else None
                    if not perms or not perms.view_channel or not perms.read_message_history:
                        continue
                    since = datetime.now(UTC) - timedelta(days=min(3, self.config.retention_days))
                    _, raw_count = await self.scan_window(channel, since)
                    log.info("Recent sync complete for channel=%s; checked_messages=%d", channel_id, raw_count)
                except discord.NotFound:
                    await asyncio.to_thread(self.store.purge_channel, self.config.guild_id, channel_id)
                except Exception as exc:
                    log.warning("Channel sync skipped: %s", type(exc).__name__)
        except Exception as exc:
            log.warning("Periodic cleanup failed: %s", type(exc).__name__)

    @reconcile_recent.before_loop
    async def before_reconcile(self) -> None:
        await self.wait_until_ready()

    async def deliver_generation(self, interaction: discord.Interaction, result: Generation, header: str) -> None:
        channels = await self.allowed_channels(interaction)
        if any(r.channel_id not in channels for r in result.sources):
            raise ValueError("처리 중 채널 접근 권한이 바뀌어 결과를 폐기했습니다.")
        if not await asyncio.to_thread(self.store.unchanged, result.sources):
            raise ValueError("처리 중 원문 수정·삭제 또는 수집 제외가 발생해 결과를 폐기했습니다. 다시 실행하세요.")
        if result.omitted:
            header += f"\n입력 한도로 오래된 메시지 {result.omitted}개를 제외한 부분 요약/답변입니다."
        await self.send(interaction, header.strip() + "\n\n" + result.text if header.strip() else result.text)

    def register_commands(self) -> None:
        @self.tree.command(name="search", description="권한이 있는 채널의 인덱스에서 키워드를 검색합니다")
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 5, key=lambda i: (i.guild_id, i.user.id))
        @app_commands.describe(query="예: 배포 일정. 여러 단어는 모두 포함 조건", days="최근 N일; 보관기간 이내", channel="생략하면 읽을 수 있는 모든 인덱싱 대상 채널")
        async def search(interaction: discord.Interaction, query: str,
                         days: app_commands.Range[int, 1, 365] = 30,
                         channel: discord.TextChannel | None = None) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            match_query(query)
            channels = await self.allowed_channels(interaction, channel)
            since = (datetime.now(UTC) - timedelta(days=min(days, self.config.retention_days))).timestamp()
            candidates = await asyncio.to_thread(self.store.search, self.config.guild_id, channels.keys(), query, since, 20)
            current = await asyncio.wait_for(self.verify_records(candidates, channels), timeout=150)
            # An edited hit must still match the query after revalidation.
            refreshed = await asyncio.to_thread(self.store.search, self.config.guild_id, channels.keys(), query, since, 100)
            matches = {r.message_id for r in refreshed}
            channels = await self.allowed_channels(interaction, channel)
            hits = [r for r in current if r.message_id in matches and r.channel_id in channels][:8]
            if not hits:
                await self.send(interaction, "검색 결과가 없습니다. 키워드를 줄이거나 /index로 과거 기록을 채우세요. 인덱스 범위 밖의 대화까지 없다는 뜻은 아닙니다.")
                return
            if not await asyncio.to_thread(self.store.unchanged, hits):
                raise ValueError("처리 중 원문이 바뀌었습니다. 검색을 다시 실행하세요.")
            lines = [f"**검색 결과 {len(hits)}개** · 보관기간 내 키워드 검색"]
            for r in hits:
                snippet = clean(r.content.replace("\n", " ")[:240])
                lines.append(f"\n**#{clean(channels[r.channel_id].name)}** · {clean(r.author_name)} · <t:{int(r.created_at)}:f>\n{snippet}\n[원문 보기]({r.url})")
            await self.send(interaction, "\n".join(lines))

        @self.tree.command(name="summary", description="선택한 채널의 최근 대화를 원문 근거와 함께 요약합니다")
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 60, key=lambda i: (i.guild_id, i.user.id))
        @app_commands.describe(hours="최근 N시간; 보관기간 이내", channel="생략하면 현재 일반 텍스트 채널")
        async def summary(interaction: discord.Interaction, hours: app_commands.Range[int, 1, 8760] = 24,
                          channel: discord.TextChannel | None = None) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            self.llm.require_enabled()
            if self.ai_lock.locked():
                raise ValueError("다른 AI 요청을 처리 중입니다. 완료 후 다시 실행하세요.")
            async with self.ai_lock:
                selected = channel or interaction.channel
                if not isinstance(selected, discord.TextChannel):
                    raise ValueError("일반 텍스트 채널을 선택하세요. 스레드와 DM은 지원하지 않습니다.")
                channels = await self.allowed_channels(interaction, selected)
                target = channels[selected.id]
                actual_hours = min(hours, self.config.retention_days * 24)
                since = datetime.now(UTC) - timedelta(hours=actual_hours)
                records, _ = await self.scan_window(target, since)

                async def guard_sources() -> None:
                    await self.allowed_channels(interaction, target)
                    if not await asyncio.to_thread(self.store.unchanged, records):
                        raise ValueError("원문이 변경되었습니다. 다시 실행하세요.")

                await guard_sources()
                result = await asyncio.wait_for(self.llm.summarize(records, before_call=guard_sources), timeout=480)
                header = f"**💬 #{clean(target.name)} 대화 요약**"
                await self.deliver_generation(interaction, result, header)

        @self.tree.command(name="ask", description="질문에 답하고 필요한 대화 근거는 자동으로 검색합니다")
        @app_commands.guild_only()
        @app_commands.checks.cooldown(1, 60, key=lambda i: (i.guild_id, i.user.id))
        @app_commands.describe(question="질문이나 요청을 입력하세요", query="선택 사항: 직접 지정할 검색어. 생략하면 질문에서 자동 추출", days="최근 N일", channel="생략하면 읽을 수 있는 인덱싱 대상 채널 전체")
        async def ask(interaction: discord.Interaction, question: str, query: str | None = None,
                      days: app_commands.Range[int, 1, 365] = 30,
                      channel: discord.TextChannel | None = None) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            self.llm.require_enabled()
            if query is not None:
                match_query(query)
            if not question.strip() or len(question) > 1000:
                raise ValueError("질문은 1~1000자로 입력하세요.")
            if self.ai_lock.locked():
                raise ValueError("다른 AI 요청을 처리 중입니다. 완료 후 다시 실행하세요.")
            async with self.ai_lock:
                channels = await self.allowed_channels(interaction, channel)
                keywords = [query] if query is not None else await asyncio.wait_for(
                    self.llm.question_keywords(question, []), timeout=60)
                since = (datetime.now(UTC) - timedelta(days=min(days, self.config.retention_days))).timestamp()
                hits: dict[int, Record] = {}
                for keyword in keywords:
                    found = await asyncio.to_thread(self.store.search, self.config.guild_id, channels.keys(), keyword, since, 6)
                    hits.update((r.message_id, r) for r in found)
                evidence = dict(hits)
                for hit in hits.values():
                    for record in await asyncio.to_thread(self.store.context, hit, since):
                        evidence[record.message_id] = record
                records = await asyncio.wait_for(self.verify_records(list(evidence.values())[:60], channels), timeout=180)
                allowed = await self.allowed_channels(interaction, channel)
                records = [r for r in records if r.channel_id in allowed]
                if not await asyncio.to_thread(self.store.unchanged, records):
                    raise ValueError("원문이 변경되었습니다. 다시 실행하세요.")
                result = await asyncio.wait_for(self.llm.thread_answer(question, records, []), timeout=100)
                await self.deliver_generation(interaction, result, "")

        @self.tree.command(name="index", description="관리자: 과거 대화를 한 페이지씩 인덱싱합니다")
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_guild=True)
        @app_commands.checks.has_permissions(manage_guild=True)
        @app_commands.describe(channel="허용 목록에 설정된 일반 텍스트 채널", days="최근 N일; 보관기간 이내", limit="읽을 원문 수, 최대 1000", before="이전 결과의 next_before 값을 넣으면 더 오래된 기록 처리")
        async def index(interaction: discord.Interaction, channel: discord.TextChannel,
                        days: app_commands.Range[int, 1, 365] = 30,
                        limit: app_commands.Range[int, 100, 1000] = 1000,
                        before: str | None = None) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            channels = await self.allowed_channels(interaction, channel)
            cursor = None
            if before:
                if not before.isascii() or not before.isdigit() or not 0 < int(before) < 2**63:
                    raise ValueError("before에는 반환된 숫자 메시지 ID를 입력하세요.")
                cursor = int(before)
                if cursor > discord.utils.time_snowflake(datetime.now(UTC)):
                    raise ValueError("미래 메시지 ID는 커서로 사용할 수 없습니다.")
            actual_days = min(days, self.config.retention_days)
            since = datetime.now(UTC) - timedelta(days=actual_days)
            if cursor and cursor <= discord.utils.time_snowflake(since, high=True):
                raise ValueError("커서가 보관/조회 기간 밖입니다. days 설정을 확인하세요.")
            records, more, next_before, read_count = await asyncio.wait_for(self.scan(channels[channel.id], since, limit, cursor), timeout=600)
            text = f"원문 {read_count}개 확인, 수집 대상 텍스트 {len(records)}개 반영. 최근 {actual_days}일 범위입니다."
            if more:
                text += f"\n이 페이지보다 오래된 기록이 남았습니다. 같은 명령의 before에 `{next_before}`를 넣어 계속하세요.\nnext_before: `{next_before}`"
            else:
                text += "\n이번 조회 구간의 끝에 도달했습니다. 커서를 넣었다면 더 최신 구간의 완전성은 별도로 확인해야 합니다."
            await self.send(interaction, text)

        @self.tree.command(name="status", description="읽을 수 있는 채널별 저장 건수와 보관 설정을 확인합니다")
        @app_commands.guild_only()
        async def status(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            channels = await self.allowed_channels(interaction)
            stats = await asyncio.to_thread(self.store.stats, self.config.guild_id, channels.keys())
            lines = [f"**인덱스 상태** · 보관 {self.config.retention_days}일 · AI {'활성' if self.llm.client else '비활성'}",
                     "범위는 저장된 가장 오래된/최신 시각이며, 중간 누락이 없다는 증명은 아닙니다."]
            for row in stats:
                lines.append(f"#{clean(channels[row['channel_id']].name)}: {row['messages']}개 · <t:{int(row['oldest'])}:f> ~ <t:{int(row['newest'])}:f>")
            if not stats:
                lines.append("저장된 기록이 없습니다. /index를 실행하거나 새 메시지를 보내세요.")
            await self.send(interaction, "\n".join(lines))

        @self.tree.command(name="privacy", description="본인의 기록 수집 제외/재허용 또는 데이터 처리 안내")
        @app_commands.guild_only()
        @app_commands.describe(action="info: 안내, optout: 기존 저장본 삭제·수집 제외, optin: 다시 허용")
        async def privacy(interaction: discord.Interaction, action: Literal["info", "optout", "optin"] = "info") -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if interaction.guild_id != self.config.guild_id:
                raise ValueError("설정된 서버에서만 사용할 수 있습니다.")
            if action == "info":
                await self.send(interaction,
                    f"허용된 텍스트 채널의 본문·표시 이름·작성 시각·메시지/작성자 ID를 로컬 DB에 최대 {self.config.retention_days}일 저장합니다(정리 주기 15분). "
                    "첨부파일 내용·DM·스레드는 인덱싱하지 않습니다. AI 기능 활성 시 요약/답변에 필요한 원문 일부를 Google Gemini API로 전송합니다. "
                    "슬래시 명령 결과는 요청자에게만 표시합니다. 지정된 질문 채널에서는 공개 스레드로 답하고, 후속 대화를 위해 원 질문과 최근 대화 최대 12개(각 1000자)를 읽어 API로 전송합니다. "
                    "질문·스레드 대화는 별도 DB에 저장하지 않습니다. 수집 제외한 사용자의 자동 질문은 처리하지 않습니다. "
                    "`/privacy action:optout`으로 본인 저장본을 삭제하고 이후 수집을 중지할 수 있습니다. "
                    "원문 Discord 메시지는 삭제하지 않으며, 타인 인용·이미 전달된 결과·외부 제공자 로그·운영자 백업까지 소급 삭제하지는 못합니다.")
            else:
                count = await asyncio.to_thread(self.store.optout, self.config.guild_id, interaction.user.id, action == "optout")
                if action == "optout":
                    await self.send(interaction, f"본인 기록 {count}개를 활성 DB와 검색 인덱스에서 삭제하고 수집 제외를 등록했습니다.")
                else:
                    await self.send(interaction, "본인 메시지 수집을 다시 허용했습니다. 정기 동기화 또는 /index로 보관기간 내 과거 메시지도 다시 수집될 수 있습니다.")


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Third-party debug output can include request metadata; keep it at WARNING.
    for name in ("openai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    config = Config.load()
    bot = ChatBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
