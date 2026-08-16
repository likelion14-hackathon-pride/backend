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

    def test_resolution_counts_grounded_answers(self):
        self.answer(Message.Verdict.GROUNDED)
        self.answer(Message.Verdict.GROUNDED_BY_CASES)
        self.answer(Message.Verdict.NO_SOURCE)

        resolution = self.get()['resolution']

        self.assertEqual((resolution['answered'], resolution['total']), (2, 3))

    # 대표에게 넘어간 것만 실패다.
    def test_a_decision_needs_the_owner(self):
        self.answer(Message.Verdict.NEEDS_DECISION)

        resolution = self.get()['resolution']

        self.assertEqual((resolution['answered'], resolution['total']), (0, 1))

    # 회사 규칙에 대한 질문이 아니었던 것을 실패로 세면 비율이 실제보다 낮아진다.
    def test_an_out_of_scope_question_is_not_a_failure(self):
        self.answer(Message.Verdict.OUT_OF_SCOPE)

        resolution = self.get()['resolution']

        self.assertEqual((resolution['answered'], resolution['total']), (1, 1))

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

    # 누적이라 우상향한다. 마지막 값이 현재 총계와 같아야 한다.
    def test_weekly_growth_is_cumulative(self):
        self.entry('오래된 규칙', confirmed_at=timezone.now() - timedelta(weeks=3))
        self.entry('최근 규칙')

        handbook = self.get()['handbook']

        self.assertEqual(handbook['weekly'][-1], handbook['confirmed'])
        self.assertLess(handbook['weekly'][0], handbook['weekly'][-1])

    # 확정 시각이 없는 옛 항목이 빠지면 그래프 끝이 총계보다 낮아진다.
    def test_entries_without_a_confirmed_time_still_count(self):
        HandbookEntry.objects.create(
            company=self.company, scope=self.eng, title='시각 없는 규칙', body_ko='본문',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
        )

        handbook = self.get()['handbook']

        self.assertEqual(handbook['confirmed'], 1)
        self.assertEqual(handbook['weekly'], [1] * len(handbook['weekly']))

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
