from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from companies.models import Company
from handbook.models import CompanyScope

from .chunking import build_chunks, embed_chunks, sync_chunks
from .models import Chunk, Connection, Identity, Item, RawDocument
from .text import redact_secrets

VECTOR = [0.1] * 1536


def embeddings_stub(count):
    return SimpleNamespace(data=[SimpleNamespace(embedding=VECTOR) for _ in range(count)])


class RedactSecretsTests(SimpleTestCase):
    def test_slack_token(self):
        text, hit = redact_secrets('키는 xoxb-1234567890-abcdefghij 입니다')

        self.assertTrue(hit)
        self.assertNotIn('xoxb-1234567890', text)

    def test_openai_key(self):
        text, hit = redact_secrets('sk-abcdefghijklmnopqrstuvwxyz123456')

        self.assertTrue(hit)
        self.assertNotIn('sk-abcdefghij', text)

    def test_aws_access_key(self):
        text, hit = redact_secrets('AKIAIOSFODNN7EXAMPLE 로 접속')

        self.assertTrue(hit)
        self.assertNotIn('AKIAIOSFODNN7EXAMPLE', text)

    def test_jwt(self):
        token = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefg'
        text, hit = redact_secrets(f'토큰 {token}')

        self.assertTrue(hit)
        self.assertNotIn('eyJhbGciOi', text)

    def test_private_key_block(self):
        text, hit = redact_secrets(
            '-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----'
        )

        self.assertTrue(hit)
        self.assertNotIn('abc', text)

    # 비밀정보를 말로만 언급한 것은 가리면 안 된다.
    def test_plain_text_untouched(self):
        text, hit = redact_secrets('시크릿 키는 절대 커밋하지 마세요')

        self.assertFalse(hit)
        self.assertEqual(text, '시크릿 키는 절대 커밋하지 마세요')

    def test_empty(self):
        self.assertEqual(redact_secrets(''), ('', False))


@override_settings(OPENAI_API_KEY='test-key', OPENAI_EMBEDDING_MODEL='text-embedding-3-small')
class ChunkingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev', scope=self.scope,
        )
        Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U001', external_handle='조상원',
        )

    def document(self, ref, text, **extra):
        return RawDocument.objects.create(
            company=self.company, item=self.item, external_ref=ref,
            raw_text=text, content_hash=ref.ljust(64, '0'),
            occurred_at=timezone.now(), **extra,
        )

    def embed(self):
        with patch('sources.chunking.OpenAI') as client:
            client.return_value.embeddings.create.side_effect = (
                lambda **kwargs: embeddings_stub(len(kwargs['input']))
            )
            return embed_chunks(self.company)

    # --- 청크 생성 ---

    def test_creates_one_chunk_per_document(self):
        self.document('1.1', '배포는 금요일에 하지 않습니다')
        self.document('1.2', '넵 알겠습니다')

        build_chunks(self.company)

        self.assertEqual(Chunk.objects.count(), 2)
        self.assertEqual(Chunk.objects.filter(ord=0).count(), 2)

    # 검색 품질을 위해 멘션을 사람이 읽는 형태로 바꿔 저장한다.
    def test_stores_normalized_text(self):
        self.document('1.1', '<@U001> 님 확인 부탁드려요')

        build_chunks(self.company)

        self.assertEqual(Chunk.objects.get().text, '@조상원 님 확인 부탁드려요')

    def test_inherits_scope_from_channel(self):
        self.document('1.1', '배포 규칙입니다')

        build_chunks(self.company)

        self.assertEqual(Chunk.objects.get().scope, self.scope)

    def test_detects_language(self):
        self.document('1.1', '한국어 메시지입니다')
        self.document('1.2', 'English only message')

        build_chunks(self.company)

        self.assertEqual(Chunk.objects.get(document__external_ref='1.1').lang, 'ko')
        self.assertEqual(Chunk.objects.get(document__external_ref='1.2').lang, 'en')

    # 임베딩은 외부로 나가는 경로다. 자격증명이 실려 나가면 회수할 수 없다.
    def test_redacts_secrets_before_storing(self):
        self.document('1.1', '토큰은 xoxb-9999999999-secretvalue 입니다')

        build_chunks(self.company)

        chunk = Chunk.objects.get()
        self.assertTrue(chunk.is_secret_filtered)
        self.assertNotIn('xoxb-9999999999', chunk.text)

    def test_skips_removed_documents(self):
        self.document('1.1', '삭제된 원문', sync_state=RawDocument.SyncState.REMOVED)

        build_chunks(self.company)

        self.assertEqual(Chunk.objects.count(), 0)

    # --- 재실행 ---

    def test_rebuild_does_not_duplicate(self):
        self.document('1.1', '배포는 금요일에 하지 않습니다')
        build_chunks(self.company)
        build_chunks(self.company)

        self.assertEqual(Chunk.objects.count(), 1)

    # 내용이 그대로면 벡터를 버리지 않는다. 다시 임베딩하면 돈이 든다.
    def test_unchanged_text_keeps_embedding(self):
        self.document('1.1', '배포는 금요일에 하지 않습니다')
        build_chunks(self.company)
        self.embed()
        before = Chunk.objects.get().embedded_at

        build_chunks(self.company)

        self.assertEqual(Chunk.objects.get().embedded_at, before)

    # 원문이 바뀌면 기존 벡터는 더 이상 그 텍스트가 아니다.
    def test_edited_text_clears_embedding(self):
        document = self.document('1.1', '배포는 금요일에 하지 않습니다')
        build_chunks(self.company)
        self.embed()

        document.raw_text = '배포는 금요일 오후에만 하지 않습니다'
        document.save()
        build_chunks(self.company)

        chunk = Chunk.objects.get()
        self.assertIsNone(chunk.embedded_at)
        self.assertIsNone(chunk.embedding)

    # --- 임베딩 ---

    def test_embeds_pending_chunks(self):
        self.document('1.1', '배포는 금요일에 하지 않습니다')
        build_chunks(self.company)

        count, errors = self.embed()

        self.assertEqual((count, errors), (1, []))
        chunk = Chunk.objects.get()
        self.assertEqual(len(chunk.embedding), 1536)
        self.assertEqual(chunk.embedding_model, 'text-embedding-3-small')
        self.assertIsNotNone(chunk.embedded_at)

    def test_does_not_reembed(self):
        self.document('1.1', '배포는 금요일에 하지 않습니다')
        build_chunks(self.company)
        self.embed()

        count, _ = self.embed()

        self.assertEqual(count, 0)

    # 임베딩 모델을 바꾸면 차원이나 의미 공간이 달라진다. 전부 다시 만들어야 한다.
    def test_model_change_triggers_reembed(self):
        self.document('1.1', '배포는 금요일에 하지 않습니다')
        build_chunks(self.company)
        self.embed()

        with override_settings(OPENAI_EMBEDDING_MODEL='text-embedding-3-large'):
            count, _ = self.embed()

        self.assertEqual(count, 1)

    def test_embed_failure_is_reported(self):
        from openai import OpenAIError

        self.document('1.1', '배포는 금요일에 하지 않습니다')
        build_chunks(self.company)

        with patch('sources.chunking.OpenAI') as client:
            client.return_value.embeddings.create.side_effect = OpenAIError('down')
            count, errors = embed_chunks(self.company)

        self.assertEqual(count, 0)
        self.assertEqual(errors[0]['scope'], 'embed_chunks')
        self.assertIsNone(Chunk.objects.get().embedded_at)

    def test_sync_chunks_does_both(self):
        self.document('1.1', '배포는 금요일에 하지 않습니다')

        with patch('sources.chunking.OpenAI') as client:
            client.return_value.embeddings.create.side_effect = (
                lambda **kwargs: embeddings_stub(len(kwargs['input']))
            )
            count, errors = sync_chunks(self.company)

        self.assertEqual((count, errors), (1, []))
        self.assertIsNotNone(Chunk.objects.get().embedded_at)

    def test_other_company_chunks_untouched(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_connection = Connection.objects.create(
            company=other, kind=Connection.Kind.SLACK, bot_token='xoxb-o'
        )
        other_item = Item.objects.create(
            company=other, connection=other_connection, external_id='C009', label='#other'
        )
        RawDocument.objects.create(
            company=other, item=other_item, external_ref='9.9',
            raw_text='남의 회사 메시지', content_hash='9' * 64, occurred_at=timezone.now(),
        )
        self.document('1.1', '우리 회사 메시지')

        build_chunks(self.company)

        self.assertEqual(Chunk.objects.count(), 1)
        self.assertEqual(Chunk.objects.get().company, self.company)
