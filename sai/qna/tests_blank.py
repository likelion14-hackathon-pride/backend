from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from cards.models import Blank, InstructionCard
from companies.models import Company
from handbook.models import CompanyScope
from sources.models import Connection, Item, RawDocument

from .escalation import AnswerJudgement
from .models import Escalation

DRAFT_KO = '결제 실패 로그 확인 건인데, 어느 환경 로그를 보면 될까요?'


def drafter_stub(text=DRAFT_KO):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
                )
            )
        )
    )


# 카드에 "확인이 필요합니다"가 떠도 물어볼 방법이 없으면 거기서 끝난다.
@override_settings(OPENAI_API_KEY='test-key')
class BlankEscalationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Alex', ui_language='en'
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
        document = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref='1.1',
            raw_text='결제 실패 로그 좀 봐주실 수 있을까요? 내일 오전까지면 좋겠어요',
            content_hash='a' * 64, occurred_at=timezone.now(),
        )
        self.card = InstructionCard.objects.create(
            company=self.company, scope=self.scope, document=document,
            assignee=self.member, purpose='결제 실패 로그의 원인을 파악한다',
        )
        self.blank = Blank.objects.create(
            company=self.company, card=self.card,
            question_en='Which environment should I check?',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.url = f'/api/companies/{self.company.id}/questions'

    def escalate(self, payload=None, draft=DRAFT_KO):
        with patch('qna.escalation.OpenAI', return_value=drafter_stub(draft)):
            return self.client.post(
                self.url, payload or {'blankId': self.blank.id}, format='json'
            )

    # --- 질문 만들기 ---

    def test_blank_becomes_an_escalation(self):
        response = self.escalate()

        self.assertEqual(response.status_code, 201)
        escalation = Escalation.objects.get(id=response.data['id'])
        self.assertEqual(escalation.question_en, 'Which environment should I check?')
        self.assertEqual(escalation.draft_ko, DRAFT_KO)
        self.assertEqual(escalation.status, Escalation.Status.DRAFT)

    # 어느 카드에서 나온 질문인지 남아야 답이 돌아올 자리를 안다.
    def test_blank_is_linked_to_the_escalation(self):
        response = self.escalate()

        self.blank.refresh_from_db()
        self.assertEqual(self.blank.escalation_id, response.data['id'])

    # 카드의 지식공간을 물려받아야 답변이 맞는 자리의 규칙이 된다.
    def test_scope_comes_from_the_card(self):
        response = self.escalate()

        self.assertEqual(Escalation.objects.get(id=response.data['id']).scope, self.scope)

    # 같은 항목을 두 번 물으면 대표에게 같은 질문이 두 번 간다.
    def test_second_escalation_is_rejected(self):
        self.escalate()

        response = self.escalate()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['field'], 'blankId')
        self.assertEqual(response.data['error']['message'], 'already escalated')

    # 직접 쓴 초안이 있으면 AI를 부르지 않는다.
    def test_given_draft_is_used_as_is(self):
        with patch('qna.escalation.OpenAI') as client:
            response = self.client.post(
                self.url,
                {'blankId': self.blank.id, 'draftKo': '스테이징인가요 운영인가요?'},
                format='json',
            )

        self.assertFalse(client.called)
        self.assertEqual(
            Escalation.objects.get(id=response.data['id']).draft_ko, '스테이징인가요 운영인가요?'
        )

    # 초안을 못 만들면 영어 원문이 그대로 대표에게 나간다. 그럴 바엔 막는다.
    def test_empty_draft_is_rejected(self):
        response = self.escalate(draft='   ')

        self.assertEqual(response.status_code, 400)

    def test_other_company_blank_is_404(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        Blank.objects.filter(id=self.blank.id).update(company=other)

        self.assertEqual(self.escalate().status_code, 404)

    def test_message_and_blank_together_is_rejected(self):
        response = self.client.post(
            self.url, {'blankId': self.blank.id, 'messageId': 1}, format='json'
        )

        self.assertEqual(response.status_code, 400)

    # --- 답이 돌아왔을 때 ---

    def answer(self, is_answer=True):
        escalation = Escalation.objects.get(id=self.escalate().data['id'])
        Escalation.objects.filter(id=escalation.id).update(
            status=Escalation.Status.SENT, slack_thread_ref='C001:1.2'
        )
        judgement = AnswerJudgement(
            reason='운영 환경이라고 답했습니다.',
            is_answer=is_answer,
            needs_review=False,
            answer_ko='운영 환경 로그를 보시면 됩니다.' if is_answer else '',
            answer_en='Check the production logs.' if is_answer else '',
            title_ko='결제 로그는 Sentry에서 확인합니다.' if is_answer else '',
        )
        with patch('qna.services.fetch_reply', return_value=(object(), '운영이요')), \
             patch('qna.services.judge_reply', return_value=judgement):
            self.client.post(f'{self.url}/{escalation.id}/check-answer', format='json')

        return escalation

    # 답은 왔는데 카드가 그대로 비어 있으면 물어본 보람이 없다.
    def test_answer_fills_the_card_blank(self):
        self.answer()

        self.blank.refresh_from_db()
        self.assertEqual(self.blank.sai_answer_ko, '운영 환경 로그를 보시면 됩니다.')
        self.assertEqual(self.blank.sai_answer_en, 'Check the production logs.')
        self.assertEqual(self.blank.answered_by, Blank.AnsweredBy.OWNER)

    # 얼버무린 답장은 카드에 넣지 않는다. 빈칸은 그대로 사람을 기다린다.
    def test_non_answer_leaves_the_card_blank_empty(self):
        self.answer(is_answer=False)

        self.blank.refresh_from_db()
        self.assertIsNone(self.blank.sai_answer_ko)
        self.assertIsNone(self.blank.sai_answer_en)
        self.assertIsNone(self.blank.answered_by)

    # 카드 화면에서 답을 바로 볼 수 있어야 한다.
    def test_card_detail_shows_the_answer(self):
        self.answer()

        response = self.client.get(
            f'/api/companies/{self.company.id}/cards/{self.card.id}'
        )

        blank = response.data['blanks'][0]
        self.assertEqual(blank['saiAnswerEn'], 'Check the production logs.')
        self.assertIsNotNone(blank['escalationId'])
