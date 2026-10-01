from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo
import tempfile
import unittest

from chatbot.config import Config
from chatbot.llm import LLM, SUMMARY_STYLE, evidence_line, link_citations, prepare, preprocess_summary, citation_evidence, validate_citations
from chatbot.store import Record, Store


class LLMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.cfg = Config("dummy-not-a-token", 1, frozenset({10}), self.store.path,
                          30, "Asia/Seoul", "", "gemini-3.5-flash-lite", False, 50, 500)
        self.llm = LLM(self.cfg, self.store)
        self.record = Record(100, 1, 10, 50, "김테스트", "배포는 내일입니다", 1700000000, 1700000000)

    def tearDown(self):
        self.tmp.cleanup()

    def attach_mock(self, text="배포 예정입니다. [m:1]", status="stop"):
        message = SimpleNamespace(content=text, refusal=None, tool_calls=None)
        create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=status)]))
        self.llm.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return create

    async def test_external_api_disabled_by_default(self):
        with self.assertRaises(ValueError):
            await self.llm.call("요약", "data")

    async def test_single_chunk_summary_and_gemini_contract(self):
        create = self.attach_mock()
        result = await self.llm.summarize([self.record])
        self.assertIn(self.record.url, result.text)
        self.assertEqual(create.await_count, 1)
        args = create.call_args.kwargs
        self.assertNotIn("store", args)
        self.assertEqual(args["max_completion_tokens"], 1500)
        self.assertEqual([m["role"] for m in args["messages"]], ["system", "user"])
        self.assertNotIn("tools", args)
        self.assertIn(SUMMARY_STYLE, args["messages"][0]["content"])
        self.assertEqual(result.omitted, 0)

    async def test_multi_chunk_map_reduce(self):
        create = self.attach_mock(text="일부 요약")
        records = [replace(self.record, message_id=100+i, content=str(i)+"대화" * 2000) for i in range(30)]
        result = await self.llm.summarize(records)
        self.assertGreater(create.await_count, 1)
        self.assertTrue(result.sources)
        for call in create.call_args_list[:-1]:
            self.assertNotIn(SUMMARY_STYLE, call.kwargs["messages"][0]["content"])
        self.assertIn(SUMMARY_STYLE, create.call_args.kwargs["messages"][0]["content"])

    async def test_summary_uses_user_prompt_as_separate_data(self):
        import json
        create=self.attach_mock()
        prompt="결정 사항만 세 줄로 요약해줘"
        await self.llm.summarize([self.record],instruction=prompt,history=[
            {"role":"user","content":"이전 질문"},
            {"role":"assistant","content":"이전 요약의 잘못된 사실"}])
        request=create.call_args.kwargs['messages']
        self.assertNotIn(prompt,request[0]['content'])
        self.assertIn(SUMMARY_STYLE,request[0]['content'])
        payload=json.loads(request[1]['content'].split('\n',1)[1])
        self.assertEqual(payload['question'],prompt)
        self.assertIn(self.record.content,payload['evidence'])
        self.assertEqual(payload['conversation'][0]['content'],"이전 질문")
        self.assertEqual(len(payload['conversation']),1)
        self.assertNotIn("이전 요약의 잘못된 사실",request[1]['content'])

    async def test_custom_prompt_reaches_partial_and_final_summaries(self):
        import json
        create=self.attach_mock(text="일부 요약")
        records=[replace(self.record,message_id=100+i,content=str(i)+"대화"*2000) for i in range(30)]
        await self.llm.summarize(records,instruction="위험 요소만 정리해줘")
        self.assertGreater(create.await_count,1)
        for call in create.call_args_list:
            payload=json.loads(call.kwargs['messages'][1]['content'].split('\n',1)[1])
            self.assertEqual(payload['question'],"위험 요소만 정리해줘")
        self.assertIn('partial_summaries',payload)

    async def test_summary_rechecks_guard_between_provider_calls(self):
        create=self.attach_mock()
        records=[replace(self.record,message_id=100+i,content=str(i)+"대화"*2000) for i in range(30)]
        guard=AsyncMock(side_effect=[None,ValueError("privacy changed")])
        with self.assertRaisesRegex(ValueError,"privacy changed"):
            await self.llm.summarize(records,instruction="요약해줘",before_call=guard)
        self.assertEqual(create.await_count,1)
        self.assertEqual(guard.await_count,2)

    async def test_incomplete_model_output_rejected(self):
        self.attach_mock(status="length")
        with self.assertRaises(ValueError):
            await self.llm.call("요약", "data")

    async def test_empty_model_output_rejected(self):
        self.attach_mock(text=" ")
        with self.assertRaises(ValueError):
            await self.llm.call("요약", "data")

    async def test_missing_choices_rejected(self):
        create = self.attach_mock()
        create.return_value = SimpleNamespace(choices=[])
        with self.assertRaises(ValueError):
            await self.llm.call("요약", "data")

    async def test_filter_and_unknown_completion_rejected(self):
        for reason in ("content_filter", "tool_calls", None, "unknown"):
            with self.subTest(reason=reason):
                self.attach_mock(status=reason)
                with self.assertRaises(ValueError):
                    await self.llm.call("요약", "data")

    async def test_failed_partial_prevents_merge(self):
        create = self.attach_mock()
        normal = create.return_value
        create.side_effect = [normal, ValueError("failed")]
        records = [replace(self.record, message_id=100+i, content=str(i)+"대화" * 2000) for i in range(30)]
        with self.assertRaises(ValueError):
            await self.llm.summarize(records)
        self.assertEqual(create.await_count, 2)

    async def test_answer_requires_evidence(self):
        self.attach_mock()
        with self.assertRaises(ValueError):
            await self.llm.answer("언제?", [])

    async def test_answer_validates_question(self):
        self.attach_mock()
        with self.assertRaises(ValueError):
            await self.llm.answer("x" * 1001, [self.record])

    async def test_followup_keyword_extraction_and_bad_outputs(self):
        create = self.attach_mock(text='["배포", "일정", "배포"]')
        self.assertEqual(await self.llm.question_keywords("그럼 언제야?", [{"role":"user","content":"배포에 대해 알려줘"}]), ["배포","일정"])
        self.assertIn("배포에 대해 알려줘", create.call_args.kwargs["messages"][1]["content"])
        self.assertNotIn(SUMMARY_STYLE, create.call_args.kwargs["messages"][0]["content"])
        for text in ('{}', '[null]', '["https://evil.test"]', '["a","b","c","d","e"]', 'not JSON'):
            self.attach_mock(text=text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                await self.llm.question_keywords("질문", [])

    async def test_thread_conversation_without_evidence_and_history_cap(self):
        create = self.attach_mock(text="도움이 되었다니 다행입니다.")
        history = [{"role":"user","content":str(i)+"가"*1200} for i in range(20)]
        result = await self.llm.thread_answer("고마워", [], history)
        self.assertEqual(result.sources, [])
        self.assertNotIn("확인되지 않은 근거", result.text)
        import json
        payload = create.call_args.kwargs["messages"][1]["content"].split("\n",1)[1].split("\n이번 evidence:",1)[0]
        turns = json.loads(payload)["conversation"]
        self.assertEqual(len(turns),12)
        self.assertTrue(all(len(turn["content"]) <= 1000 for turn in turns))

    async def test_empty_classifier_still_searches_participant_question(self):
        self.attach_mock(text='[]')
        for question, expected in [
            ("다들 계절이 뭔지 알려줘", ["계절"]),
            ("각자 소속이 어디야", ["소속"]),
            ("우리 배포 담당자 누구야", ["배포", "담당자"]),
        ]:
            with self.subTest(question=question):
                self.assertEqual(await self.llm.question_keywords(question, []), expected)

    async def test_general_requests_do_not_trigger_participant_fallback(self):
        self.attach_mock(text='[]')
        for question in ["계절이란 무엇이야?", "strlen 설명해줘", "우리 환영 인사 써줘", "다들 읽을 안내문 작성해줘"]:
            with self.subTest(question=question):
                self.assertEqual(await self.llm.question_keywords(question, []), [])

    async def test_general_answer_without_evidence_preserves_text(self):
        explanation = "strlen은 널 문자를 제외한 문자열의 바이트 수를 반환합니다."
        self.attach_mock(text=explanation)
        result = await self.llm.thread_answer("C언어 strlen 설명해줘", [], [])
        self.assertEqual(result.text, explanation)
        self.assertEqual(result.sources, [])
        self.assertEqual(result.omitted, 0)

    async def test_general_answer_cannot_fabricate_server_citation(self):
        self.attach_mock(text="서버에서도 그렇게 결정했습니다. [m:1]")
        with self.assertRaises(ValueError):
            await self.llm.thread_answer("C언어 strlen 설명해줘", [], [])

    def test_citation_allowlist_and_external_url_removal(self):
        text = link_citations("근거 [m:100] 거짓 [m:999] https://example.test/steal", [self.record])
        self.assertIn(self.record.url, text)
        self.assertIn("확인되지 않은 근거", text)
        self.assertNotIn("example.test", text)

    def test_input_budget_selects_recent_complete_records(self):
        records = [replace(self.record, message_id=100+i, content="본문" * 500) for i in range(20)]
        selected, lines = prepare(records, ZoneInfo("Asia/Seoul"), budget=5000)
        self.assertLess(len(selected), len(records))
        self.assertEqual(selected[-1].message_id, 119)
        self.assertLessEqual(sum(len(x) + 1 for x in lines), 5000)

    def test_preprocessing_preserves_agreement_disagreement_and_reply_context(self):
        texts = ["안녕하세요!", "ㅋㅋㅋ", "토요일 7시에 모일까?", "네", "아니요", "좋아", "일요일로 변경 확정", "안녕하세요"]
        records = [replace(self.record, message_id=100+i, content=text) for i,text in enumerate(texts)]
        records.append(replace(self.record,message_id=108,content="그 인사는 누구한테 한 거야?",reply_to=107))
        kept, removed = preprocess_summary(records)
        self.assertEqual(removed,2)
        self.assertEqual([r.message_id for r in kept],list(range(102,109)))
        self.assertEqual(records[0].content,"안녕하세요!")

    def test_duplicate_filter_keeps_different_people_later_repeats_and_replies(self):
        first=self.record
        duplicate=replace(first,message_id=101)
        other_person=replace(first,message_id=102,author_id=51)
        later=replace(first,message_id=103,author_id=51,created_at=first.created_at+180)
        reply=replace(first,message_id=104,author_id=51,created_at=later.created_at,reply_to=100)
        kept, removed=preprocess_summary([first,duplicate,other_person,later,reply])
        self.assertEqual(removed,1)
        self.assertEqual([r.message_id for r in kept],[100,102,103,104])

    def test_requests_about_reactions_or_verbatim_content_skip_filtering(self):
        records=[replace(self.record,content="ㅋㅋㅋ")]
        for request in ("웃는 반응을 정리해줘", "인사를 누가 했어?", "원문 그대로 보여줘"):
            self.assertEqual(preprocess_summary(records,request),(records,0))

    def test_whitespace_changes_in_code_are_not_duplicates(self):
        records=[replace(self.record,content="if ok:\n    deploy()\nlog()"),
                 replace(self.record,message_id=101,content="if ok:\n    deploy()\n    log()")]
        self.assertEqual(preprocess_summary(records),(records,0))

    def test_compact_evidence_preserves_every_field_and_escaping(self):
        import json
        record=replace(self.record,content='인용 "내용"\n두 번째 줄',reply_to=99)
        row=json.loads(evidence_line(record,ZoneInfo("Asia/Seoul")))
        self.assertEqual(row,["m:100","10","2023-11-15T07:13:20+09:00","김테스트",record.content,"m:99"])

    async def test_greeting_only_summary_needs_no_provider(self):
        create=self.attach_mock()
        result=await self.llm.summarize([replace(self.record,content="안녕하세요!")])
        create.assert_not_awaited()
        self.assertEqual(result.preprocessed,1)
        self.assertEqual(result.omitted,0)

    async def test_larger_window_preserves_over_500_messages(self):
        create=self.attach_mock(text="일부 요약")
        records=[replace(self.record,message_id=100+i,content=f"결정 사항 {i}",
                         created_at=self.record.created_at+i*120) for i in range(1500)]
        result=await self.llm.summarize(records)
        self.assertEqual(len(result.sources),1500)
        self.assertEqual(result.omitted,0)
        self.assertLessEqual(create.await_count,5)

    async def test_noise_omission_is_separate_from_budget_omission(self):
        self.attach_mock()
        result=await self.llm.summarize([replace(self.record,message_id=99,content="ㅋㅋㅋ"),self.record])
        self.assertEqual(result.preprocessed,1)
        self.assertEqual(result.omitted,0)
        self.assertEqual(result.sources,[self.record])

    def test_grouped_citations_link_only_known_ids_and_deduplicate(self):
        second = replace(self.record, message_id=101)
        expected = f"근거 [원문]({self.record.url}) [원문]({second.url}) [확인되지 않은 근거]"
        for citation in ("[m:100, m:101, m:100, m:999]", "[ m:100,101,100,999 ]"):
            with self.subTest(citation=citation):
                self.assertEqual(link_citations("근거 " + citation, [self.record, second]), expected)

    async def test_short_alias_links_to_exact_19_digit_discord_id(self):
        import json
        record=replace(self.record,message_id=1555090654625792003,reply_to=1555090654625792002)
        create=self.attach_mock(text="일정이 변경됨 [m:1].")
        result=await self.llm.summarize([record])
        self.assertIn(record.url,result.text)
        self.assertNotIn("확인되지 않은 근거",result.text)
        payload=create.call_args.kwargs["messages"][1]["content"]
        self.assertNotIn(str(record.message_id),payload)
        self.assertNotIn(str(record.reply_to),payload)
        self.assertIn("답글 원문 미포함",payload)
        self.assertEqual(result.sources,[record])

    async def test_invalid_model_citation_rejects_entire_answer(self):
        for citation in ("[m:999]", "[m:1555090654625792000]", "[m:10]",
                         "[m:999x]", "[m:1, m:999x]", "[m:999"):
            self.attach_mock(text="확정됐음 " + citation)
            with self.subTest(citation=citation),self.assertRaisesRegex(ValueError,"원문 연결 정보"):
                await self.llm.summarize([replace(self.record,message_id=1555090654625792003)])

    async def test_aliases_shared_across_chunks_and_merge(self):
        from unittest.mock import patch
        create=self.attach_mock()
        def response(text):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=text,refusal=None,tool_calls=None))])
        create.side_effect=[response("첫 결정 [m:1]"),response("변경된 결정 [m:2]"),
                            response("결정이 변경됨 [m:1, m:2, m:1]")]
        records=[replace(self.record,content="첫 문장\u2028다음 문장\u0085그다음\u2029끝"),
                 replace(self.record,message_id=101,content="배포일이 변경됐습니다")]
        with patch("chatbot.llm.SUMMARY_CHUNK_SIZE",1):
            result=await self.llm.summarize(records)
        self.assertEqual(create.await_count,3)
        self.assertEqual(result.text.count(self.record.url),1)
        self.assertIn(records[1].url,result.text)

    async def test_partial_cannot_cite_record_only_in_other_chunk(self):
        from unittest.mock import patch
        create=self.attach_mock(text="확정됐음 [m:2]")
        records=[self.record,replace(self.record,message_id=101,content="다른 화제")]
        with patch("chatbot.llm.SUMMARY_CHUNK_SIZE",1), self.assertRaisesRegex(ValueError,"원문 연결 정보"):
            await self.llm.summarize(records)
        self.assertEqual(create.await_count,1)

    async def test_merge_cannot_introduce_uncited_source(self):
        from unittest.mock import patch
        create=self.attach_mock()
        def response(text):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=text,refusal=None,tool_calls=None))])
        create.side_effect=[response("첫 결정 [m:1]"),response("두 번째 부분에는 관련 정보 없음"),
                            response("새 결정 [m:2]")]
        records=[self.record,replace(self.record,message_id=101,content="다른 화제")]
        with patch("chatbot.llm.SUMMARY_CHUNK_SIZE",1), self.assertRaisesRegex(ValueError,"원문 연결 정보"):
            await self.llm.summarize(records)

    async def test_answer_and_thread_answer_use_request_local_aliases(self):
        record=replace(self.record,message_id=1555090654625792003)
        for mode in ("answer", "thread_answer"):
            self.attach_mock(text="내일 예정임 [m:1]")
            if mode == "answer":
                result=await self.llm.answer("언제야?",[record])
            else:
                result=await self.llm.thread_answer("언제야?",[record],[])
            self.assertIn(record.url,result.text)

    async def test_omitted_source_gets_no_valid_alias(self):
        from unittest.mock import patch
        records=[replace(self.record,content="큰 본문"*1000),
                 replace(self.record,message_id=101,content="최신 결정")]
        self.attach_mock(text="옛 결정 [m:2]")
        with patch("chatbot.llm.SUMMARY_BUDGET",1000), self.assertRaisesRegex(ValueError,"원문 연결 정보"):
            await self.llm.summarize(records)


if __name__ == "__main__":
    unittest.main()
