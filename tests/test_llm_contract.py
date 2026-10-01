import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from openai import AsyncOpenAI
from chatbot.config import Config
from chatbot.llm import LLM
from chatbot.store import Store

class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'db.sqlite')
        self.cfg=Config('dummy',1,frozenset({2}),self.store.path,30,'Asia/Seoul','', 'gemini-test',False,50,500)
        self.llm=LLM(self.cfg,self.store)

    async def asyncTearDown(self):
        await self.llm.close()
        self.tmp.cleanup()

    def client(self, handler):
        self.llm.client=AsyncOpenAI(api_key='dummy-not-real',base_url='https://generativelanguage.googleapis.com/v1beta/openai/',max_retries=0,
                                   http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    async def test_real_sdk_wire_contract_without_external_network(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json={'id':'fake','object':'chat.completion','created':0,'model':'gemini-test',
               'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':' 요약입니다 '}}]})
        self.client(handler)
        self.assertEqual(await self.llm.call('task','source'), '요약입니다')
        request=requests[0]
        self.assertEqual(str(request.url),'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions')
        body=json.loads(request.content)
        self.assertEqual(body['max_completion_tokens'],1500)
        self.assertNotIn('store',body)
        self.assertNotIn('tools',body)
        self.assertEqual([m['role'] for m in body['messages']],['system','user'])

    async def test_error_is_sanitized_with_no_retry(self):
        for status in [400,401,403,404,429,500]:
            with self.subTest(status=status):
                calls=[]
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status,json={'error':{'message':'SECRET_SENTINEL','type':'bad_request','code':'error'}})
                self.client(handler)
                with self.assertRaises(ValueError) as caught:
                    await self.llm.call('task','source')
                self.assertNotIn('SECRET_SENTINEL',str(caught.exception))
                self.assertEqual(len(calls),1)
                await self.llm.close()
