import hashlib
import hmac
import json
import time

from django.test import TestCase, override_settings

from companies.models import Company

from .models import Connection, Identity, Item, RawDocument

TEAM_ID = 'T0BPRGDGKV2'
SECRET = 'company-signing-secret'
OTHER_SECRET = 'someone-elses-secret'
URL = '/api/slack/events/'


def sign(secret, timestamp, body):
    base = b'v0:' + timestamp.encode() + b':' + body

    return 'v0=' + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


@override_settings(SLACK_SIGNING_SECRET='app-level-secret')
class SlackWebhookTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK,
            external_workspace_id=TEAM_ID, bot_token='xoxb-test', signing_secret=SECRET,
            workspace_url='https://eco.slack.com/',
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev',
        )

    def post(self, payload, secret=SECRET, timestamp=None, signature=None, body=None):
        body = body if body is not None else json.dumps(payload).encode()
        timestamp = timestamp if timestamp is not None else str(int(time.time()))
        # signature='' 는 '헤더 없음' 테스트다. falsy 검사로 서명해 버리면 안 된다.
        if signature is None:
            signature = sign(secret, timestamp, body)
        return self.client.post(
            URL, data=body, content_type='application/json',
            headers={
                'x-slack-request-timestamp': timestamp,
                'x-slack-signature': signature,
            },
        )

    def event(self, **overrides):
        payload = {
            'type': 'event_callback',
            'team_id': TEAM_ID,
            'event': {
                'type': 'message', 'channel': 'C001', 'user': 'U001',
                'text': '배포는 금요일에 하지 않습니다', 'ts': '1786800000.000100',
                **overrides,
            },
        }
        return self.post(payload)

    # --- URL 검증 ---

    def test_url_verification_with_app_secret(self):
        response = self.post(
            {'type': 'url_verification', 'challenge': 'abc123'}, secret='app-level-secret'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['challenge'], 'abc123')

    # 회사마다 자기 슬랙 앱을 만들면 시크릿도 회사마다 다르다.
    def test_url_verification_with_company_secret(self):
        response = self.post({'type': 'url_verification', 'challenge': 'abc123'}, secret=SECRET)

        self.assertEqual(response.status_code, 200)

    def test_url_verification_rejects_unknown_secret(self):
        response = self.post(
            {'type': 'url_verification', 'challenge': 'abc123'}, secret=OTHER_SECRET
        )

        self.assertEqual(response.status_code, 403)

    def test_url_verification_without_challenge(self):
        response = self.post({'type': 'url_verification'}, secret=SECRET)

        self.assertEqual(response.json()['challenge'], '')

    # --- 서명 검증 ---

    def test_saves_message(self):
        response = self.event()

        self.assertEqual(response.status_code, 200)
        document = RawDocument.objects.get()
        self.assertEqual(document.raw_text, '배포는 금요일에 하지 않습니다')
        self.assertEqual(document.item, self.item)
        self.assertEqual(document.company, self.company)
        self.assertEqual(len(document.content_hash), 64)
        self.assertEqual(
            document.permalink, 'https://eco.slack.com/archives/C001/p1786800000000100'
        )

    # 남의 team_id를 적어도 그 회사 시크릿을 모르면 통과할 수 없다.
    def test_wrong_secret_is_rejected(self):
        payload = {'type': 'event_callback', 'team_id': TEAM_ID,
                   'event': {'type': 'message', 'channel': 'C001', 'user': 'U001',
                             'text': '위조', 'ts': '1786800000.000100'}}

        response = self.post(payload, secret=OTHER_SECRET)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(RawDocument.objects.exists())

    def test_unknown_team_is_rejected(self):
        payload = {'type': 'event_callback', 'team_id': 'T_UNKNOWN',
                   'event': {'type': 'message', 'channel': 'C001', 'user': 'U001',
                             'text': '아무개', 'ts': '1786800000.000100'}}

        response = self.post(payload)

        self.assertEqual(response.status_code, 403)

    # 숫자가 아닌 타임스탬프로 500이 나면 안 된다.
    def test_malformed_timestamp_is_403_not_500(self):
        response = self.post({'type': 'event_callback', 'team_id': TEAM_ID}, timestamp='abc')

        self.assertEqual(response.status_code, 403)

    def test_replay_is_rejected(self):
        old = str(int(time.time()) - 10_000)

        response = self.post({'type': 'event_callback', 'team_id': TEAM_ID}, timestamp=old)

        self.assertEqual(response.status_code, 403)

    def test_missing_signature_is_rejected(self):
        response = self.post({'type': 'event_callback', 'team_id': TEAM_ID}, signature='')

        self.assertEqual(response.status_code, 403)

    def test_malformed_json_is_rejected(self):
        response = self.post(None, body=b'not json')

        self.assertEqual(response.status_code, 403)

    def test_get_is_not_allowed(self):
        self.assertEqual(self.client.get(URL).status_code, 405)

    # --- 저장 규칙 ---

    # 슬랙은 3초 안에 200을 못 받으면 같은 이벤트를 다시 보낸다.
    def test_retry_does_not_duplicate(self):
        self.event()
        self.event()

        self.assertEqual(RawDocument.objects.count(), 1)

    def test_edited_text_updates_in_place(self):
        self.event()
        self.event(text='배포는 금요일 오후에만 하지 않습니다')

        document = RawDocument.objects.get()
        self.assertEqual(document.raw_text, '배포는 금요일 오후에만 하지 않습니다')

    # 대표가 수집 대상으로 등록하지 않은 채널은 저장하지 않는다.
    def test_unregistered_channel_is_ignored(self):
        response = self.event(channel='C999')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(RawDocument.objects.exists())

    def test_removed_channel_is_ignored(self):
        self.item.removed_at = '2026-08-01T00:00:00Z'
        self.item.save()

        self.event()

        self.assertFalse(RawDocument.objects.exists())

    # SAI가 보낸 확인 질문이 다시 수집되면 안 된다.
    # 슬랙은 앱 메시지에 bot_id 와 user 를 함께 실어 보낸다. user가 있어도 봇이다.
    def test_bot_message_is_ignored(self):
        self.event(bot_id='B001', user='U0BPPRH9HTK')

        self.assertFalse(RawDocument.objects.exists())

    def test_bot_message_without_user_is_ignored(self):
        self.event(bot_id='B001', user=None)

        self.assertFalse(RawDocument.objects.exists())

    def test_system_message_is_ignored(self):
        self.event(subtype='channel_join', text='조상원님이 참여했습니다')

        self.assertFalse(RawDocument.objects.exists())

    def test_empty_text_is_ignored(self):
        self.event(text='   ')

        self.assertFalse(RawDocument.objects.exists())

    def test_non_message_event_is_ignored(self):
        self.event(type='reaction_added')

        self.assertFalse(RawDocument.objects.exists())

    # --- 스레드 / 작성자 ---

    def test_thread_reply_keeps_parent_reference(self):
        self.event(ts='1786800000.000200', thread_ts='1786800000.000100', text='답글입니다')

        self.assertEqual(RawDocument.objects.get().thread_ref, '1786800000.000100')

    # 슬랙은 최상위 메시지에도 thread_ts를 채워 보낼 때가 있다. 자기 자신은 부모가 아니다.
    def test_self_thread_ts_is_not_a_parent(self):
        self.event(ts='1786800000.000100', thread_ts='1786800000.000100')

        self.assertIsNone(RawDocument.objects.get().thread_ref)

    # 이름을 채우려면 users.list를 불러야 하는데 웹훅에서는 시간이 없다.
    def test_creates_minimal_identity(self):
        self.event()

        identity = Identity.objects.get()
        self.assertEqual(identity.external_user_id, 'U001')
        self.assertIsNone(identity.external_handle)
        self.assertEqual(RawDocument.objects.get().author_identity, identity)

    def test_reuses_existing_identity(self):
        existing = Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U001', external_handle='조상원',
        )

        self.event()

        self.assertEqual(Identity.objects.count(), 1)
        self.assertEqual(RawDocument.objects.get().author_identity, existing)

    def test_updates_item_count(self):
        self.event()
        self.event(ts='1786800000.000200', text='두 번째')

        self.item.refresh_from_db()
        self.assertEqual(self.item.item_count, 2)

    # 처리 중 오류가 나도 200을 줘야 한다. 500이면 슬랙이 세 번 더 보낸다.
    def test_handler_error_still_returns_200(self):
        from unittest.mock import patch

        with patch('sources.views.handle_event', side_effect=RuntimeError('boom')):
            response = self.event()

        self.assertEqual(response.status_code, 200)

    # 회사가 다르면 서로의 메시지를 저장할 수 없다.
    def test_other_company_channel_is_not_touched(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_connection = Connection.objects.create(
            company=other, kind=Connection.Kind.SLACK,
            external_workspace_id='T_OTHER', bot_token='xoxb-o', signing_secret='other',
        )
        Item.objects.create(
            company=other, connection=other_connection, external_id='C001', label='#dev'
        )

        self.event()

        self.assertEqual(RawDocument.objects.get().company, self.company)
