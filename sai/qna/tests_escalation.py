from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry, HandbookEvidence
from sources.models import Connection, Item
from sources.slack import SlackError

from .escalation import AnswerJudgement, make_thread_ref, parse_thread_ref
from .models import Escalation, Message, Thread

REPLY_TS = '1786800000.000200'
SENT_TS = '1786800000.000100'


def judgement(is_answer=True, needs_review=False, reason='명확히 답했습니다.',
              answer_ko='연차는 사전 승인 없이 쓰고 캘린더에 등록만 합니다.',
              answer_en='Take leave without prior approval; just add it to the calendar.',
              title_ko='연차 사용 절차'):
    return AnswerJudgement(
        is_answer=is_answer, needs_review=needs_review, reason=reason,
        answer_ko=answer_ko if is_answer else '', answer_en=answer_en if is_answer else '',
        title_ko=title_ko if is_answer else '',
    )


class EscalationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY, name='Company',
        )
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='김대표', ui_language='ko'
        )
        Membership.objects.create(user=self.owner, company=self.company, role=Membership.Role.OWNER)
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Alex', ui_language='en'
        )
        Membership.objects.create(user=self.member, company=self.company, role=Membership.Role.MEMBER)

        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=connection, external_id='C001', label='#dev'
        )

        # Ask SAI가 근거를 못 찾은 상황
        self.thread = Thread.objects.create(company=self.company, user=self.member)
        Message.objects.create(
            company=self.company, thread=self.thread, role=Message.Role.USER,
            body_en='How many vacation days do I get?',
        )
        self.ai_message = Message.objects.create(
            company=self.company, thread=self.thread, role=Message.Role.AI,
            verdict='NO_SOURCE', body_en="I don't know, let's ask the owner.",
            body_ko='대표님, 연차는 며칠인지 확인 부탁드립니다.',
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.base = f'/api/companies/{self.company.id}/questions'

    def create(self, payload=None):
        # payload={} 는 '빈 요청' 테스트라 falsy 검사로 기본값을 씌우면 안 된다.
        if payload is None:
            payload = {'messageId': self.ai_message.id}
        return self.client.post(self.base, payload, format='json')

    def send(self, escalation_id, error=None):
        with patch('qna.escalation.SlackClient.post_message',
                   return_value={'ok': True, 'ts': SENT_TS}, side_effect=error) as post:
            response = self.client.post(f'{self.base}/{escalation_id}/send',
                                        {'itemId': self.item.id}, format='json')
        return response, post

    def check(self, escalation_id, replies=None, history=None, verdict=None):
        default_replies = [
            {'ts': SENT_TS, 'bot_id': 'B1', 'text': 'SAI가 보낸 질문'},
            {'ts': REPLY_TS, 'user': 'U001', 'text': '연차는 그냥 쓰고 캘린더에만 등록해주세요'},
        ]
        # judge_reply 는 views 가 이미 이름으로 가져갔으므로 views 쪽을 갈아끼워야 한다.
        # escalation 모듈을 패치하면 실제 OpenAI가 호출된다.
        with patch('qna.escalation.SlackClient.thread_replies',
                   return_value=default_replies if replies is None else replies), \
             patch('qna.escalation.SlackClient.channel_history', return_value=history or []), \
             patch('qna.services.judge_reply', return_value=verdict or judgement()) as judge:
            self.judge_mock = judge
            return self.client.post(f'{self.base}/{escalation_id}/check-answer')

    # --- 스레드 참조 ---

    def test_thread_ref_round_trip(self):
        self.assertEqual(parse_thread_ref(make_thread_ref('C001', SENT_TS)), ('C001', SENT_TS))
        self.assertEqual(parse_thread_ref(None), (None, None))
        self.assertEqual(parse_thread_ref('garbage'), (None, None))

    # --- 초안 생성 ---

    def test_creates_draft_from_ask_message(self):
        response = self.create()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'DRAFT')
        self.assertEqual(response.data['questionEn'], 'How many vacation days do I get?')
        self.assertEqual(response.data['draftKo'], '대표님, 연차는 며칠인지 확인 부탁드립니다.')
        self.assertEqual(response.data['askedByName'], 'Alex')

    def test_cannot_escalate_same_message_twice(self):
        self.create()
        response = self.create()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Escalation.objects.count(), 1)

    def test_creates_from_free_text(self):
        response = self.create({'questionEn': 'Do we have a dress code?', 'draftKo': '복장 규정 있나요?'})

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(response.data['originMessageId'])

    def test_requires_question_or_message(self):
        self.assertEqual(self.create({}).status_code, 400)

    # 초안이 없으면 영어 원문이 그대로 대표에게 나간다. 막아야 한다.
    def test_rejects_when_no_korean_draft(self):
        response = self.create({'questionEn': 'Do we have a dress code?'})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Escalation.objects.exists())

    # GROUNDED 메시지의 body_ko는 초안이 아니라 답변이다. 초안으로 쓰면 안 된다.
    def test_grounded_message_body_is_not_used_as_draft(self):
        grounded = Message.objects.create(
            company=self.company, thread=self.thread, role=Message.Role.AI,
            verdict='GROUNDED', body_ko='배포는 금요일에 하지 않습니다.',
        )

        response = self.create({'messageId': grounded.id})

        self.assertEqual(response.status_code, 400)

    # --- 초안 수정 ---

    def test_edits_draft_before_sending(self):
        escalation_id = self.create().data['id']

        response = self.client.patch(
            f'{self.base}/{escalation_id}', {'draftKo': '연차 며칠인가요?'}, format='json'
        )

        self.assertEqual(response.data['draftKo'], '연차 며칠인가요?')

    def test_cannot_edit_after_sending(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        response = self.client.patch(
            f'{self.base}/{escalation_id}', {'draftKo': '수정 시도'}, format='json'
        )

        self.assertEqual(response.status_code, 400)

    # --- 발송 ---

    def test_sends_to_slack(self):
        escalation_id = self.create().data['id']

        response, post = self.send(escalation_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'SENT')
        self.assertEqual(response.data['slackThreadRef'], f'C001:{SENT_TS}')
        # 질문자 이름과 초안이 함께 나가야 대표가 맥락을 안다.
        text = post.call_args[0][1]
        self.assertIn('Alex', text)
        self.assertIn('대표님, 연차는 며칠인지 확인 부탁드립니다.', text)

    def test_cannot_send_twice(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        response, _ = self.send(escalation_id)

        self.assertEqual(response.status_code, 400)

    def test_slack_error_is_reported(self):
        escalation_id = self.create().data['id']

        response, _ = self.send(escalation_id, error=SlackError('channel_not_found'))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Escalation.objects.get(id=escalation_id).status, 'DRAFT')

    # --- 답변 회수 ---

    def test_records_answer(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        response = self.check(escalation_id)

        self.assertEqual(response.data['status'], 'ANSWERED')
        self.assertTrue(response.data['answerIsAnswer'])
        self.assertEqual(response.data['answerKo'], '연차는 사전 승인 없이 쓰고 캘린더에 등록만 합니다.')
        self.assertIsNotNone(response.data['answeredAt'])

    # "확인해볼게요" 같은 회피성 답변은 답변으로 치지 않는다.
    def test_deflection_is_not_an_answer(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        response = self.check(
            escalation_id,
            verdict=judgement(is_answer=False, reason='확인해보겠다는 말만 있습니다.'),
        )

        self.assertEqual(response.data['status'], 'SENT')
        self.assertFalse(response.data['answerIsAnswer'])
        self.assertEqual(response.data['answerReason'], '확인해보겠다는 말만 있습니다.')
        self.assertIsNone(response.data['answerKo'])

    def test_no_reply_yet_keeps_status(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        response = self.check(escalation_id, replies=[{'ts': SENT_TS, 'bot_id': 'B1', 'text': '질문'}])

        self.assertEqual(response.data['status'], 'SENT')
        self.assertIsNone(response.data['answerIsAnswer'])

    # 슬랙에서는 스레드 대신 채널에 그냥 답하는 경우가 훨씬 흔하다.
    def test_picks_up_channel_reply_when_no_thread_reply(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        response = self.check(
            escalation_id,
            replies=[{'ts': SENT_TS, 'bot_id': 'B1', 'text': '질문'}],
            history=[
                {'ts': '1786800000.000300', 'user': 'U001', 'text': '캘린더에만 등록해주세요'},
                {'ts': REPLY_TS, 'user': 'U001', 'text': '연차는 사전 승인 없이 쓰시고'},
                {'ts': SENT_TS, 'bot_id': 'B1', 'text': '질문'},
            ],
        )

        self.assertEqual(response.data['status'], 'ANSWERED')
        # 여러 줄로 나눠 쓴 답도 시간순으로 합쳐서 넘긴다.
        self.assertEqual(
            self.judge_mock.call_args[0][2],
            '연차는 사전 승인 없이 쓰시고\n캘린더에만 등록해주세요',
        )

    # 질문보다 앞선 메시지는 답이 아니다.
    def test_ignores_messages_before_the_question(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        response = self.check(
            escalation_id,
            replies=[{'ts': SENT_TS, 'bot_id': 'B1', 'text': '질문'}],
            history=[
                {'ts': SENT_TS, 'bot_id': 'B1', 'text': '질문'},
                {'ts': '1786700000.000100', 'user': 'U001', 'text': '이전 대화'},
            ],
        )

        self.assertEqual(response.data['status'], 'SENT')
        self.assertIsNone(response.data['answerIsAnswer'])

    # 다음 질문이 올라오면 거기서 끊는다. 남의 질문 답을 가져오면 안 된다.
    def test_stops_at_next_bot_message(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        self.check(
            escalation_id,
            replies=[{'ts': SENT_TS, 'bot_id': 'B1', 'text': '질문'}],
            history=[
                {'ts': '1786800000.000400', 'user': 'U001', 'text': '다른 질문의 답'},
                {'ts': '1786800000.000300', 'bot_id': 'B1', 'text': '다음 질문'},
                {'ts': REPLY_TS, 'user': 'U001', 'text': '내 질문의 답'},
                {'ts': SENT_TS, 'bot_id': 'B1', 'text': '질문'},
            ],
        )

        self.assertEqual(self.judge_mock.call_args[0][2], '내 질문의 답')

    # 스레드 답글이 있으면 그쪽이 우선이다.
    def test_thread_reply_wins_over_channel(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        self.check(
            escalation_id,
            history=[{'ts': '1786800000.000300', 'user': 'U001', 'text': '채널에 쓴 잡담'}],
        )

        self.assertEqual(
            self.judge_mock.call_args[0][2], '연차는 그냥 쓰고 캘린더에만 등록해주세요'
        )

    def test_cannot_check_before_sending(self):
        escalation_id = self.create().data['id']

        response = self.check(escalation_id)

        self.assertEqual(response.status_code, 400)

    def test_needs_review_flag(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        response = self.check(escalation_id, verdict=judgement(needs_review=True))

        self.assertTrue(response.data['answerNeedsReview'])

    # --- 핸드북 승격 ---

    def test_owner_promotes_answer_to_handbook(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)
        self.check(escalation_id)

        self.client.force_authenticate(user=self.owner)
        response = self.client.post(f'{self.base}/{escalation_id}/approve')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'APPROVED')

        entry = HandbookEntry.objects.get(id=response.data['proposedEntryId'])
        self.assertEqual(entry.status, HandbookEntry.Status.DRAFT)
        self.assertEqual(entry.origin, HandbookEntry.Origin.ESCALATION)
        self.assertEqual(entry.body_ko, '연차는 사전 승인 없이 쓰고 캘린더에 등록만 합니다.')

        evidence = entry.evidences.get()
        self.assertEqual(evidence.tag, HandbookEvidence.Tag.OWNER)

    def test_cannot_promote_without_answer(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)

        self.client.force_authenticate(user=self.owner)
        response = self.client.post(f'{self.base}/{escalation_id}/approve')

        self.assertEqual(response.status_code, 400)
        self.assertFalse(HandbookEntry.objects.exists())

    def test_member_cannot_promote(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)
        self.check(escalation_id)

        response = self.client.post(f'{self.base}/{escalation_id}/approve')

        self.assertEqual(response.status_code, 403)

    # --- 목록 / 권한 ---

    def test_member_sees_only_own_questions(self):
        self.create()
        other = User.objects.create_user(email='o@example.com', password='pw', display_name='Other')
        Membership.objects.create(user=other, company=self.company, role=Membership.Role.MEMBER)
        Escalation.objects.create(
            company=self.company, asked_by=other, question_en='다른 사람 질문', draft_ko='초안'
        )

        response = self.client.get(self.base)

        self.assertEqual(len(response.data['items']), 1)

    def test_owner_sees_all_questions(self):
        self.create()
        Escalation.objects.create(
            company=self.company, asked_by=self.member, question_en='두 번째', draft_ko='초안'
        )

        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self.base)

        self.assertEqual(len(response.data['items']), 2)

    def test_status_filter(self):
        escalation_id = self.create().data['id']
        self.send(escalation_id)
        Escalation.objects.create(
            company=self.company, asked_by=self.member, question_en='보내지 않은 것', draft_ko='초안'
        )

        response = self.client.get(f'{self.base}?status=SENT')

        self.assertEqual([i['id'] for i in response.data['items']], [escalation_id])

    def test_outsider_cannot_list(self):
        outsider = User.objects.create_user(email='x@example.com', password='pw', display_name='X')
        self.client.force_authenticate(user=outsider)

        self.assertEqual(self.client.get(self.base).status_code, 403)

    # --- 물리기 ---

    # 답할 필요가 없다고 판단한 질문이 목록에 계속 남으면 안 된다.
    def test_dismiss(self):
        escalation_id = self.create().data['id']
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(f'{self.base}/{escalation_id}/dismiss')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'DISMISSED')

    def test_dismissed_is_filtered_out(self):
        escalation_id = self.create().data['id']
        self.client.force_authenticate(user=self.owner)
        self.client.post(f'{self.base}/{escalation_id}/dismiss')

        response = self.client.get(f'{self.base}?status=DRAFT')

        self.assertEqual(response.data['items'], [])

    # 이미 규칙이 된 답변을 물리면 규칙만 남고 출처가 사라진다.
    def test_approved_cannot_be_dismissed(self):
        escalation = Escalation.objects.create(
            company=self.company, asked_by=self.member, question_en='q', draft_ko='초안',
            status=Escalation.Status.APPROVED,
        )
        self.client.force_authenticate(user=self.owner)

        response = self.client.post(f'{self.base}/{escalation.id}/dismiss')

        self.assertEqual(response.status_code, 400)
        # 본문 없는 요청이라 화면에 붙일 칸이 없다. 사유는 code 가 나른다.
        self.assertEqual(response.data['error']['code'], 'already_approved')
        self.assertIsNone(response.data['error']['field'])

    def test_member_cannot_dismiss(self):
        escalation_id = self.create().data['id']

        response = self.client.post(f'{self.base}/{escalation_id}/dismiss')

        self.assertEqual(response.status_code, 403)

    # --- 답 확인 ---

    # 확인하지 않으면 카드가 Answered 열에 계속 남는다.
    def test_acknowledge(self):
        escalation_id = self.create().data['id']
        Escalation.objects.filter(id=escalation_id).update(
            status=Escalation.Status.ANSWERED, answered_at=timezone.now()
        )

        response = self.client.post(f'{self.base}/{escalation_id}/acknowledge')

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(Escalation.objects.get(id=escalation_id).acknowledged_at)

    def test_acknowledge_before_the_answer_is_rejected(self):
        escalation_id = self.create().data['id']

        response = self.client.post(f'{self.base}/{escalation_id}/acknowledge')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['code'], 'no_answer_yet')
        self.assertIsNone(response.data['error']['field'])

    def test_acknowledge_twice_keeps_the_first_time(self):
        escalation_id = self.create().data['id']
        Escalation.objects.filter(id=escalation_id).update(
            status=Escalation.Status.ANSWERED, answered_at=timezone.now()
        )
        self.client.post(f'{self.base}/{escalation_id}/acknowledge')
        first = Escalation.objects.get(id=escalation_id).acknowledged_at

        self.client.post(f'{self.base}/{escalation_id}/acknowledge')

        self.assertEqual(Escalation.objects.get(id=escalation_id).acknowledged_at, first)
