from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from qna.models import Escalation, Message, Thread
from sources.models import Connection, Item, RawDocument

from .models import InstructionCard


class HomeTests(TestCase):
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
        self.url = f'/api/companies/{self.company.id}/home'

    def document(self, ref='1.1', occurred_at=None):
        return RawDocument.objects.create(
            company=self.company, item=self.item, external_ref=ref,
            raw_text='결제 쪽 이거 좀 봐주세요', content_hash=ref.ljust(64, '0'),
            occurred_at=occurred_at or timezone.now(),
        )

    def card(self, ref='1.1', occurred_at=None, **extra):
        return InstructionCard.objects.create(
            company=self.company, scope=self.project,
            document=self.document(ref, occurred_at),
            purpose='결제 실패 로그의 원인을 파악한다',
            purpose_en='Find the cause of the payment failures', **extra,
        )

    def answer(self, verdict, when=None):
        thread = Thread.objects.create(company=self.company, user=self.member)
        message = Message.objects.create(
            company=self.company, thread=thread, role=Message.Role.AI, verdict=verdict
        )
        if when:
            Message.objects.filter(id=message.id).update(created_at=when)

        return message

    def escalated(self, message, sent=True):
        return Escalation.objects.create(
            company=self.company, asked_by=self.member, origin_message=message,
            question_en='?', draft_ko='?',
            status=Escalation.Status.SENT if sent else Escalation.Status.DRAFT,
            sent_at=timezone.now() if sent else None,
        )

    def entry(self, title, scope=None, confirmed_at=None):
        return HandbookEntry.objects.create(
            company=self.company, scope=scope or self.eng, title=title, body_ko='본문',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
            confirmed_at=confirmed_at or timezone.now(),
        )

    def get(self):
        return self.client.get(self.url).data

    # --- 오늘 읽은 것 ---

    def test_counts_todays_messages_and_cards(self):
        self.card(ref='a.1')
        self.document(ref='a.2')

        read = self.get()['readToday']

        self.assertEqual(read['messages'], 2)
        self.assertEqual(read['cards'], 1)

    def test_yesterdays_messages_are_not_today(self):
        self.document(ref='b.1', occurred_at=timezone.now() - timedelta(days=1))

        self.assertEqual(self.get()['readToday']['messages'], 0)

    # 어제 온 지시를 오늘 처리해도 오늘 읽은 것은 아니다.
    # 만든 시각으로 세면 원문 0건인데 카드 1건이 나온다.
    def test_a_card_made_today_from_an_old_message_is_not_today(self):
        self.card(ref='b.2', occurred_at=timezone.now() - timedelta(days=1))

        read = self.get()['readToday']

        self.assertEqual(read['messages'], 0)
        self.assertEqual(read['cards'], 0)

    # 카드가 된 것은 오늘 들어온 원문의 부분집합이다.
    def test_cards_never_exceed_messages(self):
        self.card(ref='b.3')
        self.document(ref='b.4')

        read = self.get()['readToday']

        self.assertLessEqual(read['cards'], read['messages'])

    # 남이 보낸 질문을 내가 기다릴 이유가 없다.
    def test_waiting_counts_only_my_questions(self):
        other = User.objects.create_user(
            email='o@example.com', password='pw', display_name='Linh'
        )
        Membership.objects.create(
            user=other, company=self.company, role=Membership.Role.MEMBER
        )
        for user in (self.member, other):
            Escalation.objects.create(
                company=self.company, asked_by=user, question_en='?',
                draft_ko='?', status=Escalation.Status.SENT,
            )

        self.assertEqual(self.get()['readToday']['waiting'], 1)

    def test_answered_questions_are_not_waiting(self):
        Escalation.objects.create(
            company=self.company, asked_by=self.member, question_en='?',
            draft_ko='?', status=Escalation.Status.ANSWERED,
        )

        self.assertEqual(self.get()['readToday']['waiting'], 0)

    # --- 안 읽은 지시 ---

    def test_unread_shows_the_newest(self):
        self.card(ref='c.1')
        newest = self.card(ref='c.2')

        unread = self.get()['unread']

        self.assertEqual(unread['count'], 2)
        self.assertEqual(unread['latest']['cardId'], newest.id)
        self.assertEqual(unread['latest']['text'], '결제 쪽 이거 좀 봐주세요')

    def test_read_cards_drop_out(self):
        card = self.card()
        InstructionCard.objects.filter(id=card.id).update(read_at=timezone.now())

        unread = self.get()['unread']

        self.assertEqual(unread['count'], 0)
        self.assertIsNone(unread['latest'])

    def test_duplicates_are_not_counted_twice(self):
        original = self.card(ref='d.1')
        self.card(ref='d.2', duplicate_of=original)

        self.assertEqual(self.get()['unread']['count'], 1)

    # --- 해결률 ---

    def test_resolution_counts_answers(self):
        self.answer(Message.Verdict.GROUNDED)
        self.answer(Message.Verdict.GROUNDED_BY_CASES)

        resolution = self.get()['resolution']

        self.assertEqual((resolution['answered'], resolution['total']), (2, 2))

    # 슬랙으로 보낸 것만 실패다.
    def test_a_question_sent_to_the_owner_is_a_failure(self):
        self.answer(Message.Verdict.GROUNDED)
        self.escalated(self.answer(Message.Verdict.NO_SOURCE))

        resolution = self.get()['resolution']

        self.assertEqual((resolution['answered'], resolution['total']), (1, 2))

    # 근거가 없다고 답해도 팀원이 안 보내고 넘어갔으면 대표를 부른 적이 없다.
    def test_an_unsent_question_is_not_a_failure(self):
        self.answer(Message.Verdict.NO_SOURCE)
        self.answer(Message.Verdict.NEEDS_DECISION)

        resolution = self.get()['resolution']

        self.assertEqual((resolution['answered'], resolution['total']), (0, 0))

    # 초안만 만들고 안 보낸 것도 마찬가지다.
    def test_a_draft_that_never_went_out_is_not_a_failure(self):
        self.escalated(self.answer(Message.Verdict.NO_SOURCE), sent=False)

        self.assertEqual(self.get()['resolution']['total'], 0)

    # 회사 규칙에 대한 질문이 아니었던 것은 답도 아니고 대표를 부르지도 않았다.
    def test_an_out_of_scope_question_is_counted_nowhere(self):
        self.answer(Message.Verdict.OUT_OF_SCOPE)

        resolution = self.get()['resolution']

        self.assertEqual((resolution['answered'], resolution['total']), (0, 0))

    # 카드 미정 항목에서 올라온 질문은 팀원이 물어서 생긴 것이 아니다.
    def test_a_card_blank_question_is_not_counted(self):
        Escalation.objects.create(
            company=self.company, asked_by=self.member, question_en='?',
            draft_ko='?', status=Escalation.Status.SENT, sent_at=timezone.now(),
        )

        self.assertEqual(self.get()['resolution']['total'], 0)

    def test_old_answers_fall_out_of_the_window(self):
        self.answer(Message.Verdict.GROUNDED, when=timezone.now() - timedelta(days=30))

        self.assertEqual(self.get()['resolution']['total'], 0)

    def test_no_questions_is_not_an_error(self):
        resolution = self.get()['resolution']

        self.assertEqual((resolution['answered'], resolution['total']), (0, 0))

    # --- 핸드북 ---

    def test_counts_confirmed_entries(self):
        self.entry('배포 전 공지')
        HandbookEntry.objects.create(
            company=self.company, scope=self.eng, title='초안', body_ko='본문',
            status=HandbookEntry.Status.DRAFT, origin=HandbookEntry.Origin.SLACK,
        )

        handbook = self.get()['handbook']

        self.assertEqual(handbook['confirmed'], 1)
        self.assertEqual(handbook['addedThisMonth'], 1)

    # 지식공간별 개수. 화면이 공간마다 세면 쿼리가 공간 수만큼 늘어난다.
    def test_scope_counts(self):
        self.entry('회사 규칙')
        self.entry('프로젝트 규칙', scope=self.project)

        counts = {s['name']: s['entryCount'] for s in self.get()['handbook']['scopes']}

        self.assertEqual(counts['Product / Engineering'], 1)
        self.assertEqual(counts['payment-api'], 1)

    # 총계와 이번 달 증가는 따로 나간다. 차트는 어느 주에 늘었는지를 맡는다.
    def test_weekly_shows_when_rules_were_added(self):
        self.entry('오래된 규칙', confirmed_at=timezone.now() - timedelta(weeks=3, days=1))
        self.entry('최근 규칙')

        self.assertEqual(self.get()['handbook']['weekly'], [1, 0, 0, 1])

    def test_weekly_counts_each_week_separately(self):
        for _ in range(3):
            self.entry('이번 주 규칙')

        self.assertEqual(self.get()['handbook']['weekly'], [0, 0, 0, 3])

    # 언제 확정됐는지 모르는 항목은 어느 주에도 넣지 않는다. 총계에는 들어간다.
    def test_an_entry_without_a_confirmed_time_is_in_no_week(self):
        HandbookEntry.objects.create(
            company=self.company, scope=self.eng, title='시각 없는 규칙', body_ko='본문',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
        )

        handbook = self.get()['handbook']

        self.assertEqual(handbook['confirmed'], 1)
        self.assertEqual(handbook['weekly'], [0, 0, 0, 0])

    # 4주보다 오래된 것은 차트 밖이다.
    def test_rules_older_than_the_window_are_not_shown(self):
        self.entry('아주 오래된 규칙', confirmed_at=timezone.now() - timedelta(weeks=10))

        handbook = self.get()['handbook']

        self.assertEqual(handbook['confirmed'], 1)
        self.assertEqual(sum(handbook['weekly']), 0)

    # --- 할 일 ---

    def test_home_carries_the_todos(self):
        self.card(ref='e.1', assignee=self.member)

        self.assertEqual(len(self.get()['todos']), 1)

    # --- 접근 권한 ---

    def test_outsider_is_rejected(self):
        outsider = User.objects.create_user(
            email='x@example.com', password='pw', display_name='X'
        )
        self.client.force_authenticate(user=outsider)

        self.assertEqual(self.client.get(self.url).status_code, 403)
