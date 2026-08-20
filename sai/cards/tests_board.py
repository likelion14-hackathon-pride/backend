from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from policy.models import RiskKeyword
from qna.models import Escalation
from qna.tests import openai_stub
from sources.models import Connection, Item, RawDocument

from .models import Blank, InstructionCard

VECTOR = [0.1] * 1536


class BoardTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.eng = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='payment-api'
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh', ui_language='en'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=connection,
            external_id='C001', label='#payment-api', scope=self.project,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.base = f'/api/companies/{self.company.id}/cards'

    def card(self, ref='1.1', text='결제 로그 좀 봐주세요', **extra):
        document = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref=ref, raw_text=text,
            content_hash=ref.ljust(64, '0'), occurred_at=timezone.now(),
        )

        return InstructionCard.objects.create(
            company=self.company, scope=self.project, document=document,
            purpose='결제 실패 로그의 원인을 파악한다',
            purpose_en='Find the cause of the payment failures',
            embedding=VECTOR, **extra,
        )

    def question(self, card, status, acknowledged=False):
        escalation = Escalation.objects.create(
            company=self.company, asked_by=self.member, question_en='How deep?',
            draft_ko='어디까지 볼까요?', status=status,
            answered_at=timezone.now() if status == Escalation.Status.ANSWERED else None,
            acknowledged_at=timezone.now() if acknowledged else None,
        )
        Blank.objects.create(
            company=self.company, card=card, question_en='How deep?', escalation=escalation
        )

        return escalation

    def columns(self):
        return {i['id']: i['column'] for i in self.client.get(self.base).data['items']}

    # --- 열 산출 ---

    def test_new_card_is_ready(self):
        card = self.card()

        self.assertEqual(self.columns()[card.id], 'READY')

    def test_taken_card_is_in_progress(self):
        card = self.card(status=InstructionCard.Status.IN_PROGRESS)

        self.assertEqual(self.columns()[card.id], 'IN_PROGRESS')

    # 질문을 보내면 사람이 옮기지 않아도 Waiting 으로 간다.
    def test_sent_question_moves_to_waiting(self):
        card = self.card(status=InstructionCard.Status.IN_PROGRESS)
        self.question(card, Escalation.Status.SENT)

        self.assertEqual(self.columns()[card.id], 'WAITING')

    def test_answer_moves_to_answered(self):
        card = self.card(status=InstructionCard.Status.IN_PROGRESS)
        self.question(card, Escalation.Status.ANSWERED)

        self.assertEqual(self.columns()[card.id], 'ANSWERED')

    # 답을 확인하면 원래 하던 자리로 돌아간다.
    def test_acknowledged_answer_returns_to_status(self):
        card = self.card(status=InstructionCard.Status.IN_PROGRESS)
        self.question(card, Escalation.Status.ANSWERED, acknowledged=True)

        self.assertEqual(self.columns()[card.id], 'IN_PROGRESS')

    # 어느 열에서든 Done 으로 끌어다 놓을 수 있어야 한다.
    def test_done_beats_an_open_question(self):
        card = self.card(status=InstructionCard.Status.DONE)
        self.question(card, Escalation.Status.SENT)

        self.assertEqual(self.columns()[card.id], 'DONE')

    # --- 열 이동 ---

    def move(self, card, target):
        return self.client.patch(
            f'{self.base}/{card.id}', {'status': target}, format='json'
        )

    def test_ready_can_be_taken_on(self):
        card = self.card()

        self.assertEqual(self.move(card, 'IN_PROGRESS').data['column'], 'IN_PROGRESS')

    def test_in_progress_can_be_finished(self):
        card = self.card(status=InstructionCard.Status.IN_PROGRESS)

        self.assertEqual(self.move(card, 'DONE').data['column'], 'DONE')

    # 답을 받고 나면 바로 끝낼 수 있어야 한다.
    def test_answered_can_be_finished(self):
        card = self.card(status=InstructionCard.Status.IN_PROGRESS)
        self.question(card, Escalation.Status.ANSWERED)

        self.assertEqual(self.move(card, 'DONE').data['column'], 'DONE')

    # 손도 안 댄 일을 끝났다고 할 수는 없다.
    def test_ready_cannot_jump_to_done(self):
        card = self.card()

        response = self.move(card, 'DONE')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['field'], 'status')

    # 답을 기다리는 중에는 끝났다고 할 수 없다.
    def test_waiting_cannot_be_finished(self):
        card = self.card(status=InstructionCard.Status.IN_PROGRESS)
        self.question(card, Escalation.Status.SENT)

        self.assertEqual(self.move(card, 'DONE').status_code, 400)

    def test_done_can_be_reopened(self):
        card = self.card(status=InstructionCard.Status.DONE)

        self.assertEqual(self.move(card, 'IN_PROGRESS').data['column'], 'IN_PROGRESS')

    # 한 번 잡은 일을 안 잡은 것으로 되돌릴 수는 없다.
    def test_nothing_goes_back_to_ready(self):
        for state in (InstructionCard.Status.IN_PROGRESS, InstructionCard.Status.DONE):
            card = self.card(ref=f'r.{state}', status=state)

            self.assertEqual(self.move(card, 'READY').status_code, 400)

    # 상태를 안 보내면 전이 규칙과 무관하다.
    def test_assignee_only_patch_is_not_a_move(self):
        card = self.card()

        response = self.client.patch(
            f'{self.base}/{card.id}', {'assigneeId': self.member.id}, format='json'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['column'], 'READY')

    def test_question_counts(self):
        card = self.card(status=InstructionCard.Status.IN_PROGRESS)
        self.question(card, Escalation.Status.SENT)

        item = self.client.get(self.base).data['items'][0]
        self.assertEqual(item['openQuestionCount'], 1)
        self.assertEqual(item['answeredQuestionCount'], 0)

    # --- 필터 ---

    def test_column_filter(self):
        self.card(ref='1.1')
        taken = self.card(ref='1.2', status=InstructionCard.Status.IN_PROGRESS)

        response = self.client.get(f'{self.base}?column=IN_PROGRESS')

        self.assertEqual([i['id'] for i in response.data['items']], [taken.id])

    def test_scope_filter(self):
        mine = self.card(ref='1.1')
        other = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='admin-web'
        )
        InstructionCard.objects.filter(id=self.card(ref='1.2').id).update(scope=other)

        response = self.client.get(f'{self.base}?scopeId={self.project.id}')

        self.assertEqual([i['id'] for i in response.data['items']], [mine.id])

    def test_invalid_scope_id(self):
        self.assertEqual(self.client.get(f'{self.base}?scopeId=abc').status_code, 400)

    # 카드마다 질문을 세면 목록 한 번에 쿼리가 카드 수만큼 늘어난다.
    def test_list_query_count_does_not_grow(self):
        self.question(self.card(ref='2.0'), Escalation.Status.SENT)
        with CaptureQueriesContext(connection) as one:
            self.client.get(self.base)

        for i in range(1, 5):
            self.question(self.card(ref=f'2.{i}'), Escalation.Status.SENT)
        with CaptureQueriesContext(connection) as many:
            self.client.get(self.base)

        self.assertEqual(len(many.captured_queries), len(one.captured_queries))

    # --- 읽음 ---

    def test_detail_marks_read(self):
        card = self.card()

        self.assertFalse(self.client.get(self.base).data['items'][0]['isRead'])
        self.client.get(f'{self.base}/{card.id}')

        self.assertTrue(self.client.get(self.base).data['items'][0]['isRead'])

    # --- 상세 ---

    # In progress 에서 보여 줄 규칙. 카드에 저장된 벡터를 써서 AI 호출이 없다.
    def test_detail_lists_related_rules(self):
        card = self.card()
        entry = HandbookEntry.objects.create(
            company=self.company, scope=self.eng, title='배포 전 공지',
            body_ko='배포 전에 #dev 에 공지합니다.', body_en='Post in #dev before deploying.',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
            embedding_ko=VECTOR,
        )

        response = self.client.get(f'{self.base}/{card.id}')

        self.assertEqual(
            [r['entryId'] for r in response.data['relatedRules']], [entry.id]
        )

    def test_detail_without_embedding_has_no_rules(self):
        card = self.card()
        InstructionCard.objects.filter(id=card.id).update(embedding=None)

        response = self.client.get(f'{self.base}/{card.id}')

        self.assertEqual(response.data['relatedRules'], [])

    # 지시를 받고 바로 실행하는 자리라 위험 작업 경고가 여기 있어야 한다.
    def test_detail_warns_about_risky_actions(self):
        card = self.card(text='배포 좀 해주세요')
        RiskKeyword.objects.create(
            company=self.company, word='배포', level='DANGER',
            note='프로덕션 배포는 대표가 직접 실행합니다.',
        )

        response = self.client.get(f'{self.base}/{card.id}')

        self.assertEqual(response.data['riskWarnings'][0]['keyword'], '배포')

    def test_detail_lists_questions(self):
        card = self.card()
        escalation = self.question(card, Escalation.Status.SENT)

        response = self.client.get(f'{self.base}/{card.id}')

        question = response.data['questions'][0]
        self.assertEqual(question['escalationId'], escalation.id)
        self.assertEqual(question['status'], 'SENT')

    # --- 카드 문맥 질문 ---

    @override_settings(OPENAI_API_KEY='test-key')
    def test_ask_uses_the_card_scope(self):
        card = self.card()
        HandbookEntry.objects.create(
            company=self.company, scope=self.project, title='배포 전 QA 승인',
            body_ko='운영 배포 전에 QA 승인을 받습니다.', status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.SLACK, embedding_ko=VECTOR,
        )

        client = openai_stub()
        with (
            patch('qna.answering.OpenAI', return_value=client),
            patch('handbook.gaps.OpenAI', return_value=client),
        ):
            response = self.client.post(
                f'{self.base}/{card.id}/ask', {'question': 'Do I need QA?'}, format='json'
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['citations'][0]['title'], '배포 전 QA 승인')

    @override_settings(OPENAI_API_KEY='test-key')
    def test_ask_sends_the_card_as_context(self):
        card = self.card()
        captured = {}

        def answer(company, question, lang, scope):
            captured['question'] = question
            raise RuntimeError('stop here')

        with patch('qna.services.answer_question', side_effect=answer):
            self.client.post(
                f'{self.base}/{card.id}/ask', {'question': 'How deep?'}, format='json'
            )

        self.assertIn('Find the cause of the payment failures', captured['question'])
        self.assertIn('결제 로그 좀 봐주세요', captured['question'])

    @override_settings(OPENAI_API_KEY='test-key')
    def test_card_ask_escalation_moves_ready_card_to_waiting(self):
        card = self.card()
        client = openai_stub(
            verdict='NO_SOURCE', answer='', cited=(),
            draft_ko='대표님, 어느 환경 로그를 보면 될까요?',
        )
        with (
            patch('qna.answering.OpenAI', return_value=client),
            patch('handbook.gaps.OpenAI', return_value=client),
        ):
            answer = self.client.post(
                f'{self.base}/{card.id}/ask',
                {'question': 'Which environment should I check?'},
                format='json',
            )

        escalation = self.client.post(
            f'/api/companies/{self.company.id}/questions',
            {'messageId': answer.data['messageId']},
            format='json',
        )
        with patch('qna.escalation.SlackClient.post_message',
                   return_value={'ok': True, 'ts': '1786800000.000100'}):
            self.client.post(
                f'/api/companies/{self.company.id}/questions/{escalation.data["id"]}/send',
                {'itemId': self.item.id},
                format='json',
            )

        self.assertEqual(self.columns()[card.id], 'WAITING')
        blank = Blank.objects.get(card=card)
        self.assertEqual(blank.escalation_id, escalation.data['id'])

    def test_ask_requires_a_question(self):
        card = self.card()

        response = self.client.post(f'{self.base}/{card.id}/ask', {}, format='json')

        self.assertEqual(response.status_code, 400)
