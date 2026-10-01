"""Question-channel messages and public reply threads; never index dialogue."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import logging
import re
import time

import discord

from .store import Record
from .periods import TimeWindow, resolve_period
from .text import safe_chunks

log = logging.getLogger("chat-index")
UTC = timezone.utc


def thread_title(question: str, channels: dict[int, discord.TextChannel]) -> str:
    """Readable first-question title, without another paid model call."""
    title = re.sub(r"<#([0-9]{1,19})>",
                   lambda match: channels[int(match[1])].name + " · "
                   if int(match[1]) in channels else "채널 · ", question)
    title = re.sub(r"<@!?[0-9]+>", "사용자", title)
    title = re.sub(r"<@&[0-9]+>", "역할", title)
    title = re.sub(r"https?://\S+", "링크", title)
    title = re.sub(r"[`*_~|\x00-\x1f\x7f]", " ", title)
    title = " ".join(title.split()).strip(" ·#")
    title = re.sub(r"대화(?:를|는)?\s+(요약|정리)\s*해\s*(?:줘|주세요)[.!?。]*$",
                   r"대화 \1", title)
    title = title or "새 대화"
    # At most 50 Unicode characters also fits Discord's 100 UTF-16 units.
    return title if len(title) <= 50 else title[:49].rstrip() + "…"


def summary_request(text: str, history: list[dict], retention_days: int,
                    timezone_name: str = "Asia/Seoul", now: datetime | None = None) -> tuple[list[int], TimeWindow | None]:
    """Inherit channel and time scope from user turns; resolve dates locally."""
    turns = [item["content"] for item in history if item.get("role") == "user"] + [text]
    ids = []
    for turn in reversed(turns):
        found = list(dict.fromkeys(int(value) for value in re.findall(r"<#([0-9]{1,19})>", turn)))
        if found:
            ids = found
            break
    if len(ids) > 3:
        raise ValueError("한 번에 요약할 채널은 최대 3개까지 태그해 주세요.")
    window = resolve_period(text, history, retention_days, timezone_name, now or datetime.now(UTC)) if ids else None
    return ids, window


def audience_signature(channel: discord.TextChannel) -> dict:
    """Conservative equality of view/history overwrites for every role/member.

    Guild base roles are shared. Other permission bits cannot grant access to
    source text. Unknown overwrite targets fail closed via the raw pairs too.
    """
    mask = discord.Permissions(view_channel=True, read_message_history=True).value
    # Use raw overwrites: channel.overwrites omits roles/users absent from cache.
    return {(overwrite.type, overwrite.id): (overwrite.allow & mask, overwrite.deny & mask)
            for overwrite in channel._overwrites
            if (overwrite.allow | overwrite.deny) & mask}


class ThreadQA:
    def __init__(self, bot):
        self.bot = bot
        self.seen: OrderedDict[int, None] = OrderedDict()
        self.cooldowns: OrderedDict[int, float] = OrderedDict()

    def accepts(self, message: discord.Message) -> bool:
        config = self.bot.config
        if (not config.question_channel_ids or self.bot.user is None or message.guild is None
                or message.guild.id != config.guild_id or message.author.bot
                or message.webhook_id is not None or not message.content.strip()
                or message.type not in (discord.MessageType.default, discord.MessageType.reply)):
            return False
        channel = message.channel
        if isinstance(channel, discord.TextChannel):
            return channel.id in config.question_channel_ids and not channel.is_nsfw()
        return (isinstance(channel, discord.Thread)
                and channel.parent_id in config.question_channel_ids
                and channel.owner_id == self.bot.user.id
                and channel.type == discord.ChannelType.public_thread
                and not channel.archived and not channel.locked)

    async def eligible(self, messages: list[discord.Message]) -> list[discord.Message]:
        records = [Record(m.id, m.guild.id, m.channel.id, m.author.id, "", "", 0, 0) for m in messages]
        allowed = {r.message_id for r in await asyncio.to_thread(self.bot.store.filter_eligible, records)}
        return [m for m in messages if m.id in allowed]

    async def scope(self, message: discord.Message, parent_id: int, *, creating: bool = False):
        """Fresh requester/bot ACL and public audience check before using sources."""
        guild = message.guild
        member, bot_member, channels = await asyncio.gather(
            guild.fetch_member(message.author.id), guild.fetch_member(self.bot.user.id), guild.fetch_channels())
        parent = next((c for c in channels if isinstance(c, discord.TextChannel) and c.id == parent_id), None)
        if parent is None or parent.is_nsfw() or parent.id not in self.bot.config.question_channel_ids:
            raise ValueError("현재 사용할 수 있는 질문 채널이 아닙니다.")
        user_perm, bot_perm = parent.permissions_for(member), parent.permissions_for(bot_member)
        if not (user_perm.view_channel and user_perm.read_message_history
                and bot_perm.view_channel and bot_perm.read_message_history and bot_perm.send_messages_in_threads):
            raise ValueError("질문 채널 보기·기록 읽기·스레드 답변 권한을 확인해 주세요.")
        if creating and not bot_perm.create_public_threads:
            raise ValueError("봇에 공개 스레드 만들기 권한이 필요합니다.")
        audience = audience_signature(parent)
        result = {}
        for channel in channels:
            if (not isinstance(channel, discord.TextChannel) or channel.is_nsfw()
                    or channel.id not in self.bot.config.channel_ids
                    or audience_signature(channel) != audience):
                continue
            up, bp = channel.permissions_for(member), channel.permissions_for(bot_member)
            if up.view_channel and up.read_message_history and bp.view_channel and bp.read_message_history:
                result[channel.id] = channel
        return parent, result

    async def history(self, thread: discord.Thread, current: discord.Message, parent: discord.TextChannel) -> list[dict]:
        root = await parent.fetch_message(thread.id)
        previous = [m async for m in thread.history(limit=12, before=current, oldest_first=False)]
        cutoff = datetime.now(UTC) - timedelta(days=self.bot.config.retention_days)
        messages = sorted({m.id: m for m in [root, *previous]
                           if m.id != current.id and m.created_at >= cutoff and m.content.strip()
                           and m.webhook_id is None
                           and (not m.author.bot or m.author.id == self.bot.user.id)
                           and m.type in (discord.MessageType.default, discord.MessageType.reply)}.values(), key=lambda m: m.id)
        messages = await self.eligible(messages)
        # Preserve the original topic, then the newest turns, within a fixed budget.
        messages = ([messages[0]] + messages[-11:]) if len(messages) > 12 else messages
        return [{"role": "assistant" if m.author.id == self.bot.user.id else "user", "content": m.content[:1000]}
                for m in messages]

    async def retrieve(self, keywords: list[str], channels: dict) -> list[Record]:
        since = (datetime.now(UTC) - timedelta(days=self.bot.config.retention_days)).timestamp()
        hits = {}
        for word in keywords:
            for record in await asyncio.to_thread(self.bot.store.search, self.bot.config.guild_id, channels.keys(), word, since, 3):
                hits[record.message_id] = record
        evidence = dict(hits)
        for hit in hits.values():
            for record in await asyncio.to_thread(self.bot.store.context, hit, since, radius=30, window_seconds=1800):
                evidence[record.message_id] = record
        return await asyncio.wait_for(self.bot.verify_records(list(evidence.values())[:120], channels), timeout=180)

    async def scan_mentions(self, ids: list[int], window: TimeWindow, channels: dict) -> tuple[list[Record], str]:
        if any(channel_id not in self.bot.config.channel_ids for channel_id in ids):
            raise ValueError("태그한 채널을 먼저 관리 웹의 수집 채널에 추가해 주세요. 일반 텍스트 채널만 요약할 수 있습니다.")
        if any(channel_id not in channels for channel_id in ids):
            raise ValueError("태그한 채널을 이 스레드에 공개할 수 없습니다. 읽기 권한과 질문 채널의 열람 권한 설정을 확인해 주세요.")
        records = []
        for channel_id in ids:
            page, _ = await self.bot.scan_window(channels[channel_id], window.start, until=window.end)
            records.extend(r for r in page if window.start.timestamp() <= r.created_at < window.end.timestamp())
        if not records:
            raise ValueError(f"태그한 채널의 {window.label} 기간에서 요약할 수 있는 대화가 확인되지 않았습니다.")
        names = ", ".join(f"<#{channel_id}>" for channel_id in ids)
        header = f"**💬 {names} 대화 요약**"
        return records, header

    async def reply(self, target, text: str) -> None:
        for chunk in safe_chunks(text):
            await target.send(chunk, suppress_embeds=True, allowed_mentions=discord.AllowedMentions.none())

    async def handle(self, message: discord.Message) -> None:
        if not self.accepts(message) or message.id in self.seen:
            return
        self.seen[message.id] = None
        if len(self.seen) > 2048:
            self.seen.popitem(last=False)
        creating = isinstance(message.channel, discord.TextChannel)
        target = None if creating else message.channel
        try:
            if not await self.eligible([message]):
                return  # Opted-out users' dialogue is not sent to the provider.
            if len(message.content) > 1000:
                raise ValueError("질문은 1~1000자로 입력하세요.")
            self.bot.llm.require_enabled()
            now = time.monotonic()
            if self.cooldowns.get(message.author.id, 0) > now:
                raise ValueError("질문을 너무 빠르게 보내고 있습니다. 10초 후 다시 질문해 주세요.")
            if self.bot.ai_lock.locked():
                raise ValueError("다른 AI 요청을 처리 중입니다. 잠시 후 이 스레드에서 다시 질문해 주세요.")
            self.cooldowns[message.author.id] = now + 10
            self.cooldowns.move_to_end(message.author.id)
            if len(self.cooldowns) > 4096:
                self.cooldowns.popitem(last=False)
            async with self.bot.ai_lock:
                parent_id = message.channel.id if creating else message.channel.parent_id
                parent, channels = await self.scope(message, parent_id, creating=creating)
                if creating:
                    # Claim cooldown/AI work before creating any Discord resource.
                    message = await parent.fetch_message(message.id)
                    target = await message.create_thread(name=thread_title(message.content, channels),
                                                         auto_archive_duration=1440)
                history = [] if creating else await self.history(target, message, parent)
                if not await self.eligible([message]):
                    return
                request_now = datetime.now(UTC)
                mentioned, window = summary_request(message.content, history, self.bot.config.retention_days,
                                                    self.bot.config.timezone, request_now)
                summary_header = None
                if mentioned:
                    records, summary_header = await self.scan_mentions(mentioned, window, channels)
                else:
                    keywords = await asyncio.wait_for(self.bot.llm.question_keywords(message.content, history), timeout=60)
                    if keywords and not channels:
                        raise ValueError("질문 채널과 열람 권한 설정이 같은 수집 채널이 없습니다. 채널 보기·기록 읽기 권한을 맞춰 주세요.")
                    records = await self.retrieve(keywords, channels)
                _, current_channels = await self.scope(message, parent_id)
                if any(r.channel_id not in current_channels for r in records) or not await asyncio.to_thread(self.bot.store.unchanged, records):
                    raise ValueError("원문 또는 채널 권한이 변경되었습니다. 다시 질문해 주세요.")
                # A /privacy optout can arrive during keyword extraction/retrieval.
                # Re-read dialogue before the second outbound provider request.
                history = [] if creating else await self.history(target, message, parent)
                if not await self.eligible([message]):
                    return
                if (mentioned, window) != summary_request(message.content, history, self.bot.config.retention_days,
                                                          self.bot.config.timezone, request_now):
                    raise ValueError("처리 중 대화의 대상 채널이나 기간이 바뀌었습니다. 다시 질문해 주세요.")
                if mentioned:
                    async def before_summary_call():
                        # Each map/reduce call is another disclosure boundary.
                        if not await self.eligible([message]):
                            raise ValueError("수집 제외 설정이 변경되어 요약을 중단했습니다.")
                        updated = [] if creating else await self.history(target, message, parent)
                        if updated != history:
                            raise ValueError("이전 대화가 수정·삭제되거나 수집 제외되어 요약을 중단했습니다. 다시 요청해 주세요.")
                        _, allowed = await self.scope(message, parent_id)
                        if any(r.channel_id not in allowed for r in records) or not await asyncio.to_thread(self.bot.store.unchanged, records):
                            raise ValueError("원문 또는 채널 권한이 변경되어 요약을 중단했습니다.")
                        latest = await message.channel.fetch_message(message.id)
                        if latest.content != message.content:
                            raise ValueError("질문이 수정되어 요약을 중단했습니다. 다시 요청해 주세요.")
                    result = await asyncio.wait_for(self.bot.llm.summarize(records,
                                                                          instruction=message.content, history=history,
                                                                          before_call=before_summary_call,
                                                                          retrieval_scope={
                                                                              "now": request_now.isoformat(),
                                                                              "timezone": self.bot.config.timezone,
                                                                              "start": window.start.isoformat(),
                                                                              "end_exclusive": window.end.isoformat(),
                                                                              "model_resolves_period": window.model_resolves_period,
                                                                          }), timeout=480)
                else:
                    result = await asyncio.wait_for(self.bot.llm.thread_answer(message.content, records, history), timeout=100)
                _, current_channels = await self.scope(message, parent_id)
                if any(r.channel_id not in current_channels for r in result.sources):
                    raise ValueError("처리 중 채널 권한이 변경되어 답변을 취소했습니다.")
                if not await asyncio.to_thread(self.bot.store.unchanged, result.sources):
                    raise ValueError("처리 중 원문이 수정·삭제되거나 수집 제외되어 답변을 취소했습니다.")
                if not await self.eligible([message]):
                    return
                current = await message.channel.fetch_message(message.id)
                if current.content != message.content:
                    raise ValueError("처리 중 질문이 수정되었습니다. 다시 질문해 주세요.")
                if summary_header:
                    answer = summary_header + "\n" + result.text
                    if result.omitted:
                        answer += "\n\n※ 입력 한도로 일부 대화만 반영한 요약입니다."
                    await self.reply(target, answer)
                else:
                    answer = result.text
                    if result.omitted:
                        answer += "\n\n※ 입력 한도로 일부 대화만 반영한 답변입니다."
                    await self.reply(target, answer)
        except (ValueError, discord.HTTPException, TimeoutError) as exc:
            if isinstance(exc, ValueError):
                error = str(exc)
            elif isinstance(exc, TimeoutError):
                error = "답변 제한시간을 넘었습니다. 잠시 후 다시 질문해 주세요."
            else:
                error = "질문 또는 원문에 접근할 수 없습니다. 삭제 여부와 봇의 스레드 권한을 확인해 주세요."
            await self.error(message, target, error)
        except Exception as exc:
            log.error("Command failed: thread question %s", type(exc).__name__)
            await self.error(message, target, "답변을 처리하지 못했습니다. API 설정과 연결 상태를 확인해 주세요.")

    async def error(self, message, target, error: str) -> None:
        try:
            if target is not None:
                await self.reply(target, error)
            else:
                await message.reply(error, mention_author=False, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
        except discord.HTTPException:
            log.warning("Could not deliver thread question error")
