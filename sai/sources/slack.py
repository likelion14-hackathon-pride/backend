import json
import urllib.error
import urllib.parse
import urllib.request

SLACK_API_BASE = 'https://slack.com/api/'
DEFAULT_TIMEOUT = 10


# 슬랙 API가 ok=false를 돌려주거나 네트워크가 실패한 경우.
# code에는 슬랙의 error 값(invalid_auth, missing_scope 등)이 들어간다.
class SlackError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# 봇 토큰으로 슬랙 Web API를 호출하는 최소 클라이언트.
# 외부 의존성을 늘리지 않으려고 urllib을 쓴다.
class SlackClient:
    def __init__(self, bot_token, timeout=DEFAULT_TIMEOUT):
        self.bot_token = bot_token
        self.timeout = timeout

    def _call(self, method, data=None, **params):
        url = SLACK_API_BASE + method
        if params:
            url += '?' + urllib.parse.urlencode(params)

        headers = {'Authorization': f'Bearer {self.bot_token}'}
        body_bytes = None
        if data is not None:
            body_bytes = urllib.parse.urlencode(data).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'

        request = urllib.request.Request(url, data=body_bytes, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SlackError('slack_unreachable') from exc

        if not body.get('ok'):
            raise SlackError(body.get('error') or 'unknown_error')

        return body

    def _get(self, method, **params):
        return self._call(method, **params)

    # 토큰 유효성 확인 + 워크스페이스 식별. 연결 저장 전에 반드시 호출한다.
    def auth_test(self):
        return self._get('auth.test')

    # 워크스페이스 채널 전체. is_member / is_private 플래그가 함께 온다.
    # 비공개 채널은 봇이 이미 멤버인 것만 응답에 포함된다(슬랙 동작).
    def list_channels(self, max_pages=20):
        channels = []
        cursor = None

        for _ in range(max_pages):
            params = {
                'types': 'public_channel,private_channel',
                'exclude_archived': 'true',
                'limit': 200,
            }
            if cursor:
                params['cursor'] = cursor

            body = self._get('conversations.list', **params)
            channels += body.get('channels', [])

            cursor = (body.get('response_metadata') or {}).get('next_cursor')
            if not cursor:
                break

        return channels

    # 봇이 공개 채널에 스스로 참여한다. 비공개 채널에는 쓸 수 없다.
    # 이미 들어가 있어도 ok로 응답하므로 재호출해도 안전하다.
    def join_channel(self, channel_id):
        return self._call('conversations.join', data={'channel': channel_id})
