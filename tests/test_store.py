from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest

from chatbot.store import Record, Store
from chatbot.text import index_terms, match_query, safe_chunks


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Store(Path(self.tmp.name) / "test.db")
        self.now = time.time()
        self.sample = Record(100, 1, 10, 500, "테스터", "배포일정을 내일로 변경합니다. 결제 오류도 수정", self.now, self.now)
        self.db.upsert([self.sample])

    def tearDown(self):
        self.tmp.cleanup()

    def search(self, query, channels=(10,), guild=1, since=0):
        return self.db.search(guild, channels, query, since)

    def test_two_character_korean_partial_match(self):
        self.assertEqual([r.message_id for r in self.search("배포")], [100])
        self.assertEqual(len(self.search("결제")), 1)

    def test_long_korean_phrase_and_multiple_terms(self):
        self.assertEqual(len(self.search("배포일정")), 1)
        self.assertEqual(len(self.search("배포 일정")), 1)
        self.assertEqual(len(self.search("배포 결제")), 1)
        self.assertEqual(len(self.search("배포 미존재")), 0)

    def test_grams_must_be_adjacent(self):
        self.db.upsert([replace(self.sample, content="가나 나다 가나마 나다라")])
        self.assertFalse(self.search("가나다"))

    def test_ascii_case_and_unicode_normalization(self):
        self.db.upsert([replace(self.sample, content="OpenAI ＡＰＩ 와 배포")])
        self.assertEqual(len(self.search("openai api 배포")), 1)

    def test_one_character_is_whole_token_only(self):
        self.db.upsert([replace(self.sample, content="가나다")])
        self.assertEqual(len(self.search("가")), 0)
        self.db.upsert([replace(self.sample, content="가 나다")])
        self.assertEqual(len(self.search("가")), 1)

    def test_guild_scope(self):
        self.db.upsert([replace(self.sample, message_id=101, guild_id=2)])
        self.assertEqual([r.message_id for r in self.search("배포")], [100])
        self.assertEqual([r.message_id for r in self.search("배포", guild=2)], [101])

    def test_channel_acl_and_empty_denial(self):
        self.db.upsert([replace(self.sample, message_id=102, channel_id=20)])
        self.assertEqual([r.message_id for r in self.search("배포")], [100])
        self.assertEqual([r.message_id for r in self.search("배포", channels=(20,))], [102])
        self.assertEqual(self.search("배포", channels=()), [])

    def test_date_filter(self):
        self.assertEqual(self.search("배포", since=self.now + 1), [])

    def test_upsert_idempotent_and_edit_reindexes(self):
        self.db.upsert([self.sample, self.sample])
        self.assertEqual(len(self.search("배포")), 1)
        updated = replace(self.sample, content="취소되었습니다", edited_at=self.now + 1)
        self.db.upsert([updated])
        self.assertEqual(len(self.search("배포")), 0)
        self.assertEqual(len(self.search("취소")), 1)

    def test_older_snapshot_cannot_overwrite_newer_edit(self):
        updated = replace(self.sample, content="배포 취소", edited_at=self.now + 20)
        self.db.upsert([updated])
        self.db.upsert([self.sample])
        self.assertEqual(self.search("배포")[0].content, "배포 취소")

    def test_delete_removes_index_and_stops_resurrection(self):
        self.db.delete([100])
        self.assertEqual(self.search("배포"), [])
        self.db.upsert([self.sample])
        self.assertEqual(self.search("배포"), [])

    def test_optout_removes_index_and_blocks_backfill(self):
        self.assertEqual(self.db.optout(1, 500, True), 1)
        self.db.upsert([self.sample])
        self.assertEqual(self.search("배포"), [])
        self.assertEqual(self.db.filter_eligible([self.sample]), [])
        self.db.optout(1, 500, False)
        self.db.upsert([self.sample])
        self.assertEqual(len(self.search("배포")), 1)

    def test_optout_is_guild_scoped(self):
        other = replace(self.sample, message_id=200, guild_id=2)
        self.db.upsert([other])
        self.db.optout(1, 500, True)
        self.assertEqual(len(self.search("배포", guild=2)), 1)

    def test_reconcile_deletes_only_verified_interval(self):
        older = replace(self.sample, message_id=50)
        newer = replace(self.sample, message_id=200)
        self.db.upsert([older, newer])
        self.db.reconcile(1, 10, [], 90, 150, False)
        self.assertEqual({r.message_id for r in self.search("배포")}, {50, 200})

    def test_reconcile_validates_scope(self):
        with self.assertRaises(ValueError):
            self.db.reconcile(1, 20, [self.sample], 0, 200, False)

    def test_reconcile_preserves_optout(self):
        self.db.optout(1, 500, True)
        self.db.reconcile(1, 10, [self.sample], 0, 200, False)
        self.assertEqual(self.search("배포"), [])

    def test_context_does_not_cross_channels_or_retention(self):
        related = replace(self.sample, message_id=101, created_at=self.now + 1)
        private = replace(self.sample, message_id=102, channel_id=20)
        expired = replace(self.sample, message_id=103, created_at=self.now - 100)
        self.db.upsert([related, private, expired])
        context = self.db.context(self.sample, since=self.now - 10)
        self.assertEqual({r.message_id for r in context}, {100, 101})

    def test_reply_context_is_scope_checked(self):
        private_parent = replace(self.sample, message_id=99, channel_id=20)
        reply = replace(self.sample, reply_to=99)
        self.db.upsert([private_parent, reply])
        self.assertNotIn(99, {r.message_id for r in self.db.context(reply, 0)})

    def test_question_context_includes_later_correction_within_scope(self):
        topic = replace(self.sample, content="다들 무슨계절인가욤")
        correction = replace(topic, message_id=101, content="저 봄이에요", created_at=self.now + 21*60)
        outside = replace(correction, message_id=102, created_at=self.now + 31*60)
        private = replace(correction, message_id=103, channel_id=20)
        expired = replace(correction, message_id=104, created_at=self.now - 1)
        self.db.upsert([topic, correction, outside, private, expired])
        self.assertNotIn(101, {r.message_id for r in self.db.context(topic, self.now)})
        expanded = self.db.context(topic, self.now, radius=30, window_seconds=1800)
        self.assertEqual({r.message_id for r in expanded}, {100, 101})

    def test_unchanged_detects_edits_and_deletion(self):
        self.assertTrue(self.db.unchanged([self.sample]))
        self.db.upsert([replace(self.sample, content="変更", edited_at=self.now + 1)])
        self.assertFalse(self.db.unchanged([self.sample]))
        self.db.delete([100])
        self.assertFalse(self.db.unchanged([self.sample]))

    def test_retention_and_allowlist_cleanup(self):
        self.db.upsert([replace(self.sample, message_id=10, created_at=self.now - 40 * 86400),
                        replace(self.sample, message_id=20, channel_id=20),
                        replace(self.sample, message_id=30, guild_id=2)])
        self.db.cleanup(1, [10], 30)
        self.assertEqual([r.message_id for r in self.search("배포")], [100])
        self.assertEqual(self.search("배포", guild=2), [])

    def test_empty_cleanup_purges_all(self):
        self.db.cleanup(1, [], 30)
        self.assertEqual(self.search("배포"), [])

    def test_fts_and_sql_control_characters_are_not_executed(self):
        for query in ['" OR *', "'; DROP TABLE messages; --", "배포' OR 1=1 --", "{내용} NOT 배포"]:
            self.search(query)
        self.assertEqual(len(self.search("배포")), 1)

    def test_query_size_limits(self):
        for query in ("", "   ", "*?", "가" * 201, " ".join(str(x) for x in range(13))):
            with self.assertRaises(ValueError):
                match_query(query)

    def test_daily_call_limit_persists(self):
        self.db.reserve_call(2)
        self.db.reserve_call(2)
        reopened = Store(self.db.path)
        with self.assertRaises(ValueError):
            reopened.reserve_call(2)

    def test_fts_integrity_after_mutations(self):
        self.db.upsert([replace(self.sample, content="결제 수정")])
        self.db.delete([100])
        self.db.upsert([replace(self.sample, message_id=101)])
        with self.db.connection() as conn:
            conn.execute("INSERT INTO messages_fts(messages_fts,rank) VALUES ('integrity-check',1)")

    def test_message_chunk_limit(self):
        text = "긴 메시지\n" * 1000
        chunks = list(safe_chunks(text))
        self.assertTrue(all(0 < len(x) <= 1850 for x in chunks))


if __name__ == "__main__":
    unittest.main()
