from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope

from .models import Connection, Identity, Item, RawDocument
from .services import register_joined_channels
from .slack import SlackError

BOT_TOKEN = 'xoxb-test-token-0123456789'
SIGNING_SECRET = 'a' * 32
AUTH_TEST_OK = {'ok': True, 'team': '에코랩 워크스페이스', 'team_id': 'T01ABCDEF', 'user': 'sai'}

# 봇 참여 2개 + 미참여 공개 1개 + 참여 중인 비공개 1개
JOINED = [
    {'id': 'C001', 'name': 'general', 'is_member': True, 'is_private': False},
    {'id': 'C002', 'name': 'payment-api', 'is_member': True, 'is_private': False},
]
NOT_JOINED_PUBLIC = {'id': 'C003', 'name': 'design', 'is_member': False, 'is_private': False}
JOINED_PRIVATE = {'id': 'G001', 'name': 'exec', 'is_member': True, 'is_private': True}
NOT_JOINED_PRIVATE = {'id': 'G002', 'name': 'secret', 'is_member': False, 'is_private': True}
ALL_CHANNELS = JOINED + [NOT_JOINED_PUBLIC, JOINED_PRIVATE]


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
             patch('sources.services.SlackClient.list_channels', return_value=channels if channels is not None else []):
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
             patch('sources.services.SlackClient.list_channels', return_value=[]):
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

    # 연동하면 봇이 이미 들어가 있는 채널만 등록된다. 미참여 채널은 대표가 고른다.
    def test_connect_registers_joined_channels_only(self):
        response = self.connect(channels=ALL_CHANNELS)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['resourceCount'], 3)
        self.assertEqual(
            set(Item.objects.values_list('external_id', flat=True)), {'C001', 'C002', 'G001'}
        )

    # 채널 조회가 실패해도 연결은 살리고 원인을 남긴다.
    def test_channel_registration_failure_keeps_connection(self):
        self.client.force_authenticate(user=self.owner)
        with patch('sources.views.SlackClient.auth_test', return_value=AUTH_TEST_OK), \
             patch('sources.services.SlackClient.list_channels', side_effect=SlackError('missing_scope')):
            response = self.client.post(
                self.url,
                {'provider': 'SLACK', 'botToken': BOT_TOKEN, 'signingSecret': SIGNING_SECRET},
                format='json',
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'ERROR')
        self.assertEqual(response.data['errorMessage'], 'missing_scope')
        self.assertTrue(Connection.objects.exists())

    # 키 교체는 채널 목록을 건드리지 않아야 한다. 대표가 제외한 채널이 되살아나면 안 되기 때문.
    def test_reconnect_does_not_touch_channels(self):
        self.connect(channels=JOINED)
        Item.objects.filter(external_id='C002').update(removed_at=timezone.now())

        self.connect(channels=JOINED, payload={
            'provider': 'SLACK', 'botToken': 'xoxb-rotated-999', 'signingSecret': 'b' * 32,
        })

        self.assertIsNotNone(Item.objects.get(external_id='C002').removed_at)


class ChannelTests(TestCase):
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

    def register(self, channels=None):
        with patch('sources.services.SlackClient.list_channels', return_value=channels or ALL_CHANNELS):
            return register_joined_channels(self.connection)

    def add(self, external_id, channels=None):
        with patch('sources.services.SlackClient.list_channels', return_value=channels or ALL_CHANNELS), \
             patch('sources.services.SlackClient.join_channel') as join:
            response = self.client.post(self.base, {'externalId': external_id}, format='json')
        return response, join

    # --- 등록 ---

    def test_register_only_joined(self):
        result = self.register()

        self.assertEqual(result['created'], 3)
        self.assertEqual(
            set(Item.objects.values_list('external_id', flat=True)), {'C001', 'C002', 'G001'}
        )

    def test_register_is_idempotent(self):
        self.register()
        result = self.register()

        self.assertEqual(result['created'], 0)
        self.assertEqual(Item.objects.count(), 3)

    # 대표가 제외한 채널을 재등록이 되살리면 안 된다.
    def test_register_does_not_revive_removed(self):
        self.register()
        Item.objects.filter(external_id='C001').update(removed_at=timezone.now())

        self.register()

        self.assertIsNotNone(Item.objects.get(external_id='C001').removed_at)

    # --- 추가 가능 목록 ---

    def test_available_excludes_registered(self):
        self.register()
        with patch('sources.services.SlackClient.list_channels', return_value=ALL_CHANNELS):
            response = self.client.get(f'{self.base}/available')

        self.assertEqual(response.status_code, 200)
        self.assertEqual([c['externalId'] for c in response.data['items']], ['C003'])
        self.assertFalse(response.data['items'][0]['isPrivate'])
        self.assertFalse(response.data['items'][0]['isMember'])

    def test_available_surfaces_slack_error(self):
        with patch('sources.services.SlackClient.list_channels', side_effect=SlackError('missing_scope')):
            response = self.client.get(f'{self.base}/available')

        self.assertEqual(response.status_code, 400)

    # --- 추가 ---

    # 미참여 공개 채널은 봇이 스스로 참여한 뒤 등록된다.
    def test_add_public_channel_joins_first(self):
        response, join = self.add('C003')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['label'], '#design')
        join.assert_called_once_with('C003')
        self.assertTrue(Item.objects.filter(external_id='C003').exists())

    # 이미 참여 중이면 join을 부르지 않는다. 채널에 불필요한 알림이 뜨지 않도록.
    def test_add_joined_channel_skips_join(self):
        response, join = self.add('C001')

        self.assertEqual(response.status_code, 201)
        join.assert_not_called()

    # 비공개 채널은 봇이 스스로 못 들어간다. 슬랙에서 초대해야 한다.
    def test_add_unjoined_private_channel_rejected(self):
        response, join = self.add('G002', channels=ALL_CHANNELS + [NOT_JOINED_PRIVATE])

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['externalId'][0], 'cannot_join_private_channel')
        join.assert_not_called()
        self.assertFalse(Item.objects.filter(external_id='G002').exists())

    # 이미 봇이 들어가 있는 비공개 채널은 join 없이 추가된다.
    def test_add_joined_private_channel_works(self):
        response, join = self.add('G001')

        self.assertEqual(response.status_code, 201)
        join.assert_not_called()

    def test_add_unknown_channel_rejected(self):
        response, _ = self.add('C999')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['externalId'][0], 'channel_not_found')

    # --- 제외 ---

    # 봇은 채널에 그대로 두고 Item만 제외한다.
    def test_remove_marks_only(self):
        self.register()
        item = Item.objects.get(external_id='C001')

        response = self.client.delete(f'{self.base}/{item.id}')

        self.assertEqual(response.status_code, 204)
        item.refresh_from_db()
        self.assertIsNotNone(item.removed_at)
        self.assertEqual(Item.objects.count(), 3)

    def test_removed_channel_is_hidden_from_list(self):
        self.register()
        item = Item.objects.get(external_id='C001')
        self.client.delete(f'{self.base}/{item.id}')

        response = self.client.get(self.base)

        # 목록은 label 오름차순. '#exec'(G001) < '#payment-api'(C002)
        self.assertEqual([c['externalId'] for c in response.data['items']], ['G001', 'C002'])

    def test_removed_channel_can_be_added_again(self):
        self.register()
        item = Item.objects.get(external_id='C001')
        self.client.delete(f'{self.base}/{item.id}')

        response, _ = self.add('C001')

        self.assertEqual(response.status_code, 201)
        item.refresh_from_db()
        self.assertIsNone(item.removed_at)
        self.assertEqual(Item.objects.count(), 3)

    def test_remove_twice_is_404(self):
        self.register()
        item = Item.objects.get(external_id='C001')
        self.client.delete(f'{self.base}/{item.id}')

        response = self.client.delete(f'{self.base}/{item.id}')

        self.assertEqual(response.status_code, 404)

    # --- 지식공간 연결 ---

    def test_map_channel_to_project_scope(self):
        self.register()
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        item = Item.objects.get(external_id='C002')

        response = self.client.patch(f'{self.base}/{item.id}', {'scopeId': scope.id}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['scopeId'], scope.id)
        self.assertEqual(response.data['scopeName'], '결제 시스템')
        self.assertEqual(response.data['scopeKind'], 'PROJECT')
        self.assertTrue(response.data['isScopeConfirmed'])

    # #general 처럼 회사 전반 규칙 범위로도 연결할 수 있어야 한다.
    def test_map_channel_to_company_scope(self):
        self.register()
        scope = CompanyScope.objects.create(
            company=self.company,
            kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY,
            name='Company',
        )
        item = Item.objects.get(external_id='C001')

        response = self.client.patch(f'{self.base}/{item.id}', {'scopeId': scope.id}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['scopeKind'], 'COMPANY')

    def test_unmap_channel(self):
        self.register()
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        item = Item.objects.get(external_id='C002')
        self.client.patch(f'{self.base}/{item.id}', {'scopeId': scope.id}, format='json')

        response = self.client.patch(f'{self.base}/{item.id}', {'scopeId': None}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['scopeId'])
        self.assertFalse(response.data['isScopeConfirmed'])

    # 다른 회사의 범위를 붙이면 회사 간 데이터가 섞인다.
    def test_other_company_scope_rejected(self):
        self.register()
        other = Company.objects.create(name='다른회사', code='TESTCODE9')
        foreign_scope = CompanyScope.objects.create(
            company=other, kind=CompanyScope.Kind.PROJECT, name='남의 프로젝트'
        )
        item = Item.objects.get(external_id='C002')

        response = self.client.patch(
            f'{self.base}/{item.id}', {'scopeId': foreign_scope.id}, format='json'
        )

        self.assertEqual(response.status_code, 400)
        item.refresh_from_db()
        self.assertIsNone(item.scope_id)

    def test_map_removed_channel_is_404(self):
        self.register()
        item = Item.objects.get(external_id='C001')
        self.client.delete(f'{self.base}/{item.id}')
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )

        response = self.client.patch(f'{self.base}/{item.id}', {'scopeId': scope.id}, format='json')

        self.assertEqual(response.status_code, 404)

    def test_member_cannot_map(self):
        self.register()
        member = User.objects.create_user(email='m@example.com', password='pw', display_name='팀원')
        Membership.objects.create(user=member, company=self.company, role=Membership.Role.MEMBER)
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        item = Item.objects.get(external_id='C002')

        self.client.force_authenticate(user=member)
        response = self.client.patch(f'{self.base}/{item.id}', {'scopeId': scope.id}, format='json')

        self.assertEqual(response.status_code, 403)

    # --- 스코프 보존 / 권한 ---

    # 대표가 지정한 지식공간 매핑이 재추가로 날아가면 안 된다.
    def test_scope_mapping_survives_readd(self):
        self.register()
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        item = Item.objects.get(external_id='C002')
        item.scope = scope
        item.is_scope_confirmed = True
        item.save()

        self.add('C002')

        item.refresh_from_db()
        self.assertEqual(item.scope_id, scope.id)
        self.assertTrue(item.is_scope_confirmed)

    def test_other_company_connection_is_not_reachable(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_owner = User.objects.create_user(email='other@example.com', password='pw', display_name='다른대표')
        Membership.objects.create(user=other_owner, company=other, role=Membership.Role.OWNER)

        self.client.force_authenticate(user=other_owner)
        response = self.client.get(
            f'/api/companies/{other.id}/source-connections/{self.connection.id}/channels'
        )

        self.assertEqual(response.status_code, 404)


USERS = [
    {'id': 'U001', 'name': 'kim', 'profile': {'display_name': '김대표', 'real_name': '김대표'}},
    {'id': 'U002', 'name': 'lee', 'profile': {'display_name': '', 'real_name': 'Lee'}},
    {'id': 'B001', 'name': 'githubbot', 'is_bot': True, 'profile': {'real_name': 'GitHub'}},
    {'id': 'U999', 'name': 'gone', 'deleted': True, 'profile': {'real_name': '퇴사자'}},
]
HISTORY = [
    {'ts': '1786700000.000100', 'user': 'U001', 'text': '배포는 제가 직접 돌립니다.'},
    {'ts': '1786700100.000200', 'user': 'U002', 'text': '넵 알겠습니다', 'reply_count': 2},
    {'ts': '1786700200.000300', 'user': 'U001', 'text': '', 'subtype': 'channel_join'},
    {'ts': '1786700300.000400', 'user': 'U001', 'text': '   '},
]
REPLIES = [
    {'ts': '1786700100.000200', 'user': 'U002', 'text': '넵 알겠습니다'},
    {'ts': '1786700150.000500', 'user': 'U001', 'text': '준비되면 스레드에 올려주세요'},
    {'ts': '1786700160.000600', 'user': 'U002', 'text': '확인했습니다'},
]
AUTH_WITH_URL = {**AUTH_TEST_OK, 'url': 'https://sai-project.slack.com/'}


class IngestionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.owner = User.objects.create_user(email='owner@example.com', password='pw', display_name='대표')
        Membership.objects.create(user=self.owner, company=self.company, role=Membership.Role.OWNER)
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token=BOT_TOKEN
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.url = f'/api/companies/{self.company.id}/ingestion-jobs'

    def ingest(self, history=None, replies=None, history_error=None, payload=None):
        with patch('sources.ingestion.SlackClient.auth_test', return_value=AUTH_WITH_URL), \
             patch('sources.ingestion.SlackClient.users_list', return_value=USERS), \
             patch('sources.ingestion.SlackClient.channel_history',
                   return_value=history if history is not None else HISTORY,
                   side_effect=history_error), \
             patch('sources.ingestion.SlackClient.thread_replies',
                   return_value=replies if replies is not None else REPLIES):
            return self.client.post(self.url, payload or {}, format='json')

    # --- 수집 ---

    def test_collects_messages_and_thread_replies(self):
        response = self.ingest()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], 'SUCCEEDED')
        self.assertEqual(response.data['progress'], 100)
        # 최상위 2건 + 스레드 답글 2건. 부모 중복과 빈 메시지는 제외.
        self.assertEqual(RawDocument.objects.count(), 4)

    def test_skips_system_and_empty_messages(self):
        self.ingest()
        stored = set(RawDocument.objects.values_list('external_ref', flat=True))

        self.assertNotIn('1786700200.000300', stored)  # channel_join
        self.assertNotIn('1786700300.000400', stored)  # 공백만

    def test_thread_reply_keeps_parent_reference(self):
        self.ingest()
        reply = RawDocument.objects.get(external_ref='1786700150.000500')

        self.assertEqual(reply.thread_ref, '1786700100.000200')
        self.assertIsNone(RawDocument.objects.get(external_ref='1786700000.000100').thread_ref)

    def test_stores_author_permalink_and_hash(self):
        self.ingest()
        document = RawDocument.objects.get(external_ref='1786700000.000100')

        self.assertEqual(document.author_identity.external_handle, '김대표')
        self.assertEqual(
            document.permalink,
            'https://sai-project.slack.com/archives/C001/p1786700000000100',
        )
        self.assertEqual(len(document.content_hash), 64)
        self.assertEqual(document.classified_as, RawDocument.ClassifiedAs.UNCLASSIFIED)

    # 같은 메시지를 두 번 수집해도 행이 늘어나면 안 된다.
    def test_reingest_is_idempotent(self):
        self.ingest()
        self.ingest()

        self.assertEqual(RawDocument.objects.count(), 4)

    # 원문이 바뀌면 내용과 해시가 갱신된다.
    def test_edited_message_is_updated(self):
        self.ingest()
        before = RawDocument.objects.get(external_ref='1786700000.000100').content_hash

        edited = [{**HISTORY[0], 'text': '배포는 이제 각자 하셔도 됩니다.'}]
        self.ingest(history=edited, replies=[])

        document = RawDocument.objects.get(external_ref='1786700000.000100')
        self.assertEqual(document.raw_text, '배포는 이제 각자 하셔도 됩니다.')
        self.assertNotEqual(document.content_hash, before)

    def test_updates_item_counters(self):
        self.ingest()
        self.item.refresh_from_db()

        self.assertEqual(self.item.item_count, 4)
        self.assertIsNotNone(self.item.last_synced_at)

    # --- Identity ---

    def test_builds_identities(self):
        self.ingest()

        self.assertEqual(Identity.objects.count(), 3)  # 삭제된 사용자 제외
        self.assertTrue(Identity.objects.get(external_user_id='B001').is_bot)
        # display_name이 비면 real_name으로 대체한다.
        self.assertEqual(Identity.objects.get(external_user_id='U002').external_handle, 'Lee')

    # --- 작업 상태 ---

    def test_job_detail_is_pollable(self):
        job_id = self.ingest().data['id']

        response = self.client.get(f'{self.url}/{job_id}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'SUCCEEDED')
        self.assertEqual(response.data['documentCount'], 4)
        self.assertIsNotNone(response.data['completedAt'])

    def test_channel_failure_is_recorded(self):
        response = self.ingest(history_error=SlackError('not_in_channel'))

        self.assertEqual(response.data['status'], 'FAILED')
        self.assertEqual(response.data['errors'][0]['code'], 'not_in_channel')
        self.assertEqual(RawDocument.objects.count(), 0)

    # 채널 하나는 성공, 하나는 실패면 PARTIAL.
    def test_partial_failure(self):
        other = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C002', label='#payment',
        )

        def history(channel_id, max_messages=None):
            if channel_id == other.external_id:
                raise SlackError('not_in_channel')
            return HISTORY

        with patch('sources.ingestion.SlackClient.auth_test', return_value=AUTH_WITH_URL), \
             patch('sources.ingestion.SlackClient.users_list', return_value=USERS), \
             patch('sources.ingestion.SlackClient.channel_history', side_effect=history), \
             patch('sources.ingestion.SlackClient.thread_replies', return_value=REPLIES):
            response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.data['status'], 'PARTIAL')
        self.assertEqual(len(response.data['errors']), 1)

    def test_only_targets_selected_channels(self):
        Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C002', label='#payment',
        )

        response = self.ingest(payload={'itemIds': [self.item.id]})

        self.assertEqual(response.data['itemIds'], [self.item.id])

    def test_removed_channel_is_not_ingested(self):
        self.item.removed_at = timezone.now()
        self.item.save()

        response = self.ingest()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(RawDocument.objects.count(), 0)

    def test_member_cannot_ingest(self):
        member = User.objects.create_user(email='m@example.com', password='pw', display_name='팀원')
        Membership.objects.create(user=member, company=self.company, role=Membership.Role.MEMBER)
        self.client.force_authenticate(user=member)

        response = self.ingest()

        self.assertEqual(response.status_code, 403)
