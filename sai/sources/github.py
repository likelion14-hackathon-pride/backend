import base64
import json
import time
import urllib.error
import urllib.request

import jwt


GITHUB_API_BASE = 'https://api.github.com'
DEFAULT_TIMEOUT = 10


# GitHub API 호출 또는 GitHub App 설정이 실패한 경우.
class GitHubError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# GitHub App 설치 토큰으로 API를 호출하는 최소 클라이언트.
# 개인키는 DB가 아닌 서버 설정에서만 받아 사용한다.
class GitHubClient:
    def __init__(self, app_id, private_key, installation_id, timeout=DEFAULT_TIMEOUT):
        self.app_id = str(app_id)
        self.private_key = private_key.replace('\\n', '\n')
        self.installation_id = str(installation_id)
        self.timeout = timeout

    def _app_jwt(self):
        if not self.app_id or not self.private_key or not self.installation_id:
            raise GitHubError('github_app_not_configured')

        now = int(time.time())
        payload = {
            'iat': now - 60,
            'exp': now + 9 * 60,
            'iss': self.app_id,
        }

        try:
            return jwt.encode(payload, self.private_key, algorithm='RS256')
        except Exception as exc:
            raise GitHubError('github_app_key_invalid') from exc

    def _request(self, method, path, token, data=None):
        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': '2022-11-28',
        }
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers['Content-Type'] = 'application/json'

        request = urllib.request.Request(
            GITHUB_API_BASE + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise GitHubError('github_auth_failed') from exc
            if exc.code == 403:
                raise GitHubError('github_permission_denied') from exc
            if exc.code == 404:
                raise GitHubError('github_resource_not_found') from exc
            raise GitHubError('github_request_failed') from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GitHubError('github_unreachable') from exc

    # App JWT로 설치 토큰을 발급받는다. 이 토큰은 GitHub API 호출에만 잠깐 사용한다.
    def _installation_token(self):
        body = self._request(
            'POST',
            f'/app/installations/{self.installation_id}/access_tokens',
            self._app_jwt(),
        )
        token = body.get('token')
        if not token:
            raise GitHubError('github_auth_failed')

        return token

    # 목록 API는 한 번에 최대 100개만 주므로 끝까지 나눠서 가져온다.
    def _paginate(self, path, token, max_pages=20):
        results = []
        separator = '&' if '?' in path else '?'

        for page in range(1, max_pages + 1):
            current = self._request(
                'GET',
                f'{path}{separator}per_page=100&page={page}',
                token,
            )
            if not isinstance(current, list):
                raise GitHubError('github_response_invalid')

            results += current
            if len(current) < 100:
                break

        return results

    # 연결 화면에 보여 줄 설치 계정 정보.
    def installation(self):
        return self._request(
            'GET',
            f'/app/installations/{self.installation_id}',
            self._app_jwt(),
        )

    # 이 GitHub App 설치에서 접근 가능한 레포 전체.
    def repositories(self, max_pages=20):
        token = self._installation_token()
        repositories = []

        for page in range(1, max_pages + 1):
            body = self._request(
                'GET',
                f'/installation/repositories?per_page=100&page={page}',
                token,
            )
            current = body.get('repositories', [])
            repositories += current

            if len(current) < 100:
                break

        return repositories

    # 레포 최상위 README. README가 없는 레포는 오류 대신 None을 반환한다.
    def readme(self, repository):
        token = self._installation_token()
        try:
            result = self._request('GET', f'/repos/{repository}/readme', token)
        except GitHubError as exc:
            if exc.code == 'github_resource_not_found':
                return None
            raise

        if result.get('encoding') != 'base64':
            raise GitHubError('github_response_invalid')

        try:
            text = base64.b64decode(result.get('content', '')).decode('utf-8')
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubError('github_response_invalid') from exc

        return {
            'path': result.get('path') or 'README.md',
            'sha': result.get('sha'),
            'html_url': result.get('html_url'),
            'text': text,
        }

    # Issue API에는 PR도 함께 나오므로 실제 Issue만 남긴다.
    def issues(self, repository, max_pages=20):
        token = self._installation_token()
        issues = self._paginate(
            f'/repos/{repository}/issues?state=all&sort=updated&direction=asc',
            token,
            max_pages,
        )

        return [issue for issue in issues if 'pull_request' not in issue]

    # 일반 Issue 댓글과 PR 대화 댓글은 같은 API에서 조회된다.
    def issue_comments(self, repository, max_pages=20):
        token = self._installation_token()
        return self._paginate(
            f'/repos/{repository}/issues/comments?sort=updated&direction=asc',
            token,
            max_pages,
        )

    def pull_requests(self, repository, max_pages=20):
        token = self._installation_token()
        return self._paginate(
            f'/repos/{repository}/pulls?state=all&sort=updated&direction=asc',
            token,
            max_pages,
        )

    # 코드 줄에 달린 리뷰 댓글. PR 전체에 단 댓글은 issue_comments에서 가져온다.
    def pull_review_comments(self, repository, max_pages=20):
        token = self._installation_token()
        return self._paginate(
            f'/repos/{repository}/pulls/comments?sort=updated&direction=asc',
            token,
            max_pages,
        )
