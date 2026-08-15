from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from policy.models import RiskKeyword

from .answering import (
    PROMPT_VERSION,
    AnswerRateLimited,
    AnswerResult,
    find_risk_warnings,
)
from .models import Citation, Message, Thread


def openai_stub(verdict='GROUNDED', answer='No. Deployments are not done on Friday afternoons.',
                cited=(0,), draft_ko='', embedding=None):
    parsed = AnswerResult(
        verdict=verdict, answer=answer, cited_indexes=list(cited), draft_ko=draft_ko
    )
    client = SimpleNamespace(
        embeddings=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                data=[SimpleNamespace(embedding=embedding or [0.1] * 1536)]
            )
        ),
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                parse=lambda **kwargs: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
                    usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40),
                )
            )
        ),
    )
    return client


@override_settings(OPENAI_API_KEY='test-key')
class AskTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Lee', ui_language='en'
        )
        Membership.objects.create(user=self.member, company=self.company, role=Membership.Role.MEMBER)

        self.entry = HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title='금요일 오후 배포 금지',
            body_ko='배포는 금요일 오후에 하지 않습니다.',
            body_en='Deployments are not done on Friday afternoons.',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
            embedding_ko=[0.1] * 1536, embedding_en=[0.1] * 1536,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.url = f'/api/companies/{self.company.id}/ask'

    def ask(self, question='Can I deploy on Friday?', payload=None, **stub):
        with patch('qna.answering.OpenAI', return_value=openai_stub(**stub)):
            return self.client.post(self.url, payload or {'question': question}, format='json')

    # --- 정상 답변 ---

    def test_grounded_answer(self):
        response = self.ask()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['verdict'], 'GROUNDED')
        self.assertEqual(response.data['resultType'], 'ANSWERED')
        self.assertEqual(response.data['citations'][0]['entryId'], self.entry.id)
        self.assertEqual(response.data['citations'][0]['title'], '금요일 오후 배포 금지')

    def test_creates_thread_and_messages(self):
        response = self.ask()

        thread = Thread.objects.get(id=response.data['threadId'])
        messages = Message.objects.filter(thread=thread).order_by('id')
        self.assertEqual([m.role for m in messages], ['USER', 'AI'])
        # 화면 언어가 en인 사용자에게는 영어 칸에 저장한다.
        self.assertEqual(messages[0].body_en, 'Can I deploy on Friday?')
        self.assertIsNone(messages[0].body_ko)

    def test_records_usage_and_retrieval(self):
        response = self.ask()
        message = Message.objects.get(id=response.data['messageId'])

        self.assertEqual(message.prompt_tokens, 120)
        self.assertEqual(message.completion_tokens, 40)
        self.assertEqual(message.prompt_version, PROMPT_VERSION)
        self.assertIsNotNone(message.latency_ms)
        # 검색 스냅샷에는 id와 점수만 남는다. 본문은 넣지 않는다.
        # JSONB는 키 순서를 보존하지 않으므로 집합으로 비교한다.
        self.assertEqual(set(message.retrieval[0]), {'entryId', 'chunkId', 'score'})

    def test_citation_rows_created(self):
        response = self.ask()

        citation = Citation.objects.get(message_id=response.data['messageId'])
        self.assertEqual(citation.entry_id, self.entry.id)

    # --- 근거 없음 ---

    def test_no_source_returns_draft_for_owner(self):
        response = self.ask(
            verdict='NO_SOURCE', answer='', cited=(),
            draft_ko='대표님, 재택근무 규정이 따로 있을까요?',
        )

        self.assertEqual(response.data['verdict'], 'NO_SOURCE')
        self.assertEqual(response.data['resultType'], 'NEEDS_OWNER')
        self.assertIsNone(response.data['answer'])
        self.assertEqual(response.data['draftKo'], '대표님, 재택근무 규정이 따로 있을까요?')
        self.assertEqual(response.data['citations'], [])

    def test_needs_decision_also_needs_owner(self):
        response = self.ask(verdict='NEEDS_DECISION', answer='Rules conflict.', cited=())

        self.assertEqual(response.data['resultType'], 'NEEDS_OWNER')

    # 한국어 초안을 응답으로만 주고 버리면 나중에 에스컬레이션에서 되찾을 수 없다.
    def test_owner_draft_is_persisted(self):
        response = self.ask(
            verdict='NO_SOURCE', answer='', cited=(), draft_ko='대표님, 연차 며칠인가요?'
        )
        message = Message.objects.get(id=response.data['messageId'])

        self.assertEqual(message.body_ko, '대표님, 연차 며칠인가요?')

    # 답변이 있는 경우의 body_ko 는 초안이 아니다.
    def test_grounded_message_has_no_draft(self):
        response = self.ask()
        message = Message.objects.get(id=response.data['messageId'])

        self.assertIsNone(message.body_ko)

    def test_out_of_scope(self):
        response = self.ask(verdict='OUT_OF_SCOPE', answer='', cited=())

        self.assertEqual(response.data['resultType'], 'ANSWERED')
        self.assertIsNone(response.data['answer'])

    # 모델이 없는 번호를 인용해도 응답에 새어 나가면 안 된다.
    def test_out_of_range_citation_is_dropped(self):
        response = self.ask(cited=(0, 99))

        self.assertEqual(len(response.data['citations']), 1)

    # 인용 번호가 본문에 섞여 나오면 사용자에게 그대로 보인다.
    def test_citation_markers_are_stripped_from_answer(self):
        response = self.ask(answer='Only Jo Sang-won can restart it. [0] Ask him first. [1][2]')

        self.assertEqual(response.data['answer'], 'Only Jo Sang-won can restart it. Ask him first.')

    # --- 검색 범위 ---

    # 확정되지 않은 규칙은 답변 근거가 될 수 없다.
    def test_draft_entries_are_not_retrieved(self):
        self.entry.status = HandbookEntry.Status.DRAFT
        self.entry.save()

        response = self.ask(verdict='NO_SOURCE', answer='', cited=())

        self.assertEqual(response.data['citations'], [])
        message = Message.objects.get(id=response.data['messageId'])
        self.assertEqual(message.retrieval, [])

    # 임베딩이 없으면 검색되지 않는다.
    def test_unembedded_entries_are_not_retrieved(self):
        self.entry.embedding_ko = None
        self.entry.embedding_en = None
        self.entry.save()

        response = self.ask(verdict='NO_SOURCE', answer='', cited=())

        message = Message.objects.get(id=response.data['messageId'])
        self.assertEqual(message.retrieval, [])

    def test_other_company_entries_are_not_retrieved(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_scope = CompanyScope.objects.create(
            company=other, kind=CompanyScope.Kind.PROJECT, name='남의 프로젝트'
        )
        HandbookEntry.objects.create(
            company=other, scope=other_scope, title='남의 규칙', body_ko='내용',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
            embedding_ko=[0.1] * 1536,
        )

        response = self.ask()
        message = Message.objects.get(id=response.data['messageId'])

        self.assertEqual([r['entryId'] for r in message.retrieval], [self.entry.id])

    # --- 위험 키워드 ---

    def test_risk_keyword_warning(self):
        RiskKeyword.objects.create(
            company=self.company, word='프로덕션 DB', aliases=['production db'],
            note='운영 DB 접근은 대표 입회 하에만 가능합니다.', level=RiskKeyword.Level.DANGER,
        )

        response = self.ask(question='Can I connect to the production db?')

        self.assertEqual(response.data['warnings'][0]['keyword'], '프로덕션 DB')
        self.assertEqual(response.data['warnings'][0]['level'], 'DANGER')

    def test_no_warning_when_keyword_absent(self):
        RiskKeyword.objects.create(
            company=self.company, word='프로덕션 DB', note='주의', level=RiskKeyword.Level.DANGER
        )

        response = self.ask(question='Can I deploy on Friday?')

        self.assertEqual(response.data['warnings'], [])

    def test_warning_matches_answer_text_too(self):
        RiskKeyword.objects.create(
            company=self.company, word='deploy', note='배포는 대표만 실행합니다.',
            level=RiskKeyword.Level.CAUTION,
        )

        response = self.ask(question='What is the rule?')

        self.assertEqual(len(response.data['warnings']), 1)

    def test_find_risk_warnings_is_case_insensitive(self):
        RiskKeyword.objects.create(
            company=self.company, word='Production', note='주의', level=RiskKeyword.Level.CAUTION
        )

        self.assertEqual(len(find_risk_warnings(self.company, 'the PRODUCTION server')), 1)

    # --- 스레드 / 권한 ---

    def test_continues_existing_thread(self):
        first = self.ask()
        thread_id = first.data['threadId']

        second = self.ask(payload={'question': 'And on Thursday?', 'threadId': thread_id})

        self.assertEqual(second.data['threadId'], thread_id)
        self.assertEqual(Message.objects.filter(thread_id=thread_id).count(), 4)

    def test_cannot_use_other_users_thread(self):
        other = User.objects.create_user(email='o@example.com', password='pw', display_name='O')
        Membership.objects.create(user=other, company=self.company, role=Membership.Role.MEMBER)
        thread = Thread.objects.create(company=self.company, user=other)

        response = self.ask(payload={'question': 'hi', 'threadId': thread.id})

        self.assertEqual(response.status_code, 404)

    def test_outsider_cannot_ask(self):
        outsider = User.objects.create_user(email='x@example.com', password='pw', display_name='X')
        self.client.force_authenticate(user=outsider)

        response = self.ask()

        self.assertEqual(response.status_code, 403)

    # OpenAI 장애는 500이 아니라 503으로 알린다.
    @override_settings(OPENAI_API_KEY='')
    def test_missing_api_key_returns_503(self):
        response = self.client.post(self.url, {'question': 'hi'}, format='json')

        self.assertEqual(response.status_code, 503)

    # 사용량 한도는 장애가 아니다. 429로 알리고 언제 다시 오면 되는지 준다.
    def test_rate_limit_returns_429_with_retry_after(self):
        with patch('qna.views.answer_question', side_effect=AnswerRateLimited(20)):
            response = self.client.post(self.url, {'question': 'hi'}, format='json')

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response['Retry-After'], '20')
        # 질문은 남기되 AI 답변 행은 만들지 않는다.
        self.assertEqual(Message.objects.filter(role='AI').count(), 0)

    # --- 대화 이력 ---

    def test_thread_history(self):
        response = self.ask()
        thread_id = response.data['threadId']

        history = self.client.get(
            f'/api/companies/{self.company.id}/qna/threads/{thread_id}/messages'
        )

        self.assertEqual(history.status_code, 200)
        self.assertEqual([m['role'] for m in history.data['items']], ['USER', 'AI'])
        self.assertEqual(history.data['items'][1]['citations'][0]['title'], '금요일 오후 배포 금지')
