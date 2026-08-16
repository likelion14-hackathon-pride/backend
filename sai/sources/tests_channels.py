from datetime import timedelta

from django.db import connection as db
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from cards.models import InstructionCard
from companies.models import Company
from handbook.models import CompanyScope

from .models import Connection, Identity, Item, RawDocument


class ChannelReadingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='payment-api'
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh', ui_language='en'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.payment = self.channel('C001', '#payment', self.project)
        self.author = Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U1', external_handle='김대표',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.base = f'/api/companies/{self.company.id}/channels'

    def channel(self, external_id, label, scope=None, **extra):
        return Item.objects.create(
            company=self.company, connection=self.connection,
            external_id=external_id, label=label, scope=scope, **extra,
        )

    def message(self, ref, text='결제 쪽 이거 좀 봐주세요', item=None, occurred_at=None,
                author=True):
        return RawDocument.objects.create(
            company=self.company, item=item or self.payment, external_ref=ref,
            author_identity=self.author if author else None,
            raw_text=text, content_hash=ref.ljust(64, '0'),
            occurred_at=occurred_at or timezone.now(),
        )

    def card(self, document, **extra):
        return InstructionCard.objects.create(
            company=self.company, scope=self.project, document=document,
            purpose='결제 실패 로그의 원인을 파악한다',
            purpose_en='Find the cause of the payment failures', **extra,
        )

    def channels(self, query=''):
        return self.client.get(self.base + query).data['items']

    def messages(self, item=None, query=''):
        return self.client.get(
            f'{self.base}/{(item or self.payment).id}/messages{query}'
        ).data

    # --- 채널 목록 ---

    def test_lists_live_channels(self):
        self.channel('C002', '#dev-general')

        labels = [c['label'] for c in self.channels()]

        self.assertEqual(sorted(labels), ['#dev-general', '#payment'])

    def test_channel_carries_its_project(self):
        payment = next(c for c in self.channels() if c['label'] == '#payment')

        self.assertEqual(payment['scopeName'], 'payment-api')
        self.assertEqual(payment['scopeKind'], 'PROJECT')

    def test_removed_channels_are_hidden(self):
        self.channel('C003', '#old', removed_at=timezone.now())

        self.assertEqual([c['label'] for c in self.channels()], ['#payment'])

    def test_channels_of_a_disconnected_source_are_hidden(self):
        Connection.objects.filter(id=self.connection.id).update(
            disconnected_at=timezone.now()
        )

        self.assertEqual(self.channels(), [])

    # 마지막 대화가 있는 채널이 위로 온다.
    def test_busiest_channel_comes_first(self):
        quiet = self.channel('C004', '#quiet')
        self.message('m.1', item=quiet, occurred_at=timezone.now() - timedelta(days=2))
        self.message('m.2', item=self.payment)

        self.assertEqual([c['label'] for c in self.channels()][0], '#payment')

    def test_channel_without_messages_has_no_last_time(self):
        self.assertIsNone(self.channels()[0]['lastMessageAt'])

    def test_scope_filter(self):
        self.channel('C005', '#general')

        labels = [c['label'] for c in self.channels(f'?scopeId={self.project.id}')]

        self.assertEqual(labels, ['#payment'])

    # --- 안 읽은 수 ---

    def test_unread_counts_unopened_instructions(self):
        self.card(self.message('u.1'))
        self.card(self.message('u.2'))

        self.assertEqual(self.channels()[0]['unreadCount'], 2)

    def test_opened_instructions_drop_out(self):
        card = self.card(self.message('u.3'))
        InstructionCard.objects.filter(id=card.id).update(read_at=timezone.now())

        self.assertEqual(self.channels()[0]['unreadCount'], 0)

    # 카드가 없는 메시지는 지시가 아니라 세지 않는다.
    def test_plain_messages_are_not_unread(self):
        self.message('u.4')

        self.assertEqual(self.channels()[0]['unreadCount'], 0)

    def test_repeated_requests_count_once(self):
        original = self.card(self.message('u.5'))
        self.card(self.message('u.6'), duplicate_of=original)

        self.assertEqual(self.channels()[0]['unreadCount'], 1)

    # --- 메시지 목록 ---

    def test_lists_messages_newest_first(self):
        self.message('a.1', text='첫 번째', occurred_at=timezone.now() - timedelta(hours=1))
        self.message('a.2', text='두 번째')

        bodies = [m['body'] for m in self.messages()['items']]

        self.assertEqual(bodies, ['두 번째', '첫 번째'])

    def test_message_carries_its_author(self):
        self.message('b.1')

        item = self.messages()['items'][0]

        self.assertEqual(item['author'], '김대표')
        self.assertFalse(item['isBot'])

    def test_message_without_an_author(self):
        self.message('b.2', author=False)

        self.assertIsNone(self.messages()['items'][0]['author'])

    def test_bot_message_is_marked(self):
        Identity.objects.filter(id=self.author.id).update(is_bot=True)
        self.message('b.3')

        self.assertTrue(self.messages()['items'][0]['isBot'])

    # 카드가 있으면 지시다. 화면이 이걸로 해석을 열지 말지 가른다.
    def test_a_message_with_a_card_is_an_instruction(self):
        card = self.card(self.message('c.1'))

        item = self.messages()['items'][0]

        self.assertTrue(item['isInstruction'])
        self.assertEqual(item['cardId'], card.id)

    def test_a_message_without_a_card_is_context(self):
        self.message('c.2', text='가능하면 오늘까지 부탁드립니다')

        item = self.messages()['items'][0]

        self.assertFalse(item['isInstruction'])
        self.assertIsNone(item['cardId'])

    def test_messages_of_another_channel_do_not_leak(self):
        other = self.channel('C006', '#admin-web')
        self.message('d.1', item=other)

        self.assertEqual(self.messages()['items'], [])

    def test_pagination(self):
        for index in range(3):
            self.message(f'e.{index}')

        page = self.messages(query='?limit=2')

        self.assertEqual(len(page['items']), 2)
        self.assertIsNotNone(page['nextCursor'])

    # 메시지마다 카드를 찾으면 목록 한 번에 쿼리가 메시지 수만큼 늘어난다.
    def test_query_count_does_not_grow(self):
        self.card(self.message('f.0'))
        with CaptureQueriesContext(db) as one:
            self.messages()

        for index in range(1, 5):
            self.card(self.message(f'f.{index}'))
        with CaptureQueriesContext(db) as many:
            self.messages()

        self.assertEqual(len(many.captured_queries), len(one.captured_queries))

    # --- 접근 권한 ---

    def test_another_companys_channel_is_not_found(self):
        other = Company.objects.create(name='다른회사', code='OTHERCODE')
        stranger = Item.objects.create(
            company=other, connection=self.connection, external_id='C999', label='#x'
        )

        self.assertEqual(
            self.client.get(f'{self.base}/{stranger.id}/messages').status_code, 404
        )

    def test_outsider_is_rejected(self):
        outsider = User.objects.create_user(
            email='x@example.com', password='pw', display_name='X'
        )
        self.client.force_authenticate(user=outsider)

        self.assertEqual(self.client.get(self.base).status_code, 403)
