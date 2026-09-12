from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope

from .classifier import (
    CLASSIFIER_VERSION,
    ClassificationResult,
    MessageLabel,
    classify_documents,
)
from .ingestion import PROGRESS_COLLECTED
from .local_ingestion import ingest_local_file, run_local_ingestion
from .models import Connection, Identity, IngestionJob, Item, RawDocument
from .services import register_joined_channels
from .slack import SlackError
from .text import normalize_slack_text

BOT_TOKEN = 'xoxb-test-token-0123456789'
SIGNING_SECRET = 'a' * 32
AUTH_TEST_OK = {'ok': True, 'team': '에코랩 워크스페이스', 'team_id': 'T01ABCDEF', 'user': 'sai'}

# 봇 참여 2개 + 미참여 공개 1개 + 참여 중인 비공개 1개
JOINED = [
    {'id': 'C001', 'name': 'general', 'is_member': True, 'is_private': False},
    {'id': 'C002', 'name': 'payment-api', 'is_member': True, 'is_private': False},
]
NOT_JOINED_PUBLIC = {
    'id': 'C003', 'name': 'design', 'is_member': False,
    'is_private': False, 'num_members': 7,
}
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
        with patch('sources.services.SlackClient.auth_test', return_value=auth_result, side_effect=side_effect), \
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
        with patch('sources.services.SlackClient.auth_test') as auth_test:
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
        with patch('sources.services.SlackClient.auth_test', return_value=AUTH_TEST_OK), \
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
        with patch('sources.services.SlackClient.auth_test', return_value=AUTH_TEST_OK), \
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
        self.assertEqual(response.data['items'][0]['memberCount'], 7)

    def test_available_channel_without_member_count_returns_null(self):
        channel = {**NOT_JOINED_PUBLIC}
        channel.pop('num_members')
        with patch('sources.services.SlackClient.list_channels', return_value=[channel]):
            response = self.client.get(f'{self.base}/available')

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data['items'][0]['memberCount'])

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
        self.assertEqual(response.data['error']['field'], 'externalId')
        self.assertEqual(response.data['error']['message'], 'cannot_join_private_channel')
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
        self.assertEqual(response.data['error']['field'], 'externalId')
        self.assertEqual(response.data['error']['message'], 'channel_not_found')

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
    # 슬랙 앱이 올린 메시지. user 가 있어도 봇이므로 수집 대상이 아니다.
    {'ts': '1786700400.000500', 'user': 'U0BPPRH9HTK', 'bot_id': 'B001', 'text': 'SAI가 보낸 확인 질문'},
]
REPLIES = [
    {'ts': '1786700100.000200', 'user': 'U002', 'text': '넵 알겠습니다'},
    {'ts': '1786700150.000500', 'user': 'U001', 'text': '준비되면 스레드에 올려주세요'},
    {'ts': '1786700160.000600', 'user': 'U002', 'text': '확인했습니다'},
]
AUTH_WITH_URL = {**AUTH_TEST_OK, 'url': 'https://sai-project.slack.com/'}


# 실제 서비스에서는 워커가 큐에서 꺼내 처리한다.
# 이 클래스는 수집 결과를 검증하므로 요청 안에서 바로 돌린다.
@override_settings(INGESTION_RUN_INLINE=True)
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

    def ingest(self, history=None, replies=None, history_error=None, payload=None, users=None,
               on_classify=None):
        # 분류는 여기서 검증 대상이 아니다. 막지 않으면 실제 OpenAI를 호출한다.
        with patch('sources.ingestion.SlackClient.auth_test', return_value=AUTH_WITH_URL), \
             patch('sources.ingestion.SlackClient.users_list',
                   return_value=USERS if users is None else users), \
             patch('sources.ingestion.SlackClient.channel_history',
                   return_value=history if history is not None else HISTORY,
                   side_effect=history_error), \
             patch('sources.ingestion.SlackClient.thread_replies',
                   return_value=replies if replies is not None else REPLIES), \
             patch('sources.ingestion.classify_documents',
                   return_value=(0, []), side_effect=on_classify) as classify, \
             patch('sources.ingestion.sync_chunks', return_value=(0, [])) as chunks, \
             patch('sources.ingestion.draft_entries', return_value=([], [])) as draft, \
             patch('sources.ingestion.generate_cards', return_value=([], [])) as cards:
            self.classify_mock = classify
            self.chunk_mock = chunks
            self.draft_mock = draft
            self.card_mock = cards
            return self.client.post(self.url, payload or {}, format='json')

    # --- 수집 ---

    def test_collects_messages_and_thread_replies(self):
        response = self.ingest()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['status'], 'SUCCEEDED')
        self.assertEqual(response.data['progress'], 100)
        # 최상위 2건 + 스레드 답글 2건. 부모 중복과 빈 메시지는 제외.
        self.assertEqual(RawDocument.objects.count(), 4)

    def test_skips_system_and_empty_messages(self):
        self.ingest()
        stored = set(RawDocument.objects.values_list('external_ref', flat=True))

        self.assertNotIn('1786700200.000300', stored)  # channel_join
        self.assertNotIn('1786700300.000400', stored)  # 공백만

    # 백필도 웹훅과 같은 규칙을 써야 한다. SAI가 보낸 글이 되먹임되면 안 된다.
    def test_skips_bot_messages(self):
        self.ingest()
        stored = set(RawDocument.objects.values_list('external_ref', flat=True))

        self.assertNotIn('1786700400.000500', stored)

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

    # --- SAI 계정 연결 ---

    # 슬랙 이메일과 가입 이메일이 같으면 이어 준다. 지시 카드의 담당자가 여기서 정해진다.
    def test_links_identity_to_user_by_email(self):
        member = User.objects.create_user(
            email='kim@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(
            user=member, company=self.company, role=Membership.Role.MEMBER
        )

        self.ingest(users=[{**USERS[0], 'profile': {**USERS[0]['profile'], 'email': 'kim@example.com'}}])

        self.assertEqual(Identity.objects.get(external_user_id='U001').user, member)

    # 슬랙 이메일 대소문자가 달라도 같은 사람이다.
    def test_email_match_is_case_insensitive(self):
        member = User.objects.create_user(
            email='kim@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(user=member, company=self.company, role=Membership.Role.MEMBER)

        self.ingest(users=[{**USERS[0], 'profile': {'email': 'KIM@Example.COM', 'real_name': '김'}}])

        self.assertEqual(Identity.objects.get(external_user_id='U001').user, member)

    # 다른 회사 사용자와 이메일이 겹쳐도 연결하면 안 된다.
    def test_does_not_link_user_from_another_company(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE9')
        outsider = User.objects.create_user(
            email='kim@example.com', password='pw', display_name='남의 회사 김씨'
        )
        Membership.objects.create(user=outsider, company=other, role=Membership.Role.MEMBER)

        self.ingest(users=[{**USERS[0], 'profile': {'email': 'kim@example.com', 'real_name': '김'}}])

        self.assertIsNone(Identity.objects.get(external_user_id='U001').user)

    # 퇴사자에게 새 지시가 배정되면 안 된다.
    def test_does_not_link_left_member(self):
        gone = User.objects.create_user(email='gone@example.com', password='pw', display_name='퇴사자')
        Membership.objects.create(
            user=gone, company=self.company, role=Membership.Role.MEMBER, left_at=timezone.now()
        )

        self.ingest(users=[{**USERS[0], 'profile': {'email': 'gone@example.com', 'real_name': 'G'}}])

        self.assertIsNone(Identity.objects.get(external_user_id='U001').user)

    def test_no_email_leaves_user_null(self):
        self.ingest()

        self.assertIsNone(Identity.objects.get(external_user_id='U001').user)

    # 매칭 실패가 기존 연결을 끊으면 안 된다. 손으로 이어 둔 것을 지울 수 있다.
    def test_existing_link_survives_when_email_missing(self):
        member = User.objects.create_user(email='kim@example.com', password='pw', display_name='김')
        Membership.objects.create(user=member, company=self.company, role=Membership.Role.MEMBER)
        Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U001', user=member,
        )

        self.ingest()

        self.assertEqual(Identity.objects.get(external_user_id='U001').user, member)

    # --- 작업 상태 ---

    def test_job_detail_is_pollable(self):
        job_id = self.ingest().data['id']

        response = self.client.get(f'{self.url}/{job_id}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'SUCCEEDED')
        self.assertEqual(response.data['documentCount'], 4)
        self.assertIsNotNone(response.data['completedAt'])

    # 수집만 끝나도 100이 되면 화면의 진행바가 멈춘 것처럼 보인다.
    # 분류가 시작되는 시점의 진행률이 100보다 작아야 한다.
    def test_progress_reflects_whole_pipeline(self):
        seen = []

        def record(company_id):
            seen.append(IngestionJob.objects.get(company_id=company_id).progress)
            return 0, []

        response = self.ingest(on_classify=record)

        self.assertEqual(seen, [PROGRESS_COLLECTED])
        self.assertEqual(response.data['progress'], 100)

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
             patch('sources.ingestion.SlackClient.thread_replies', return_value=REPLIES), \
             patch('sources.ingestion.classify_documents', return_value=(0, [])), \
             patch('sources.ingestion.sync_chunks', return_value=(0, [])), \
             patch('sources.ingestion.draft_entries', return_value=([], [])), \
             patch('sources.ingestion.generate_cards', return_value=([], [])):
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
        self.assertEqual(response.data['error']['field'], 'itemIds')
        self.assertEqual(response.data['error']['message'], 'no item registered')
        self.assertEqual(RawDocument.objects.count(), 0)

    # Swagger 기본 예시 [0] 을 그대로 보내는 일이 흔하다. 원인이 구분되어야 한다.
    def test_unknown_item_id_reports_no_match(self):
        response = self.ingest(payload={'itemIds': [0]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['field'], 'itemIds')
        self.assertEqual(response.data['error']['message'], 'no matching item')

    def test_empty_item_ids_rejected(self):
        response = self.ingest(payload={'itemIds': []})

        self.assertEqual(response.status_code, 400)

    # 본문을 비워 보내면 등록된 채널 전체가 대상이다. Swagger 기본 사용 방식.
    def test_empty_body_targets_all_channels(self):
        other = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C002', label='#payment',
        )

        response = self.ingest(payload={})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(set(response.data['itemIds']), {self.item.id, other.id})

    def test_member_cannot_ingest(self):
        member = User.objects.create_user(email='m@example.com', password='pw', display_name='팀원')
        Membership.objects.create(user=member, company=self.company, role=Membership.Role.MEMBER)
        self.client.force_authenticate(user=member)

        response = self.ingest()

        self.assertEqual(response.status_code, 403)

    # --- 분류 연동 ---

    def test_ingestion_triggers_classification_then_drafting(self):
        self.ingest()

        self.classify_mock.assert_called_once_with(self.company.id)
        self.draft_mock.assert_called_once_with(self.company)

    def test_entry_count_records_drafts(self):
        with patch('sources.ingestion.SlackClient.auth_test', return_value=AUTH_WITH_URL), \
             patch('sources.ingestion.SlackClient.users_list', return_value=USERS), \
             patch('sources.ingestion.SlackClient.channel_history', return_value=HISTORY), \
             patch('sources.ingestion.SlackClient.thread_replies', return_value=REPLIES), \
             patch('sources.ingestion.classify_documents', return_value=(0, [])), \
             patch('sources.ingestion.sync_chunks', return_value=(0, [])), \
             patch('sources.ingestion.generate_cards', return_value=([], [])), \
             patch('sources.ingestion.draft_entries', return_value=([1, 2, 3], [])):
            response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.data['candidateCount'], 3)

    # 분류가 실패하면 초안 생성도 건너뛴다. 라벨 없는 원문으로 규칙을 쓸 수 없다.
    def test_no_drafting_when_classification_failed(self):
        with patch('sources.ingestion.SlackClient.auth_test', return_value=AUTH_WITH_URL), \
             patch('sources.ingestion.SlackClient.users_list', return_value=USERS), \
             patch('sources.ingestion.SlackClient.channel_history', return_value=HISTORY), \
             patch('sources.ingestion.SlackClient.thread_replies', return_value=REPLIES), \
             patch('sources.ingestion.classify_documents',
                   side_effect=ImproperlyConfigured('no key')), \
             patch('sources.ingestion.sync_chunks') as chunks, \
             patch('sources.ingestion.generate_cards') as cards, \
             patch('sources.ingestion.draft_entries') as draft:
            self.client.post(self.url, {}, format='json')

        draft.assert_not_called()
        chunks.assert_not_called()
        cards.assert_not_called()

    # 분류가 실패해도 수집한 원문은 남고 작업만 PARTIAL이 된다.
    def test_classification_failure_is_partial(self):
        with patch('sources.ingestion.SlackClient.auth_test', return_value=AUTH_WITH_URL), \
             patch('sources.ingestion.SlackClient.users_list', return_value=USERS), \
             patch('sources.ingestion.SlackClient.channel_history', return_value=HISTORY), \
             patch('sources.ingestion.SlackClient.thread_replies', return_value=REPLIES), \
             patch('sources.ingestion.classify_documents',
                   side_effect=ImproperlyConfigured('no key')):
            response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.data['status'], 'PARTIAL')
        self.assertEqual(response.data['errors'][0]['code'], 'openai_not_configured')
        self.assertEqual(RawDocument.objects.count(), 4)

    # 수집이 통째로 실패하면 분류를 시도하지 않는다.
    def test_no_classification_when_collection_failed(self):
        self.ingest(history_error=SlackError('not_in_channel'))

        self.classify_mock.assert_not_called()


class LocalFileIngestionApiTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE2')
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.member = User.objects.create_user(
            email='member@example.com', password='pw', display_name='팀원'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.company_scope = CompanyScope.objects.create(
            company=self.company,
            kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY,
            name='Company',
        )
        self.people_scope = CompanyScope.objects.create(
            company=self.company,
            kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PEOPLE,
            name='People',
        )
        self.project = CompanyScope.objects.create(
            company=self.company,
            kind=CompanyScope.Kind.PROJECT,
            name='payment-api',
        )
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.LOCAL
        )
        self.item = Item.objects.create(
            company=self.company,
            connection=self.connection,
            external_id='file-1',
            label='개발규칙.pdf',
            storage_key='companies/1/local/file-1.pdf',
            mime_type='application/pdf',
            byte_size=100,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.url = f'/api/companies/{self.company.id}/ingestion-jobs'

    def test_local_collect_sets_selected_project_scope(self):
        response = self.client.post(
            self.url,
            {
                'provider': 'LOCAL',
                'itemIds': [self.item.id],
                'scopeId': self.project.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 202)
        self.item.refresh_from_db()
        self.assertEqual(self.item.scope, self.project)
        self.assertTrue(self.item.is_scope_confirmed)

    def test_local_collect_without_scope_uses_company_wide(self):
        response = self.client.post(
            self.url,
            {'provider': 'LOCAL', 'itemIds': [self.item.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 202)
        self.item.refresh_from_db()
        self.assertEqual(self.item.scope, self.company_scope)

    def test_local_collect_rejects_company_category_scope(self):
        response = self.client.post(
            self.url,
            {
                'provider': 'LOCAL',
                'itemIds': [self.item.id],
                'scopeId': self.people_scope.id,
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.scope)

    def test_direct_upload_stores_file_and_queues_collection(self):
        file_obj = SimpleUploadedFile(
            'dev-rules.txt',
            '백엔드 배포는 AWS EC2로 진행합니다.'.encode(),
            content_type='text/plain',
        )

        with patch('sources.services.upload_file') as upload:
            response = self.client.post(
                f'/api/companies/{self.company.id}/source-files',
                {'file': file_obj, 'scopeId': self.project.id},
                format='multipart',
            )

        self.assertEqual(response.status_code, 201)
        item = Item.objects.get(id=response.data['sourceFile']['id'])
        self.assertEqual(item.scope, self.project)
        self.assertTrue(item.is_scope_confirmed)
        self.assertIsNone(response.data['uploadTarget'])
        self.assertEqual(response.data['ingestionJob']['kind'], IngestionJob.Kind.COLLECT)
        self.assertEqual(response.data['ingestionJob']['itemIds'], [item.id])
        upload.assert_called_once()

    def test_local_file_status_is_error_when_ai_processing_failed(self):
        self.item.last_synced_at = timezone.now()
        self.item.save(update_fields=['last_synced_at'])
        IngestionJob.objects.create(
            company=self.company,
            connection=self.connection,
            kind=IngestionJob.Kind.COLLECT,
            status=IngestionJob.Status.PARTIAL,
            item_ids=[self.item.id],
            errors=[{'scope': 'draft', 'code': 'openai_not_configured'}],
        )

        response = self.client.get(f'/api/companies/{self.company.id}/source-files')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['items'][0]['status'], 'ERROR')

    def test_local_file_documents_are_instruction_candidates(self):
        self.item.label = '개발규칙.txt'
        self.item.mime_type = 'text/plain'
        self.item.save(update_fields=['label', 'mime_type'])
        with patch(
            'sources.local_ingestion.download_file',
            return_value='작업 내용과 진행 상황을 팀원에게 공유합니다.'.encode(),
        ):
            ingest_local_file(self.item)

        document = RawDocument.objects.get(item=self.item)
        self.assertEqual(document.classified_as, RawDocument.ClassifiedAs.INSTRUCTION)
        self.assertEqual(document.classifier_version, CLASSIFIER_VERSION)

    def test_local_file_recollect_restores_instruction_candidate(self):
        self.item.label = '개발규칙.txt'
        self.item.mime_type = 'text/plain'
        self.item.save(update_fields=['label', 'mime_type'])
        RawDocument.objects.create(
            company=self.company,
            item=self.item,
            external_ref=f'file:{self.item.external_id}:0',
            raw_text='기존 문서',
            content_hash='a' * 64,
            classified_as=RawDocument.ClassifiedAs.CONTEXT,
            classifier_version='clf-old',
        )

        with patch(
            'sources.local_ingestion.download_file',
            return_value='기존 문서'.encode(),
        ):
            ingest_local_file(self.item)

        document = RawDocument.objects.get(item=self.item)
        self.assertEqual(document.classified_as, RawDocument.ClassifiedAs.INSTRUCTION)
        self.assertEqual(document.classifier_version, CLASSIFIER_VERSION)

    def test_local_collect_drafts_file_before_heavy_processing(self):
        self.item.label = '개발규칙.txt'
        self.item.mime_type = 'text/plain'
        self.item.save(update_fields=['label', 'mime_type'])
        job = IngestionJob.objects.create(
            company=self.company,
            connection=self.connection,
            kind=IngestionJob.Kind.COLLECT,
            item_ids=[self.item.id],
        )

        with patch(
            'sources.local_ingestion.download_file',
            return_value='작업 내용과 진행 상황을 팀원에게 공유합니다.'.encode(),
        ), patch('sources.ingestion.draft_entries', return_value=([1], [])) as draft, \
             patch('sources.ingestion.classify_documents') as classify, \
             patch('sources.ingestion.sync_chunks') as chunks, \
             patch('sources.ingestion.generate_cards') as cards:
            result = run_local_ingestion(job, self.connection)

        self.assertEqual(result.status, IngestionJob.Status.SUCCEEDED)
        self.assertEqual(result.entry_count, 1)
        draft.assert_called_once()
        self.assertIn('documents', draft.call_args.kwargs)
        classify.assert_not_called()
        chunks.assert_not_called()
        cards.assert_not_called()

    def test_local_file_open_redirects_to_presigned_url(self):
        self.client.force_authenticate(user=self.member)
        with patch(
            'sources.services.create_download_url',
            return_value='https://s3.example.com/file',
        ) as presign:
            response = self.client.get(
                f'/api/companies/{self.company.id}/source-files/{self.item.id}/open'
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'https://s3.example.com/file')
        presign.assert_called_once_with(self.item.storage_key, self.item.label)


class NormalizeSlackTextTests(SimpleTestCase):
    CHANNELS = {'C001': '#dev'}
    USERS = {'U001': '조상원'}

    def normalize(self, text):
        return normalize_slack_text(text, self.CHANNELS, self.USERS)

    def test_channel_mention_becomes_label(self):
        self.assertEqual(self.normalize('<#C001> 에 공지'), '#dev 에 공지')

    def test_channel_mention_uses_inline_name_when_present(self):
        self.assertEqual(self.normalize('<#C999|payment> 확인'), '#payment 확인')

    def test_unknown_channel_falls_back(self):
        self.assertEqual(self.normalize('<#C999> 확인'), '#채널 확인')

    def test_user_mention(self):
        self.assertEqual(self.normalize('<@U001> 님 확인 부탁'), '@조상원 님 확인 부탁')

    # <url|표시텍스트> 는 표시텍스트만 남긴다. URL이 두 번 들어가는 걸 막는다.
    def test_link_with_label(self):
        self.assertEqual(
            self.normalize('문서 <https://example.com/a|여기> 참고'), '문서 여기 참고'
        )

    def test_link_without_label(self):
        self.assertEqual(
            self.normalize('<https://example.com/a> 참고'), 'https://example.com/a 참고'
        )

    def test_special_mention(self):
        self.assertEqual(self.normalize('<!here> 공지합니다'), '@here 공지합니다')

    def test_html_entities_are_unescaped(self):
        self.assertEqual(self.normalize('&gt; 인용문 &amp; 기타'), '> 인용문 & 기타')

    # 이스케이프를 먼저 풀면 &lt;#C001&gt; 이 진짜 멘션처럼 보인다. 순서가 중요하다.
    def test_escaped_markup_is_not_treated_as_markup(self):
        self.assertEqual(self.normalize('&lt;#C001&gt; 는 채널 문법입니다'), '<#C001> 는 채널 문법입니다')

    def test_empty(self):
        self.assertEqual(normalize_slack_text(''), '')
        self.assertEqual(normalize_slack_text(None), '')


class ClassifierTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token=BOT_TOKEN
        )
        self.item = Item.objects.create(
            company=self.company, connection=connection, external_id='C001', label='#dev'
        )
        identity = Identity.objects.create(
            company=self.company, connection=connection,
            external_user_id='U001', external_handle='조상원',
        )
        self.parent = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref='100.1',
            author_identity=identity, raw_text='PR 리뷰 기준 정할까요?', content_hash='a' * 64,
            occurred_at=timezone.now(),
        )
        self.reply = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref='100.2', thread_ref='100.1',
            author_identity=identity, raw_text='승인 1명으로 하죠', content_hash='b' * 64,
            occurred_at=timezone.now(),
        )

    def run_classify(self, labels):
        parsed = ClassificationResult(labels=[MessageLabel(**item) for item in labels])
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )
        with patch('sources.classifier.OpenAI') as client:
            client.return_value.chat.completions.parse.return_value = completion
            result = classify_documents(self.company.id)
            call = client.return_value.chat.completions.parse.call_args
        return result, call

    @override_settings(OPENAI_API_KEY='test-key')
    def test_stores_labels_and_version(self):
        (count, errors), _ = self.run_classify(
            [{'index': 0, 'label': 'AMBIGUOUS'}, {'index': 1, 'label': 'INSTRUCTION'}]
        )

        self.assertEqual((count, errors), (2, []))
        self.parent.refresh_from_db()
        self.reply.refresh_from_db()
        self.assertEqual(self.parent.classified_as, 'AMBIGUOUS')
        self.assertEqual(self.reply.classified_as, 'INSTRUCTION')
        self.assertEqual(self.reply.classifier_version, CLASSIFIER_VERSION)

    # 스레드 답글은 부모 발언이 있어야 의미가 잡힌다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_thread_reply_prompt_includes_parent(self):
        _, call = self.run_classify(
            [{'index': 0, 'label': 'AMBIGUOUS'}, {'index': 1, 'label': 'INSTRUCTION'}]
        )
        prompt = call.kwargs['messages'][1]['content']

        self.assertIn('parent: PR 리뷰 기준 정할까요?', prompt)
        self.assertIn('채널=#dev', prompt)
        self.assertIn('작성자=조상원', prompt)

    # 이미 현재 버전으로 분류된 것은 다시 부르지 않는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_skips_already_classified(self):
        self.run_classify([{'index': 0, 'label': 'CONTEXT'}, {'index': 1, 'label': 'CONTEXT'}])
        (count, _), _ = self.run_classify([])

        self.assertEqual(count, 0)

    # 프롬프트 버전이 오르면 다시 분류한다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_old_version_is_reclassified(self):
        RawDocument.objects.update(classified_as='CONTEXT', classifier_version='clf-v0')
        (count, _), _ = self.run_classify(
            [{'index': 0, 'label': 'INSTRUCTION'}, {'index': 1, 'label': 'INSTRUCTION'}]
        )

        self.assertEqual(count, 2)

    @override_settings(OPENAI_API_KEY='')
    def test_missing_api_key_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            classify_documents(self.company.id)

    # 응답에 빠진 index는 누락분만 fallback 모델에 다시 보내 복구한다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_missing_index_is_retried(self):
        (count, _), _ = self.run_classify([{'index': 0, 'label': 'CONTEXT'}])

        self.reply.refresh_from_db()
        self.assertEqual(count, 2)
        self.assertEqual(self.reply.classified_as, RawDocument.ClassifiedAs.CONTEXT)

    @override_settings(OPENAI_API_KEY='test-key')
    def test_failed_missing_index_retry_keeps_completed_labels(self):
        parsed = ClassificationResult(labels=[
            MessageLabel(index=0, label='CONTEXT')
        ])
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )
        with patch('sources.classifier.OpenAI') as client:
            client.return_value.chat.completions.parse.side_effect = [
                completion,
                ValueError('fallback failed'),
            ]
            count, errors = classify_documents(self.company.id)

        self.parent.refresh_from_db()
        self.reply.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertEqual(self.parent.classified_as, RawDocument.ClassifiedAs.CONTEXT)
        self.assertEqual(self.reply.classified_as, RawDocument.ClassifiedAs.UNCLASSIFIED)
        self.assertEqual(errors[0]['scope'], 'classify_retry')
