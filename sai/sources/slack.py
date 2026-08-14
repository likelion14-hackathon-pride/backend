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

    def _get(self, method, **params):
        url = SLACK_API_BASE + method
        if params:
            url += '?' + urllib.parse.urlencode(params)

        request = urllib.request.Request(url, headers={'Authorization': f'Bearer {self.bot_token}'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SlackError('slack_unreachable') from exc

        if not body.get('ok'):
            raise SlackError(body.get('error') or 'unknown_error')

        return body

    # 토큰 유효성 확인 + 워크스페이스 식별. 연결 저장 전에 반드시 호출한다.
    def auth_test(self):
        return self._get('auth.test')
