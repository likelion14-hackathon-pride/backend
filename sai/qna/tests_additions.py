from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope
from sources.models import Connection, Item
from sources.slack import SlackError

from .escalation import Addition, AdditionResult
from .models import Escalation, Message, Thread

SENT_TS = '1700000000.000100'


def translator(*texts):
    parsed = AdditionResult(
        lines=[Addition(index=index, text=text) for index, text in enumerate(texts)]
    )

    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                parse=lambda **kwargs: SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
                )
            )
        )
    )


@override_settings(OPENAI_API_KEY='test-key')
class AdditionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
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
            external_id='C001', label='#payment-api', scope=self.scope,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.base = f'/api/companies/{self.company.id}/questions'

    def escalation(self, draft='테스트 코드도 함께 작성할까요?'):
        return Escalation.objects.create(
            company=self.company, asked_by=self.member, scope=self.scope,
            question_en='Should I write tests?', draft_ko=draft,
        )

    def send(self, escalation, extra=None, korean=(), slack_error=None):
        payload = {'itemId': self.item.id}
        if extra is not None:
            payload['extraEn'] = extra

        with (
            patch('qna.escalation.OpenAI', return_value=translator(*korean)),
            patch('qna.escalation.SlackClient.post_message',
                  return_value={'ok': True, 'ts': SENT_TS}, side_effect=slack_error) as post,
        ):
            response = self.client.post(f'{self.base}/{escalation.id}/send',
                                        payload, format='json')

        return response, post

    def sent_text(self, escalation):
        escalation.refresh_from_db()

        return escalation.sent_text

    # --- 덧붙인 줄 ---

    # 팀원이 자기 언어로 적으면 대표에게는 한국어 문장으로 나간다.
    def test_added_line_goes_out_in_korean(self):
        escalation = self.escalation()

        response, _ = self.send(
            escalation,
            extra=['Only the payment module, right?'],
            korean=['결제 모듈만 보면 될까요?'],
        )

        self.assertEqual(response.status_code, 200)
        text = self.sent_text(escalation)
        self.assertIn('결제 모듈만 보면 될까요?', text)
        self.assertNotIn('Only the payment module', text)

    # 덧붙인 줄은 초안 뒤에 자기 문장으로 붙는다.
    def test_addition_follows_the_draft(self):
        escalation = self.escalation()

        self.send(escalation, extra=['a', 'b'], korean=['첫 줄입니다.', '둘째 줄입니다.'])

        text = self.sent_text(escalation)
        self.assertIn('> 테스트 코드도 함께 작성할까요?\n> 첫 줄입니다.\n> 둘째 줄입니다.', text)

    def test_no_additions_leaves_the_draft_alone(self):
        escalation = self.escalation()

        self.send(escalation)

        self.assertIn('> 테스트 코드도 함께 작성할까요?', self.sent_text(escalation))

    # 여러 줄짜리 초안도 줄마다 인용된다.
    def test_multiline_draft_is_quoted_line_by_line(self):
        escalation = self.escalation(draft='첫 질문입니다.\n둘째 질문입니다.')

        self.send(escalation)

        self.assertIn('> 첫 질문입니다.\n> 둘째 질문입니다.', self.sent_text(escalation))

    def test_a_blank_line_is_rejected(self):
        escalation = self.escalation()

        response, post = self.send(escalation, extra=['   '])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(post.called)

    def test_too_many_additions_are_rejected(self):
        escalation = self.escalation()

        response, post = self.send(escalation, extra=[f'line {i}' for i in range(6)])

        self.assertEqual(response.status_code, 400)
        self.assertFalse(post.called)

    # --- 실패 ---

    # 한국어로 못 바꾸면 아무것도 보내지 않는다.
    # 영어가 그대로 나가면 한국어를 직접 쓰지 않아도 된다는 약속이 깨진다.
    def test_nothing_is_sent_when_the_translation_fails(self):
        escalation = self.escalation()

        with (
            patch('qna.escalation.OpenAI', side_effect=RuntimeError('boom')),
            patch('qna.escalation.SlackClient.post_message') as post,
        ):
            response = self.client.post(
                f'{self.base}/{escalation.id}/send',
                {'itemId': self.item.id, 'extraEn': ['something']}, format='json',
            )

        self.assertEqual(response.status_code, 503)
        self.assertFalse(post.called)
        escalation.refresh_from_db()
        self.assertEqual(escalation.status, Escalation.Status.DRAFT)

    def test_slack_failure_leaves_it_a_draft(self):
        escalation = self.escalation()

        response, _ = self.send(
            escalation, extra=['a'], korean=['한 줄입니다.'],
            slack_error=SlackError('channel_not_found'),
        )

        self.assertEqual(response.status_code, 400)
        escalation.refresh_from_db()
        self.assertEqual(escalation.status, Escalation.Status.DRAFT)

    # --- 보낸 질문 목록 ---

    # 화면이 '어느 프로젝트 건인지'를 보여 주려면 이름이 필요하다.
    def test_the_list_carries_the_scope_name(self):
        self.escalation()

        item = self.client.get(self.base).data['items'][0]

        self.assertEqual(item['scopeId'], self.scope.id)
        self.assertEqual(item['scopeName'], 'payment-api')

    def test_scope_name_is_null_when_there_is_no_scope(self):
        Escalation.objects.create(
            company=self.company, asked_by=self.member, question_en='?', draft_ko='?'
        )

        names = [i['scopeName'] for i in self.client.get(self.base).data['items']]

        self.assertIn(None, names)

    # --- 응답 거부 사유 ---

    # 대표 화면은 SAI가 왜 답하지 않았는지를 먼저 보여 준다.
    def test_the_list_carries_the_refusal_verdict(self):
        thread = Thread.objects.create(company=self.company, user=self.member)
        message = Message.objects.create(
            company=self.company, thread=thread, role=Message.Role.AI,
            verdict=Message.Verdict.NEEDS_DECISION,
        )
        Escalation.objects.create(
            company=self.company, asked_by=self.member, origin_message=message,
            question_en='Can I deploy during a hotfix?', draft_ko='핫픽스 배포 가능할까요?',
        )

        item = self.client.get(self.base).data['items'][0]

        self.assertEqual(item['originVerdict'], 'NEEDS_DECISION')

    # 카드 미정 항목에서 올라온 질문은 물어본 적이 없어 판정이 없다.
    def test_a_blank_question_has_no_verdict(self):
        self.escalation()

        self.assertIsNone(self.client.get(self.base).data['items'][0]['originVerdict'])
