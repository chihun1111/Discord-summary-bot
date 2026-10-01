from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from chatbot.bot import ChatBot
from chatbot.config import Config
from chatbot.llm import Generation


class SyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bot = ChatBot(Config("dummy", 1, frozenset({10}),
            Path(self.tmp.name) / "test.db", 30, "Asia/Seoul", "", "test-model",
            False, 50, 3))
        self.now = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=900))
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id, self.channel.guild, self.channel.name = 10, self.guild, "일반"
        self.channel.is_nsfw.return_value = False
        self.channel.permissions_for.return_value = discord.Permissions.all()
        self.bot.fetch_channel = AsyncMock(return_value=self.channel)
        self.messages = [self.message(hours=i * 5) for i in range(11)]
        self.history_calls = []
        self.fail_at = None

        async def history(**kwargs):
            self.history_calls.append(kwargs)
            if len(self.history_calls) == self.fail_at:
                raise ConnectionError("history unavailable")
            candidates = [message for message in self.messages
                          if message.created_at > kwargs["after"]
                          and message.id < kwargs["before"].id]
            for message in sorted(candidates, key=lambda message: message.id, reverse=True)[:kwargs["limit"]]:
                yield message

        self.channel.history = history

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def message(self, hours, content="배포 논의", author=42):
        created = self.now - timedelta(hours=hours)
        message = MagicMock(spec=discord.Message)
        message.id = discord.utils.time_snowflake(created)
        message.guild, message.channel = self.guild, self.channel
        message.author = SimpleNamespace(id=author, bot=False, display_name="테스트")
        message.content, message.webhook_id = content, None
        message.created_at, message.edited_at, message.reference = created, None, None
        return message

    def stored_ids(self):
        with self.bot.store.connection() as conn:
            return {row[0] for row in conn.execute("SELECT message_id FROM messages")}

    def sync_state(self):
        with self.bot.store.connection() as conn:
            return dict(conn.execute("SELECT * FROM sync_state").fetchone())

    async def test_periodic_sync_fetches_every_page_in_rolling_three_days(self):
        old = self.message(hours=96)
        self.messages.append(old)
        await self.bot.reconcile_recent()
        self.assertEqual(len(self.history_calls), 4)
        self.assertEqual(self.stored_ids(), {message.id for message in self.messages if message is not old})
        self.assertTrue(all(call["limit"] == 4 for call in self.history_calls))
        self.assertEqual(len({call["after"] for call in self.history_calls}), 1)
        self.assertAlmostEqual((datetime.now(timezone.utc) - self.history_calls[0]["after"]).total_seconds(),
                               3 * 86400, delta=5)
        self.assertEqual(self.sync_state()["truncated"], 0)

    async def test_offline_edits_deletes_and_empty_bodies_on_older_pages(self):
        self.bot.store.upsert([self.bot.make_record(message) for message in self.messages])
        edited, deleted, emptied = self.messages[6:9]
        edited.content, edited.edited_at = "수정된 배포 일정", self.now
        emptied.content = ""
        self.messages.remove(deleted)
        outside = self.bot.make_record(self.message(hours=96))
        self.bot.store.upsert([outside])
        await self.bot.reconcile_recent()
        self.assertNotIn(deleted.id, self.stored_ids())
        self.assertNotIn(emptied.id, self.stored_ids())
        self.assertIn(outside.message_id, self.stored_ids())
        self.assertTrue(self.bot.store.unchanged([self.bot.make_record(edited)]))

    async def test_failed_page_does_not_complete_and_next_cycle_retries(self):
        self.fail_at = 2
        with self.assertLogs("chat-index", level="WARNING"):
            await self.bot.reconcile_recent()
        self.assertEqual(len(self.stored_ids()), 3)
        self.assertEqual(self.sync_state()["truncated"], 1)
        self.fail_at = None
        await self.bot.reconcile_recent()
        self.assertEqual(self.stored_ids(), {message.id for message in self.messages})
        self.assertEqual(self.sync_state()["truncated"], 0)
        self.assertGreater(self.history_calls[2]["before"].id, self.messages[0].id)

    async def test_short_retention_and_permissions_are_respected(self):
        self.bot.config = replace(self.bot.config, retention_days=1)
        await self.bot.reconcile_recent()
        self.assertEqual(self.stored_ids(), {message.id for message in self.messages
                                            if message.created_at > datetime.now(timezone.utc) - timedelta(days=1)})
        self.history_calls.clear()
        self.channel.permissions_for.return_value = discord.Permissions.none()
        await self.bot.reconcile_recent()
        self.assertEqual(self.history_calls, [])

    async def test_optout_and_tombstones_remain_excluded(self):
        opted = self.message(hours=57, author=43)
        self.messages.append(opted)
        self.bot.store.optout(1, 43, True)
        deleted = self.messages[7]
        self.bot.store.delete([deleted.id])
        records, count = await self.bot.scan_window(self.channel, self.now - timedelta(days=3))
        self.assertEqual(count, 12)
        self.assertNotIn(opted.id, self.stored_ids())
        self.assertNotIn(deleted.id, self.stored_ids())
        self.assertEqual({record.message_id for record in records}, self.stored_ids())

    async def test_optout_during_later_page_removes_earlier_returned_records(self):
        original_scan = self.bot.scan
        pages = 0

        async def scan(*args):
            nonlocal pages
            result = await original_scan(*args)
            pages += 1
            if pages == 2:
                self.bot.store.optout(1, 42, True)
            return result

        self.bot.scan = scan
        records, count = await self.bot.scan_window(self.channel, self.now - timedelta(days=3))
        self.assertEqual(count, 11)
        self.assertEqual(records, [])
        self.assertEqual(self.stored_ids(), set())

    async def test_summary_scans_all_pages_and_checks_sources_before_call(self):
        interaction = SimpleNamespace(channel=self.channel, response=SimpleNamespace(defer=AsyncMock()))
        self.bot.allowed_channels = AsyncMock(return_value={10: self.channel})
        self.bot.deliver_generation = AsyncMock()

        async def summarize(records, *, before_call):
            await before_call()
            self.assertEqual(len(records), 11)
            return Generation("요약", records, 0)

        self.bot.llm.require_enabled = MagicMock()
        self.bot.llm.summarize = AsyncMock(side_effect=summarize)
        await self.bot.tree.get_command("summary").callback(interaction, hours=72)
        self.assertEqual(len(self.history_calls), 4)
        self.assertEqual(self.bot.deliver_generation.call_args.args[2], "**💬 #일반 대화 요약**")
        self.assertEqual(self.bot.allowed_channels.await_count, 3)

    async def test_summary_stops_if_source_deleted_before_call(self):
        interaction = SimpleNamespace(channel=self.channel, response=SimpleNamespace(defer=AsyncMock()))
        self.bot.allowed_channels = AsyncMock(return_value={10: self.channel})
        self.bot.deliver_generation = AsyncMock()

        async def summarize(records, *, before_call):
            self.bot.store.delete([records[0].message_id])
            await before_call()
            self.fail("Deleted records must not be sent to the model")

        self.bot.llm.require_enabled = MagicMock()
        self.bot.llm.summarize = AsyncMock(side_effect=summarize)
        with self.assertRaisesRegex(ValueError, "원문이 변경"):
            await self.bot.tree.get_command("summary").callback(interaction, hours=72)
        self.bot.deliver_generation.assert_not_awaited()

    def test_ask_only_requires_question(self):
        parameters = self.bot.tree.get_command("ask").parameters
        self.assertEqual([p.name for p in parameters if p.required], ["question"])

    async def run_ask(self, question, **kwargs):
        interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()))
        self.bot.allowed_channels = AsyncMock(return_value={10: self.channel})
        self.bot.deliver_generation = AsyncMock()
        self.bot.llm.require_enabled = MagicMock()
        self.bot.llm.thread_answer = AsyncMock(return_value=Generation("답변", [], 0))
        self.bot.verify_records = AsyncMock(side_effect=lambda records, channels: records)
        await self.bot.tree.get_command("ask").callback(interaction, question, **kwargs)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_ask_auto_searches_question_and_deduplicates_hits(self):
        record = self.bot.make_record(self.messages[0])
        self.bot.store.upsert([record])
        self.bot.llm.question_keywords = AsyncMock(return_value=["배포", "논의"])
        await self.run_ask("우리 배포 논의 내용 알려줘", days=1, channel=self.channel)
        self.bot.llm.question_keywords.assert_awaited_once_with("우리 배포 논의 내용 알려줘", [])
        self.assertEqual(self.bot.llm.thread_answer.call_args.args[1], [record])
        self.assertEqual(self.bot.allowed_channels.call_args.args[1], self.channel)
        self.assertEqual(self.bot.deliver_generation.call_args.args[2], "")

    async def test_ask_general_question_needs_no_query_or_evidence(self):
        self.bot.llm.question_keywords = AsyncMock(return_value=[])
        self.bot.store.search = MagicMock()
        await self.run_ask("strlen 설명해줘")
        self.bot.store.search.assert_not_called()
        self.bot.llm.thread_answer.assert_awaited_once_with("strlen 설명해줘", [], [])

    async def test_ask_season_topic_includes_answer_without_repeating_keyword(self):
        topic = replace(self.bot.make_record(self.messages[0]), content="다들 무슨계절인가욤",
                        created_at=self.now.timestamp()-25*60)
        correction = replace(topic, message_id=topic.message_id+1, content="저 봄이에요",
                             created_at=topic.created_at+21*60)
        self.bot.store.upsert([topic, correction])
        self.bot.llm.question_keywords = AsyncMock(return_value=["계절"])
        await self.run_ask("다들 계절이 뭔지 알려줘")
        self.assertEqual({r.message_id for r in self.bot.llm.thread_answer.call_args.args[1]},
                         {topic.message_id, correction.message_id})

    async def test_ask_explicit_query_skips_keyword_call(self):
        self.bot.llm.question_keywords = AsyncMock()
        self.bot.store.search = MagicMock(return_value=[])
        await self.run_ask("배포 내용 알려줘", query="배포", days=1)
        self.bot.llm.question_keywords.assert_not_awaited()
        args = self.bot.store.search.call_args.args
        self.assertEqual(args[2], "배포")
        self.assertAlmostEqual(datetime.now(timezone.utc).timestamp() - args[3], 86400, delta=5)
        self.bot.llm.thread_answer.assert_awaited_once_with("배포 내용 알려줘", [], [])

    async def test_ask_removed_source_not_sent_to_model(self):
        record = self.bot.make_record(self.messages[0])
        self.bot.store.upsert([record])
        self.bot.llm.question_keywords = AsyncMock(return_value=["배포"])
        self.bot.store.unchanged = MagicMock(return_value=False)
        with self.assertRaisesRegex(ValueError, "원문이 변경"):
            await self.run_ask("배포 내용 알려줘")
        self.bot.llm.thread_answer.assert_not_awaited()

    async def test_generation_without_header_sends_only_answer(self):
        self.bot.allowed_channels = AsyncMock(return_value={10: self.channel})
        self.bot.send = AsyncMock()
        interaction = object()
        await self.bot.deliver_generation(interaction, Generation("답변", [], 0), "")
        self.bot.send.assert_awaited_once_with(interaction, "답변")
