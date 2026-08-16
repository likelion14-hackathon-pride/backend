from datetime import datetime, time
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from qna.models import Escalation
from sources.models import Connection, Item, RawDocument

from .models import Blank, InstructionCard, Step

SEOUL = ZoneInfo('Asia/Seoul')

# 목업과 같은 순간. 서울 수요일 21:40 은 하노이 19:40 이고 대표는 이미 퇴근했다.
NOW = datetime(2026, 8, 12, 21, 40, tzinfo=SEOUL)


def seoul(day, hour, minute=0):
    return datetime(2026, 8, day, hour, minute, tzinfo=SEOUL)


class TimingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.eng = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='payment-api'
        )
        self.owner = User.objects.create_user(
            email='o@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh',
            ui_language='en', timezone='Asia/Ho_Chi_Minh',
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
        self.url = f'/api/companies/{self.company.id}/timing'

    def card(self, ref='1.1', status=InstructionCard.Status.IN_PROGRESS, **extra):
        document = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref=ref,
            raw_text='결제 로그 좀 봐주세요', content_hash=ref.ljust(64, '0'),
            occurred_at=timezone.now(),
        )

        return InstructionCard.objects.create(
            company=self.company, scope=self.project, document=document,
            purpose='결제 실패 로그의 원인을 파악한다', status=status, **extra,
        )

    def entry(self, title='배포 전 공지'):
        return HandbookEntry.objects.create(
            company=self.company, scope=self.eng, title=title,
            body_ko='배포 전에 #dev 에 공지합니다.',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
        )

    def step(self, card, entry=None, text='Pull the failure logs', ord=0):
        return Step.objects.create(
            company=self.company, card=card, ord=ord, text='실패 로그를 받는다',
            text_en=text, entry=entry,
        )

    def blank(self, card, escalation=None, question='Are tests required?', answered_by=None):
        return Blank.objects.create(
            company=self.company, card=card, question_en=question,
            escalation=escalation, answered_by=answered_by,
        )

    def escalation(self, status=Escalation.Status.SENT, sent=None, answered=None):
        return Escalation.objects.create(
            company=self.company, asked_by=self.member, question_en='How deep?',
            draft_ko='어디까지 볼까요?', status=status,
            sent_at=sent, answered_at=answered,
        )

    def get(self, query=''):
        with patch('cards.timing.timezone.now', return_value=NOW):
            return self.client.get(self.url + query)

    # 응답은 ISO UTC 문자열이다. 같은 순간인지만 보면 되므로 파싱해서 비교한다.
    def assertMoment(self, value, expected):
        self.assertEqual(parse_datetime(value), expected)

    # --- 시각과 근무 상태 ---

    def test_two_clocks(self):
        data = self.get().data

        self.assertEqual(data['you']['timezone'], 'Asia/Ho_Chi_Minh')
        self.assertEqual(data['you']['name'], 'Minh')
        self.assertEqual(data['owner']['timezone'], 'Asia/Seoul')
        self.assertEqual(data['owner']['name'], '김대표')

    # 21:40 은 양쪽 다 근무시간 밖이다. 접속 여부가 아니라 근무시간으로 판단한다.
    def test_state_is_off_hours_at_night(self):
        data = self.get().data

        self.assertEqual(data['owner']['state'], 'OFF_HOURS')
        self.assertEqual(data['you']['state'], 'OFF_HOURS')

    # 하노이 17시는 서울 19시다. 나는 아직 근무 중이고 대표는 퇴근했다.
    def test_states_differ_across_the_gap(self):
        with patch('cards.timing.timezone.now', return_value=seoul(12, 19)):
            data = self.client.get(self.url).data

        self.assertEqual(data['you']['state'], 'WORKING')
        self.assertEqual(data['owner']['state'], 'OFF_HOURS')

    def test_state_is_unknown_when_working_hours_are_off(self):
        Company.objects.filter(id=self.company.id).update(working_hours_enabled=False)

        data = self.get().data

        self.assertEqual(data['owner']['state'], 'UNKNOWN')

    def test_working_hours_echo_the_company_setting(self):
        Company.objects.filter(id=self.company.id).update(working_hours_start=time(10, 0))

        hours = self.get().data['workingHours']

        self.assertEqual(hours['start'], '10:00:00')
        self.assertEqual(hours['end'], '18:00:00')
        self.assertEqual(hours['timezone'], 'Asia/Seoul')
        self.assertTrue(hours['enabled'])

    # --- 예상 응답 시각 ---

    # 이력이 없으면 다음 근무 시작 시각이고, 그렇다고 밝힌다.
    def test_reply_expected_without_history(self):
        expected = self.get().data['replyExpected']

        self.assertEqual(expected['basis'], 'WORKING_HOURS')
        self.assertEqual(expected['sampleSize'], 0)
        self.assertMoment(expected['at'], seoul(13, 9))

    def test_reply_expected_ignores_a_single_sample(self):
        self.escalation(Escalation.Status.ANSWERED, seoul(11, 10), seoul(11, 12))

        expected = self.get().data['replyExpected']

        self.assertEqual(expected['basis'], 'WORKING_HOURS')
        self.assertEqual(expected['sampleSize'], 1)

    # 2시간씩 걸린 이력이 셋. 다음 근무 시작 09:00 에 2시간을 더해 11:00 이 된다.
    def test_reply_expected_from_history(self):
        for day in (10, 11, 12):
            self.escalation(Escalation.Status.ANSWERED, seoul(day, 10), seoul(day, 12))

        expected = self.get().data['replyExpected']

        self.assertEqual(expected['basis'], 'HISTORY')
        self.assertEqual(expected['sampleSize'], 3)
        self.assertMoment(expected['at'], seoul(13, 11))

    # 밤을 넘긴 답변은 밤만큼 늦은 것이 아니다. 근무시간으로만 센다.
    def test_history_does_not_count_the_night(self):
        for day in (10, 11, 12):
            self.escalation(Escalation.Status.ANSWERED, seoul(day, 17), seoul(day + 1, 10))

        self.assertMoment(self.get().data['replyExpected']['at'], seoul(13, 11))

    def test_unanswered_escalations_are_not_samples(self):
        for _ in range(3):
            self.escalation(Escalation.Status.SENT, seoul(11, 10))

        self.assertEqual(self.get().data['replyExpected']['sampleSize'], 0)

    # --- 지금 할 수 있는 것 ---

    def test_can_do_lists_the_steps_of_an_unblocked_card(self):
        card = self.card()
        entry = self.entry()
        self.step(card, entry)
        self.step(card, None, text='Ask someone', ord=1)

        data = self.get().data

        self.assertEqual(data['canDoTotal'], 2)
        item = data['canDo'][0]
        self.assertEqual(item['title'], 'Pull the failure logs')
        self.assertEqual(item['cardId'], card.id)
        self.assertEqual(item['entryId'], entry.id)
        self.assertEqual(item['entryTitle'], '배포 전 공지')
        self.assertEqual(item['scopeName'], 'Product / Engineering')

    # 규칙이 붙는 단계는 드물다. 붙은 것을 먼저 올리되 나머지도 버리지 않는다.
    def test_can_do_puts_rule_backed_steps_first(self):
        card = self.card()
        self.step(card, None, text='Ask someone', ord=0)
        self.step(card, self.entry(), text='Post in #dev', ord=1)

        titles = [item['title'] for item in self.get().data['canDo']]

        self.assertEqual(titles, ['Post in #dev', 'Ask someone'])

    def test_can_do_without_a_rule_has_no_entry(self):
        self.step(self.card(), None, text='Ask someone')

        item = self.get().data['canDo'][0]

        self.assertIsNone(item['entryId'])
        self.assertIsNone(item['entryTitle'])
        self.assertIsNone(item['scopeName'])

    # 아직 안 잡은 일과 끝낸 일은 지금 할 수 있는 일이 아니다.
    def test_can_do_skips_cards_that_are_not_open(self):
        entry = self.entry()
        for ref, status in (('2.1', InstructionCard.Status.READY),
                            ('2.2', InstructionCard.Status.DONE)):
            self.step(self.card(ref=ref, status=status), entry)

        self.assertEqual(self.get().data['canDoTotal'], 0)

    # 답을 기다리는 카드는 단계도 시작할 수 없다. 무엇을 하라는 건지 아직 모르기 때문.
    def test_can_do_skips_a_card_with_an_open_question(self):
        card = self.card()
        self.step(card, self.entry())
        self.blank(card, self.escalation())

        self.assertEqual(self.get().data['canDoTotal'], 0)
        self.assertEqual(self.get().data['needsPersonTotal'], 1)

    # 답이 오면 그 카드의 단계가 다시 할 수 있는 일이 된다.
    def test_can_do_returns_once_the_question_is_answered(self):
        card = self.card()
        self.step(card, self.entry())
        self.blank(
            card,
            self.escalation(Escalation.Status.ANSWERED, seoul(11, 10), seoul(11, 12)),
            answered_by=Blank.AnsweredBy.OWNER,
        )

        self.assertEqual(self.get().data['canDoTotal'], 1)

    # SAI가 핸드북으로 답한 빈칸은 대표를 기다리지 않는다. 카드도 막지 않는다.
    def test_a_blank_answered_by_sai_does_not_block(self):
        card = self.card()
        self.step(card, self.entry())
        self.blank(card, answered_by=Blank.AnsweredBy.SAI)

        data = self.get().data

        self.assertEqual(data['canDoTotal'], 1)
        self.assertEqual(data['needsPersonTotal'], 0)

    # 막힌 카드만 빠진다. 옆 카드까지 같이 사라지면 안 된다.
    def test_one_blocked_card_does_not_hide_another(self):
        blocked = self.card(ref='4.1')
        self.step(blocked, self.entry())
        self.blank(blocked)
        self.step(self.card(ref='4.2'), None, text='Update the docs')

        titles = [item['title'] for item in self.get().data['canDo']]

        self.assertEqual(titles, ['Update the docs'])

    # --- 사람이 필요한 것 ---

    def test_needs_person_lists_unasked_blanks(self):
        card = self.card()
        blank = self.blank(card)

        item = self.get().data['needsPerson'][0]

        self.assertEqual(item['blankId'], blank.id)
        self.assertEqual(item['cardId'], card.id)
        self.assertEqual(item['title'], 'Are tests required?')
        self.assertEqual(item['scopeName'], 'payment-api')
        self.assertIsNone(item['escalationStatus'])

    def test_needs_person_keeps_sent_questions(self):
        self.blank(self.card(), self.escalation())

        self.assertEqual(self.get().data['needsPerson'][0]['escalationStatus'], 'SENT')

    # 답이 온 항목은 더 이상 사람을 기다리지 않는다.
    def test_needs_person_drops_answered_questions(self):
        self.blank(
            self.card(),
            self.escalation(Escalation.Status.ANSWERED, seoul(11, 10), seoul(11, 12)),
            answered_by=Blank.AnsweredBy.OWNER,
        )

        self.assertEqual(self.get().data['needsPersonTotal'], 0)

    # --- 상한 ---

    def test_buckets_are_capped_but_report_the_total(self):
        card = self.card()
        for index in range(8):
            self.blank(card, question=f'Q{index}')

        data = self.get().data

        self.assertEqual(len(data['needsPerson']), 5)
        self.assertEqual(data['needsPersonTotal'], 8)

    # --- 필터 ---

    def test_scope_filter(self):
        other = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='admin-web'
        )
        card = self.card()
        InstructionCard.objects.filter(id=card.id).update(scope=other)
        self.blank(card)

        self.assertEqual(self.get(f'?scopeId={self.project.id}').data['needsPersonTotal'], 0)
        self.assertEqual(self.get(f'?scopeId={other.id}').data['needsPersonTotal'], 1)

    def test_mine_filter(self):
        self.blank(self.card())

        self.assertEqual(self.get('?mine=true').data['needsPersonTotal'], 0)

        InstructionCard.objects.update(assignee=self.member)
        self.assertEqual(self.get('?mine=true').data['needsPersonTotal'], 1)

    def test_invalid_scope_id(self):
        self.assertEqual(self.get('?scopeId=abc').status_code, 400)

    # --- 접근 권한 ---

    def test_outsider_is_rejected(self):
        outsider = User.objects.create_user(
            email='x@example.com', password='pw', display_name='X'
        )
        self.client.force_authenticate(user=outsider)

        self.assertEqual(self.get().status_code, 403)

    # 카드마다 단계와 미정 항목을 세면 카드 수만큼 쿼리가 늘어난다.
    def test_query_count_does_not_grow(self):
        entry = self.entry()
        self.step(self.card(ref='3.0'), entry)
        with CaptureQueriesContext(connection) as one:
            self.get()

        for index in range(1, 5):
            card = self.card(ref=f'3.{index}')
            self.step(card, entry)
            self.blank(card)
        with CaptureQueriesContext(connection) as many:
            self.get()

        self.assertEqual(len(many.captured_queries), len(one.captured_queries))
