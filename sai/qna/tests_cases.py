from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from sources.models import Chunk, Connection, Identity, Item, RawDocument

from .models import Citation, Message
from .tests import openai_stub

VECTOR = [0.1] * 1536
FAR_VECTOR = [-0.1] * 1536
CASE_TEXT = '핫픽스는 금요일에도 나갑니다. 대신 #dev에 먼저 공지하고 올려요'


# 확정 규칙이 아직 없는 회사. 과거 대화만 쌓여 있다.
@override_settings(OPENAI_API_KEY='test-key')
class AskWithCasesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Lee', ui_language='en'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=connection,
            external_id='C001', label='#dev', scope=self.scope,
        )
        self.author = Identity.objects.create(
            company=self.company, connection=connection,
            external_user_id='U001', external_handle='조상원',
        )
        self.chunk = self.case(CASE_TEXT)

        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.url = f'/api/companies/{self.company.id}/ask'

    def case(self, text, ref='1.1', embedding=None, scope=None):
        document = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref=ref,
            author_identity=self.author, raw_text=text, content_hash=ref.ljust(64, '0'),
            occurred_at=timezone.now(), permalink=f'https://slack/{ref}',
        )

        return Chunk.objects.create(
            company=self.company, document=document, scope=scope or self.scope,
            ord=0, text=text, embedding=embedding or VECTOR,
        )

    def ask(self, question='Can I deploy a hotfix on Friday?', **stub):
        client = openai_stub(**stub)
        with (
            patch('qna.answering.OpenAI', return_value=client),
            patch('handbook.gaps.OpenAI', return_value=client),
        ):
            return self.client.post(self.url, {'question': question}, format='json')

    # 규칙이 하나도 없어도 과거 대화로 답할 수 있어야 한다.
    def test_case_is_retrieved_when_no_rule_exists(self):
        response = self.ask(
            verdict='GROUNDED_BY_CASES',
            answer='People shipped hotfixes on Friday after posting in #dev first.',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['verdict'], 'GROUNDED_BY_CASES')
        citation = response.data['citations'][0]
        self.assertIsNone(citation['entryId'])
        self.assertEqual(citation['chunkId'], self.chunk.id)
        self.assertEqual(citation['permalink'], 'https://slack/1.1')

    # 출처 이름은 어느 채널의 언제 대화인지가 된다.
    def test_case_citation_is_labelled_by_channel_and_date(self):
        response = self.ask(verdict='GROUNDED_BY_CASES')

        self.assertTrue(response.data['citations'][0]['title'].startswith('#dev '))

    def test_case_citation_row_is_saved(self):
        response = self.ask(verdict='GROUNDED_BY_CASES')

        citation = Citation.objects.get(message_id=response.data['messageId'])
        self.assertEqual(citation.chunk, self.chunk)
        self.assertIsNone(citation.entry)

    # 저장한 대화 이력에서도 사례 근거가 같은 모양으로 나와야 한다.
    def test_history_shows_case_citation(self):
        self.ask(verdict='GROUNDED_BY_CASES')
        thread = Message.objects.filter(company=self.company).first().thread

        response = self.client.get(
            f'/api/companies/{self.company.id}/qna/threads/{thread.id}/messages'
        )

        answer = [m for m in response.data['items'] if m['citations']][0]
        self.assertEqual(answer['citations'][0]['chunkId'], self.chunk.id)
        self.assertEqual(answer['citations'][0]['permalink'], 'https://slack/1.1')

    def test_retrieval_snapshot_records_chunk_id(self):
        response = self.ask(verdict='GROUNDED_BY_CASES')
        message = Message.objects.get(id=response.data['messageId'])

        self.assertEqual(message.retrieval[0]['chunkId'], self.chunk.id)
        self.assertIsNone(message.retrieval[0]['entryId'])

    # 확정 규칙이 앞 번호를 쓴다. 규칙을 먼저 보게 해야 한다.
    def test_rules_come_before_cases(self):
        HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title='금요일 오후 배포 금지',
            body_ko='배포는 금요일 오후에 하지 않습니다.',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
            embedding_ko=VECTOR,
        )

        response = self.ask(verdict='GROUNDED', cited=(0,))

        self.assertIsNotNone(response.data['citations'][0]['entryId'])

    # 멀리 있는 대화는 후보에서 뺀다. 그냥 잡담이라 조금만 멀어도 엉뚱한 게 걸린다.
    def test_distant_case_is_not_retrieved(self):
        Chunk.objects.all().delete()
        self.case('점심 뭐 드세요', ref='9.9', embedding=FAR_VECTOR)

        response = self.ask(verdict='NO_SOURCE', answer='', cited=())

        self.assertEqual(response.data['citations'], [])

    def test_other_company_case_is_not_retrieved(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        Chunk.objects.filter(id=self.chunk.id).update(company=other)

        response = self.ask(verdict='NO_SOURCE', answer='', cited=())

        self.assertEqual(response.data['citations'], [])

    # 같은 말이 여러 번 올라와도 근거로는 한 줄이면 된다.
    def test_duplicate_case_text_is_shown_once(self):
        self.case(CASE_TEXT, ref='1.2')

        response = self.ask(verdict='GROUNDED_BY_CASES', cited=(0, 1))

        self.assertEqual(len(response.data['citations']), 1)
