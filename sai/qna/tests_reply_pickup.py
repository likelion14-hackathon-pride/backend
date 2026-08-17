from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership, User
from companies.models import Company
from sources.models import Connection, Item
from sources.slack import SlackError
from sources.webhook import handle_event

from .escalation import make_thread_ref
from .models import Escalation
from .services import PENDING_ANSWER_MAX_AGE, collect_pending_answers
from .tests_escalation import judgement

SENT_TS = '1786800000.000100'
REPLY_TS = '1786800000.000200'
EARLIER_TS = '1786800000.000050'


class ReplyPickupTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Alex'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test',
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev',
        )
        self.escalation = self._escalation(SENT_TS)

    def _escalation(self, ts, channel='C001', status=Escalation.Status.SENT):
        return Escalation.objects.create(
            company=self.company, asked_by=self.member,
            question_en='How many vacation days do I get?',
            draft_ko='대표님, 연차는 며칠인지 확인 부탁드립니다.',
            status=status,
            slack_thread_ref=make_thread_ref(channel, ts) if ts else None,
            sent_at=timezone.now(),
        )

    def _event(self, **overrides):
        return {
            'type': 'message', 'channel': 'C001', 'user': 'U001',
            'text': '연차는 그냥 쓰고 캘린더에만 등록해주세요', 'ts': REPLY_TS,
            **overrides,
        }

    def _pending_at(self, escalation=None):
        (escalation or self.escalation).refresh_from_db()

        return (escalation or self.escalation).reply_pending_at

    # --- 웹훅이 답장을 표시한다 ---

    def test_thread_reply_marks_the_question(self):
        handle_event(self.connection, self._event(thread_ts=SENT_TS))

        self.assertIsNotNone(self._pending_at())

    # 스레드 답글은 한 번 더 눌러야 해서 대부분 채널에 그냥 답한다.
    def test_channel_reply_marks_the_latest_question(self):
        handle_event(self.connection, self._event())

        self.assertIsNotNone(self._pending_at())

    def test_the_newest_question_wins_in_the_same_channel(self):
        newer = self._escalation('1786800000.000150')

        handle_event(self.connection, self._event())

        self.assertIsNone(self._pending_at())
        self.assertIsNotNone(self._pending_at(newer))

    # 질문보다 먼저 올라온 메시지는 그 질문의 답일 수 없다.
    def test_message_before_the_question_is_ignored(self):
        handle_event(self.connection, self._event(ts=EARLIER_TS))

        self.assertIsNone(self._pending_at())

    def test_other_channel_is_ignored(self):
        Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C002', label='#random',
        )

        handle_event(self.connection, self._event(channel='C002'))

        self.assertIsNone(self._pending_at())

    # SAI가 보낸 질문 자체가 답장으로 잡히면 자기 글을 판정하게 된다.
    def test_bot_message_is_ignored(self):
        handle_event(self.connection, self._event(bot_id='B1'))

        self.assertIsNone(self._pending_at())

    def test_unsent_question_is_ignored(self):
        draft = self._escalation(None, status=Escalation.Status.DRAFT)

        handle_event(self.connection, self._event())

        self.assertIsNone(self._pending_at(draft))

    def test_answered_question_is_not_marked_again(self):
        self.escalation.status = Escalation.Status.ANSWERED
        self.escalation.save(update_fields=['status'])

        handle_event(self.connection, self._event())

        self.assertIsNone(self._pending_at())

    # --- 워커가 회수한다 ---

    def _collect(self, replies=None, verdict=None, error=None, now=None):
        default_replies = [
            {'ts': SENT_TS, 'bot_id': 'B1', 'text': 'SAI가 보낸 질문'},
            {'ts': REPLY_TS, 'user': 'U001', 'text': '연차는 그냥 쓰고 캘린더에만 등록해주세요'},
        ]
        with patch('qna.escalation.SlackClient.thread_replies',
                   return_value=default_replies if replies is None else replies,
                   side_effect=error), \
             patch('qna.escalation.SlackClient.channel_history', return_value=[]), \
             patch('qna.services.judge_reply', return_value=verdict or judgement()):
            return collect_pending_answers(now)

    def test_marked_reply_becomes_an_answer(self):
        handle_event(self.connection, self._event(thread_ts=SENT_TS))

        self.assertEqual(self._collect(), 1)
        self.escalation.refresh_from_db()
        self.assertEqual(self.escalation.status, Escalation.Status.ANSWERED)
        self.assertIsNotNone(self.escalation.answered_at)
        self.assertIsNone(self.escalation.reply_pending_at)

    def test_unmarked_question_is_not_collected(self):
        self.assertEqual(self._collect(), 0)
        self.escalation.refresh_from_db()
        self.assertEqual(self.escalation.status, Escalation.Status.SENT)

    # 회피성 답변은 답이 아니다. 표시만 지우고 답변대기에 그대로 둔다.
    def test_deflection_clears_the_mark_without_answering(self):
        handle_event(self.connection, self._event(thread_ts=SENT_TS))

        self.assertEqual(self._collect(verdict=judgement(is_answer=False)), 1)
        self.escalation.refresh_from_db()
        self.assertEqual(self.escalation.status, Escalation.Status.SENT)
        self.assertIsNone(self.escalation.reply_pending_at)

    # 슬랙이 잠깐 죽은 것뿐일 수 있다. 표시를 남겨야 다음 바퀴에 다시 시도한다.
    def test_failure_keeps_the_mark(self):
        handle_event(self.connection, self._event(thread_ts=SENT_TS))

        self.assertEqual(self._collect(error=SlackError('slack_down')), 0)
        self.escalation.refresh_from_db()
        self.assertEqual(self.escalation.status, Escalation.Status.SENT)
        self.assertIsNotNone(self.escalation.reply_pending_at)

    # 영영 회수되지 않는 표시를 매분 다시 시도하면 슬랙만 계속 부른다.
    def test_stale_mark_is_given_up(self):
        handle_event(self.connection, self._event(thread_ts=SENT_TS))
        later = timezone.now() + PENDING_ANSWER_MAX_AGE + timedelta(minutes=1)

        self.assertEqual(self._collect(now=later), 0)
        self.escalation.refresh_from_db()
        self.assertEqual(self.escalation.status, Escalation.Status.SENT)
