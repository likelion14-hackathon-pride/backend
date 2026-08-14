from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope

from .models import Connection, Item
from .services import sync_slack_channels
from .slack import SlackError

BOT_TOKEN = 'xoxb-test-token-0123456789'
SIGNING_SECRET = 'a' * 32
AUTH_TEST_OK = {'ok': True, 'team': '에코랩 워크스페이스', 'team_id': 'T01ABCDEF', 'user': 'sai'}
CHANNELS = [
    {'id': 'C001', 'name': 'general'},
    {'id': 'C002', 'name': 'payment-api'},
]


class SlackConnectionApiTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.owner = User.objects.create_user(email='owner@example.com', password='pw', display_name='대표')
        Membership.objects.create(user=self.owner, company=self.company, role=Membership.Role.OWNER)

        self.member = User.objects.create_user(email='member@example.com', password='pw', display_name='팀원')
        Membership.objects.create(user=self.member, company=self.company, role=Membership.Role.MEMBER)

        self.url = f'/api/companies/{self.company.id}/source-connections'
        self.client = APIClient()

    def connect(self, user=None, payload=None, auth_result=AUTH_TEST_OK, side_effect=None, channels=None):
        self.client.force_authenticate(user=user or self.owner)
        body = payload or {
            'provider': 'SLACK',
            'botToken': BOT_TOKEN,
            'signingSecret': SIGNING_SECRET,
        }
        # 연동 직후 채널 동기화가 실제 슬랙을 부르지 않도록 함께 막는다.
        with patch('sources.views.SlackClient.auth_test', return_value=auth_result, side_effect=side_effect), \
             patch('sources.services.SlackClient.joined_channels', return_value=channels if channels is not None else []):
            return self.client.post(self.url, body, format='json')

    def test_creates_connection(self):
        response = self.connect()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['provider'], 'SLACK')
        self.assertEqual(response.data['status'], 'CONNECTED')
        self.assertEqual(response.data['displayName'], '에코랩 워크스페이스')
        self.assertEqual(response.data['workspaceId'], 'T01ABCDEF')

    # 평문 저장이라 응답으로 새어 나가지 않는 것이 유일한 방어선이다.
    def test_response_never_contains_credentials(self):
        response = self.connect()
        body = str(response.data)

        self.assertNotIn(BOT_TOKEN, body)
        self.assertNotIn(SIGNING_SECRET, body)
        self.assertNotIn('botToken', response.data)
        self.assertNotIn('signingSecret', response.data)

    def test_stores_credentials(self):
        self.connect()
        connection = Connection.objects.get(company=self.company)

        self.assertEqual(connection.bot_token, BOT_TOKEN)
        self.assertEqual(connection.signing_secret, SIGNING_SECRET)

    # 키를 다시 넣으면 새 연결을 만들지 않고 교체한다.
    def test_reconnect_updates_in_place(self):
        self.connect()
        response = self.connect(payload={
            'provider': 'SLACK',
            'botToken': 'xoxb-rotated-token-999',
            'signingSecret': 'b' * 32,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Connection.objects.filter(company=self.company).count(), 1)
        self.assertEqual(Connection.objects.get(company=self.company).bot_token, 'xoxb-rotated-token-999')

    # 슬랙이 거절하면 아무것도 저장하지 않아야 한다.
    def test_invalid_token_saves_nothing(self):
        response = self.connect(side_effect=SlackError('invalid_auth'))

        self.assertEqual(response.status_code, 400)
        self.assertFalse(Connection.objects.exists())

    def test_malformed_token_rejected_before_slack_call(self):
        with patch('sources.views.SlackClient.auth_test') as auth_test:
            self.client.force_authenticate(user=self.owner)
            response = self.client.post(
                self.url,
                {'provider': 'SLACK', 'botToken': 'wrong-prefix', 'signingSecret': SIGNING_SECRET},
                format='json',
            )

        self.assertEqual(response.status_code, 400)
        auth_test.assert_not_called()

    # 한 워크스페이스가 두 회사에 붙으면 웹훅이 회사를 특정할 수 없다.
    def test_workspace_cannot_be_shared_across_companies(self):
        self.connect()

        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_owner = User.objects.create_user(email='other@example.com', password='pw', display_name='다른대표')
        Membership.objects.create(user=other_owner, company=other, role=Membership.Role.OWNER)

        self.client.force_authenticate(user=other_owner)
        with patch('sources.views.SlackClient.auth_test', return_value=AUTH_TEST_OK), \
             patch('sources.services.SlackClient.joined_channels', return_value=[]):
            response = self.client.post(
                f'/api/companies/{other.id}/source-connections',
                {'provider': 'SLACK', 'botToken': BOT_TOKEN, 'signingSecret': SIGNING_SECRET},
                format='json',
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Connection.objects.count(), 1)

    def test_member_cannot_connect(self):
        response = self.connect(user=self.member)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(Connection.objects.exists())

    def test_list_returns_connection(self):
        self.connect()
        self.client.force_authenticate(user=self.owner)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['items']), 1)
        self.assertEqual(response.data['items'][0]['displayName'], '에코랩 워크스페이스')
        self.assertEqual(response.data['items'][0]['resourceCount'], 0)
        self.assertNotIn(BOT_TOKEN, str(response.data))

    # 연동하면 채널을 바로 가져온다.
    def test_connect_syncs_channels(self):
        response = self.connect(channels=CHANNELS)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['resourceCount'], 2)
        self.assertEqual(
            set(Item.objects.values_list('label', flat=True)), {'#general', '#payment-api'}
        )

    # 채널 조회가 실패해도 연결은 살리고 원인을 남긴다.
    def test_channel_sync_failure_keeps_connection(self):
        self.client.force_authenticate(user=self.owner)
        with patch('sources.views.SlackClient.auth_test', return_value=AUTH_TEST_OK), \
             patch('sources.services.SlackClient.joined_channels', side_effect=SlackError('missing_scope')):
            response = self.client.post(
                self.url,
                {'provider': 'SLACK', 'botToken': BOT_TOKEN, 'signingSecret': SIGNING_SECRET},
                format='json',
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'ERROR')
        self.assertEqual(response.data['errorMessage'], 'missing_scope')
        self.assertTrue(Connection.objects.exists())


class ChannelSyncTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.owner = User.objects.create_user(email='owner@example.com', password='pw', display_name='대표')
        Membership.objects.create(user=self.owner, company=self.company, role=Membership.Role.OWNER)
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token=BOT_TOKEN
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}/source-connections/{self.connection.id}/channels'

    def sync(self, channels):
        with patch('sources.services.SlackClient.joined_channels', return_value=channels):
            return sync_slack_channels(self.connection)

    def test_creates_items(self):
        result = self.sync(CHANNELS)

        self.assertEqual(result, {'created': 2, 'updated': 0, 'removed': 0})
        self.assertEqual(Item.objects.filter(connection=self.connection).count(), 2)

    def test_resync_does_not_duplicate(self):
        self.sync(CHANNELS)
        result = self.sync(CHANNELS)

        self.assertEqual(result, {'created': 0, 'updated': 2, 'removed': 0})
        self.assertEqual(Item.objects.filter(connection=self.connection).count(), 2)

    # 대표가 지정한 지식공간 매핑이 재동기화로 날아가면 안 된다.
    def test_resync_preserves_scope_mapping(self):
        self.sync(CHANNELS)
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        item = Item.objects.get(external_id='C002')
        item.scope = scope
        item.is_scope_confirmed = True
        item.save()

        self.sync(CHANNELS)

        item.refresh_from_db()
        self.assertEqual(item.scope_id, scope.id)
        self.assertTrue(item.is_scope_confirmed)

    # 봇이 나간 채널은 지우지 않고 removed_at만 찍는다. 수집한 문서가 딸려 있기 때문.
    def test_left_channel_is_marked_removed(self):
        self.sync(CHANNELS)
        result = self.sync([CHANNELS[0]])

        self.assertEqual(result['removed'], 1)
        self.assertEqual(Item.objects.filter(connection=self.connection).count(), 2)
        self.assertIsNotNone(Item.objects.get(external_id='C002').removed_at)

    def test_rejoined_channel_is_restored(self):
        self.sync(CHANNELS)
        self.sync([CHANNELS[0]])
        self.sync(CHANNELS)

        self.assertIsNone(Item.objects.get(external_id='C002').removed_at)

    def test_list_excludes_removed(self):
        self.sync(CHANNELS)
        self.sync([CHANNELS[0]])

        response = self.client.get(self.base)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['items']), 1)
        self.assertEqual(response.data['items'][0]['label'], '#general')
        self.assertIsNone(response.data['items'][0]['scopeId'])
        self.assertFalse(response.data['items'][0]['isScopeConfirmed'])

    def test_sync_endpoint(self):
        with patch('sources.services.SlackClient.joined_channels', return_value=CHANNELS):
            response = self.client.post(f'{self.base}/sync')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['items']), 2)

    # 수동 재동기화는 실패를 그대로 알려 준다.
    def test_sync_endpoint_surfaces_slack_error(self):
        with patch('sources.services.SlackClient.joined_channels', side_effect=SlackError('missing_scope')):
            response = self.client.post(f'{self.base}/sync')

        self.assertEqual(response.status_code, 400)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, Connection.Status.ERROR)
        self.assertEqual(self.connection.error_message, 'missing_scope')

    def test_other_company_connection_is_not_reachable(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_owner = User.objects.create_user(email='other@example.com', password='pw', display_name='다른대표')
        Membership.objects.create(user=other_owner, company=other, role=Membership.Role.OWNER)

        self.client.force_authenticate(user=other_owner)
        response = self.client.get(
            f'/api/companies/{other.id}/source-connections/{self.connection.id}/channels'
        )

        self.assertEqual(response.status_code, 404)
