from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

import discord

from chatbot.bot import ChatBot
from chatbot.config import Config
from chatbot.llm import Generation, LLM
from chatbot.thread_qa import audience_signature, summary_request, thread_title


class ThreadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        config = Config("dummy", 1, frozenset({10}), Path(self.tmp.name)/"test.db",
                        30, "Asia/Seoul", "", "test-model", False, 50, 500, frozenset({20}))
        self.bot = ChatBot(config)
        self.bot._connection.user = SimpleNamespace(id=900)
        self.guild = SimpleNamespace(id=1)
        self.perms = discord.Permissions.all()
        self.source = self.channel(10)
        self.parent = self.channel(20)
        self.guild.fetch_channels = AsyncMock(return_value=[self.source, self.parent])
        self.guild.fetch_member = AsyncMock(side_effect=lambda uid:SimpleNamespace(id=uid))
        self.thread = MagicMock(spec=discord.Thread)
        self.thread.id, self.thread.parent_id, self.thread.owner_id = 101, 20, 900
        self.thread.type = discord.ChannelType.public_thread
        self.thread.archived = self.thread.locked = False
        self.thread.guild = self.guild
        self.thread.send = AsyncMock()
        self.messages = {}
        self.previous = []
        async def history(**kwargs):
            for msg in reversed(self.previous[-kwargs["limit"]:]):
                yield msg
        self.thread.history = history
        self.thread.fetch_message = AsyncMock(side_effect=lambda mid:self.messages[mid])
        self.root = self.message(101, self.parent, "배포가 언제야?")
        self.root.create_thread = AsyncMock(return_value=self.thread)
        self.parent.fetch_message = AsyncMock(side_effect=lambda mid:self.messages[mid])
        self.original = self.message(50, self.source, "배포는 10월 5일입니다.")
        self.source.fetch_message = AsyncMock(side_effect=lambda mid:self.messages[mid])
        self.record = self.bot.make_record(self.original)
        self.bot.store.upsert([self.record])
        self.bot.llm = SimpleNamespace(require_enabled=MagicMock(), close=AsyncMock(),
            question_keywords=AsyncMock(return_value=["배포"]),
            summarize=AsyncMock(return_value=Generation("배포 일정이 결정됐습니다.",[self.record],0)),
            thread_answer=AsyncMock(return_value=Generation("10월 5일입니다.", [self.record], 0)))

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def channel(self, channel_id):
        channel = MagicMock(spec=discord.TextChannel)
        channel.id, channel.guild = channel_id, self.guild
        channel.name = "일반" if channel_id == 10 else "요약봇"
        channel._overwrites = []
        channel.is_nsfw.return_value = False
        channel.permissions_for.side_effect = lambda member:self.perms
        return channel

    def message(self, mid, channel, text, *, author=42, bot=False):
        message = MagicMock(spec=discord.Message)
        message.id, message.guild, message.channel = mid, self.guild, channel
        message.author = SimpleNamespace(id=author, bot=bot, display_name="테스트")
        message.content, message.webhook_id = text, None
        message.type = discord.MessageType.default
        message.created_at = datetime.now(timezone.utc)
        message.edited_at = message.reference = None
        message.reply = AsyncMock()
        self.messages[mid] = message
        return message

    async def test_new_question_creates_thread_and_is_not_indexed(self):
        await self.bot.on_message(self.root)
        self.root.create_thread.assert_awaited_once_with(name="배포가 언제야?", auto_archive_duration=1440)
        self.thread.send.assert_awaited_once()
        self.assertIn("10월 5일", self.thread.send.call_args.args[0])
        self.assertFalse(self.thread.send.call_args.kwargs["allowed_mentions"].everyone)
        self.assertEqual(self.bot.store.search(1,[20],"배포",0), [])
        await self.bot.on_message(self.root)
        self.root.create_thread.assert_awaited_once()

    def test_thread_title_uses_channel_name_and_topic(self):
        self.assertEqual(thread_title("<#10> 최근 1시간 대화를 요약해줘.", {10:self.source}),
                         "일반 · 최근 1시간 대화 요약")
        self.assertEqual(thread_title("**10월 5일 배포**\n담당자는 누구야?", {}),
                         "10월 5일 배포 담당자는 누구야?")

    def test_thread_title_hides_unresolved_ids_and_bounds_length(self):
        self.assertEqual(thread_title("<#999> <@123> <@&456> 질문", {}), "채널 · 사용자 역할 질문")
        self.assertEqual(thread_title("https://example.com/private?q=secret 요약", {}), "링크 요약")
        self.assertEqual(thread_title("**\n**", {}), "새 대화")
        long_title = thread_title("😀" * 100, {})
        self.assertTrue(long_title.endswith("…"))
        self.assertLessEqual(len(long_title.encode("utf-16-le")) // 2, 100)

    async def test_followup_reads_root_and_prior_turn_without_new_thread(self):
        answer = self.message(102, self.thread, "10월 5일입니다.", author=900, bot=True)
        self.previous = [answer]
        followup = self.message(103, self.thread, "그럼 누가 맡았어?")
        await self.bot.on_message(followup)
        self.root.create_thread.assert_not_awaited()
        history = self.bot.llm.thread_answer.call_args.args[2]
        self.assertEqual([h["role"] for h in history], ["user","assistant"])
        self.assertEqual(history[0]["content"], self.root.content)
        self.assertEqual(self.bot.store.search(1,[101],"누가",0), [])

    async def test_bot_webhook_unconfigured_and_foreign_threads_ignored(self):
        for message in [self.message(110,self.parent,"안녕",bot=True), self.message(111,self.source,"일반 대화")]:
            await self.bot.thread_qa.handle(message)
        webhook = self.message(112,self.parent,"질문"); webhook.webhook_id=1
        await self.bot.thread_qa.handle(webhook)
        self.thread.owner_id=999
        await self.bot.thread_qa.handle(self.message(113,self.thread,"질문"))
        self.bot.llm.question_keywords.assert_not_awaited()

    async def test_opted_out_user_not_sent_to_model(self):
        self.bot.store.optout(1,42,True)
        await self.bot.on_message(self.root)
        self.root.create_thread.assert_not_awaited()
        self.bot.llm.question_keywords.assert_not_awaited()

    async def test_audience_difference_blocks_private_source(self):
        self.source._overwrites=[SimpleNamespace(type=0,id=123,allow=0,deny=1024)]
        await self.bot.on_message(self.root)
        self.bot.llm.thread_answer.assert_not_awaited()
        self.assertIn("열람 권한", self.thread.send.call_args.args[0])
        self.assertNotIn("10월 5일", self.thread.send.call_args.args[0])

    async def test_permission_change_during_generation_discards_answer(self):
        async def generate(*args):
            self.source._overwrites=[SimpleNamespace(type=1,id=123,allow=0,deny=1024)]
            return Generation("SECRET-RESULT",[self.record],0)
        self.bot.llm.thread_answer.side_effect=generate
        await self.bot.on_message(self.root)
        self.assertIn("답변을 취소",self.thread.send.call_args.args[0])
        self.assertNotIn("SECRET-RESULT",self.thread.send.call_args.args[0])

    async def test_deleted_source_during_generation_discards_answer(self):
        async def generate(*args):
            self.bot.store.delete([self.record.message_id])
            return Generation("SECRET-RESULT",[self.record],0)
        self.bot.llm.thread_answer.side_effect=generate
        await self.bot.on_message(self.root)
        self.assertIn("답변을 취소",self.thread.send.call_args.args[0])
        self.assertNotIn("SECRET-RESULT",self.thread.send.call_args.args[0])

    async def test_history_excludes_other_bots_webhooks_optouts(self):
        a=self.message(102,self.thread,"타 봇",author=777,bot=True)
        b=self.message(103,self.thread,"제외된 사람",author=88)
        c=self.message(104,self.thread,"웹훅",author=99); c.webhook_id=1
        self.bot.store.optout(1,88,True)
        self.previous=[a,b,c]
        current=self.message(105,self.thread,"그 다음은?")
        history=await self.bot.thread_qa.history(self.thread,current,self.parent)
        self.assertEqual(history,[{"role":"user","content":self.root.content}])

    async def test_busy_and_cooldown_do_not_call_model(self):
        await self.bot.ai_lock.acquire()
        await self.bot.on_message(self.root)
        self.bot.ai_lock.release()
        self.assertIn("다른 AI 요청",self.root.reply.call_args.args[0])
        self.root.create_thread.assert_not_awaited()
        self.bot.llm.question_keywords.assert_not_awaited()
        self.bot.thread_qa.cooldowns[42]=float("inf")
        await self.bot.on_message(self.message(106,self.thread,"다시 질문"))
        self.assertIn("10초",self.thread.send.call_args.args[0])

    async def test_root_cooldown_does_not_create_thread(self):
        self.bot.thread_qa.cooldowns[42]=float("inf")
        await self.bot.on_message(self.root)
        self.root.create_thread.assert_not_awaited()
        self.bot.llm.question_keywords.assert_not_awaited()

    async def test_optout_during_keywords_prevents_second_provider_call(self):
        async def keywords(*args):
            self.bot.store.optout(1,42,True)
            return ["배포"]
        self.bot.llm.question_keywords.side_effect=keywords
        await self.bot.on_message(self.root)
        self.bot.llm.thread_answer.assert_not_awaited()

    async def test_history_optout_during_keywords_removed_before_answer(self):
        previous=self.message(102,self.thread,"이전 대화",author=88)
        self.previous=[previous]
        async def keywords(*args):
            self.bot.store.optout(1,88,True)
            return ["배포"]
        self.bot.llm.question_keywords.side_effect=keywords
        await self.bot.on_message(self.message(103,self.thread,"계속 알려줘"))
        history=self.bot.llm.thread_answer.call_args.args[2]
        self.assertNotIn("이전 대화",str(history))

    async def test_missing_thread_permission_blocks_creation(self):
        self.perms.create_public_threads=False
        await self.bot.on_message(self.root)
        self.root.create_thread.assert_not_awaited()
        self.root.reply.assert_awaited_once()

    async def test_no_sources_can_reply_to_greeting(self):
        self.guild.fetch_channels.return_value=[self.parent]
        self.bot.llm.question_keywords.return_value=[]
        self.bot.llm.thread_answer.return_value=Generation("안녕하세요. 어떤 대화가 궁금하세요?",[],0)
        await self.bot.on_message(self.root)
        self.assertEqual(self.bot.llm.thread_answer.call_args.args[1],[])
        self.assertIn("안녕하세요",self.thread.send.call_args.args[0])

    def test_overwrites_compare_uncached_principals_and_ignore_send_only(self):
        self.source._overwrites=[SimpleNamespace(type=1,id=9999,allow=1024,deny=0)]
        self.assertNotEqual(audience_signature(self.parent),audience_signature(self.source))
        self.parent._overwrites=[SimpleNamespace(type=1,id=9999,allow=1024|2048,deny=0)]
        self.assertEqual(audience_signature(self.parent),audience_signature(self.source))

    async def test_channel_tag_scans_target_and_passes_user_prompt(self):
        self.root.content="<#10> 최근 3일 동안 결정된 내용과 할 일을 정리해줘"
        self.bot.scan_window=AsyncMock(return_value=([self.record],1500))
        await self.bot.on_message(self.root)
        self.bot.llm.question_keywords.assert_not_awaited()
        self.bot.llm.thread_answer.assert_not_awaited()
        self.bot.scan_window.assert_awaited_once()
        self.assertIs(self.bot.scan_window.call_args.args[0],self.source)
        self.assertEqual(self.bot.llm.summarize.call_args.kwargs['instruction'],self.root.content)
        answer=self.thread.send.call_args.args[0]
        self.assertIn("최근 72시간",answer)
        self.assertIn("원문 1500개 확인",answer)
        self.assertNotIn("부분 요약",answer)
        self.assertIn("<#10>",answer)
        self.assertTrue(answer.startswith("**💬 <#10> 대화 요약**\n배포 일정이 결정됐습니다."))
        self.assertGreater(answer.index("최근 72시간"), answer.index("배포 일정이 결정됐습니다."))

    async def test_summary_keeps_input_omission_notice_below_body(self):
        self.root.content="<#10> 요약해줘"
        self.bot.scan_window=AsyncMock(return_value=([self.record],1))
        self.bot.llm.summarize.return_value=Generation("배포 얘기가 오갔음.",[self.record],1)
        await self.bot.on_message(self.root)
        answer=self.thread.send.call_args.args[0]
        self.assertTrue(answer.startswith("**💬 <#10> 대화 요약**\n배포 얘기가 오갔음."))
        self.assertIn("원문 1개 제외한 부분 요약",answer)

    async def test_summary_reports_preprocessing_separately(self):
        self.root.content="<#10> 요약해줘"
        self.bot.scan_window=AsyncMock(return_value=([self.record],20))
        self.bot.llm.summarize.return_value=Generation("배포 얘기가 오갔음.",[self.record],0,5)
        await self.bot.on_message(self.root)
        answer=self.thread.send.call_args.args[0]
        self.assertIn("전처리로 반복·인사 5개 생략",answer)
        self.assertNotIn("입력 한도",answer)

    async def test_followup_inherits_tag_without_querying_other_channels(self):
        self.root.content="<#10> 할 일 위주로 요약해줘"
        self.bot.scan_window=AsyncMock(return_value=([self.record],1))
        await self.bot.on_message(self.message(103,self.thread,"담당자도 같이 적어줘"))
        self.bot.llm.summarize.assert_awaited_once()
        self.bot.llm.question_keywords.assert_not_awaited()
        args=self.bot.llm.summarize.call_args
        self.assertEqual(args.kwargs['instruction'],"담당자도 같이 적어줘")
        self.assertIn("<#10>",args.kwargs['history'][0]['content'])
        self.assertIn("최근 24시간",self.thread.send.call_args.args[0])

    async def test_unknown_or_unreadable_tag_never_falls_back_to_all_sources(self):
        self.root.content="<#999> 요약해줘"
        self.bot.scan_window=AsyncMock()
        await self.bot.on_message(self.root)
        self.bot.scan_window.assert_not_awaited()
        self.bot.llm.summarize.assert_not_awaited()
        self.bot.llm.question_keywords.assert_not_awaited()
        self.assertIn("수집 채널에 추가",self.thread.send.call_args.args[0])

    async def test_private_tag_cannot_be_summarized_publicly(self):
        self.root.content="<#10> 요약해줘"
        self.source._overwrites=[SimpleNamespace(type=0,id=55,allow=0,deny=1024)]
        self.bot.scan_window=AsyncMock()
        await self.bot.on_message(self.root)
        self.bot.scan_window.assert_not_awaited()
        self.bot.llm.summarize.assert_not_awaited()
        self.assertIn("공개할 수 없습니다",self.thread.send.call_args.args[0])

    async def test_empty_tagged_channel_does_not_call_summary(self):
        self.root.content="<#10> 요약해줘"
        self.bot.scan_window=AsyncMock(return_value=([],0))
        await self.bot.on_message(self.root)
        self.bot.llm.summarize.assert_not_awaited()
        self.assertIn("대화가 확인되지",self.thread.send.call_args.args[0])

    async def test_permission_change_during_tagged_summary_discards_result(self):
        self.root.content="<#10> 요약해줘"
        self.bot.scan_window=AsyncMock(return_value=([self.record],1))
        async def generate(*args,**kwargs):
            self.source._overwrites=[SimpleNamespace(type=1,id=55,allow=0,deny=1024)]
            return Generation("SECRET",[self.record],0)
        self.bot.llm.summarize.side_effect=generate
        await self.bot.on_message(self.root)
        self.assertNotIn("SECRET",self.thread.send.call_args.args[0])
        self.assertIn("답변을 취소",self.thread.send.call_args.args[0])

    def test_summary_scope_inheritance_limits_and_bot_tags_ignored(self):
        history=[{"role":"user","content":"<#10> 최근 2일 요약"},{"role":"assistant","content":"<#999>"}]
        for text, previous, expected_ids, hours in (("자세히",history,[10],48),
                ("<#11> 최근 3시간만",history,[11],3),("<#10> <#10>",[],[10],24)):
            ids, window = summary_request(text,previous,30)
            self.assertEqual(ids,expected_ids)
            self.assertEqual((window.end-window.start).total_seconds(),hours*3600)
        for content in ("<#10> 최근 8일", "<#10> 최근 10000일", "<#10> 최근 0시간", "<#1> <#2> <#3> <#4>"):
            with self.subTest(content=content),self.assertRaises(ValueError):
                summary_request(content,[],30)

    async def test_history_optout_between_summary_parts_stops_transmission(self):
        self.root.content="<#10> 요약해줘"
        self.previous=[self.message(102,self.thread,"제외될 이전 대화",author=88)]
        records=[replace(self.record,message_id=50+i,content=str(i)+"대화"*2000) for i in range(30)]
        self.bot.store.upsert(records)
        self.bot.scan_window=AsyncMock(return_value=(records,len(records)))
        llm=LLM(self.bot.config,self.bot.store)
        llm.require_enabled=MagicMock()
        async def provider(*args):
            self.bot.store.optout(1,88,True)
            return "일부 요약 [m:50]"
        llm.call=AsyncMock(side_effect=provider)
        self.bot.llm=llm
        await self.bot.on_message(self.message(103,self.thread,"자세히 요약해줘"))
        self.assertEqual(llm.call.await_count,1)
        self.assertIn("요약을 중단",self.thread.send.call_args.args[0])
