import html
import re

# 슬랙은 멘션과 링크를 <...> 로 감싼 자체 형식으로 준다.
# 사람이 화면에서 보는 것과 원문이 달라서, AI에 넣기 전에 읽을 수 있는 형태로 바꾼다.
CHANNEL_MENTION = re.compile(r'<#(C[A-Z0-9]+)(?:\|([^>]*))?>')
USER_MENTION = re.compile(r'<@([UWB][A-Z0-9]+)(?:\|([^>]*))?>')
LINK = re.compile(r'<(https?://[^>|]+)(?:\|([^>]*))?>')
SPECIAL_MENTION = re.compile(r'<!(here|channel|everyone)(?:\|[^>]*)?>')


def normalize_slack_text(text, channels=None, users=None):
    if not text:
        return ''

    channels = channels or {}
    users = users or {}

    def channel(match):
        external_id, inline_name = match.groups()
        if inline_name:
            return f'#{inline_name}'
        # Item.label은 이미 '#dev' 형태다.
        return channels.get(external_id, '#채널')

    def user(match):
        external_id, inline_name = match.groups()
        return f'@{inline_name or users.get(external_id, "알수없음")}'

    def link(match):
        url, label = match.groups()
        return label or url

    text = CHANNEL_MENTION.sub(channel, text)
    text = USER_MENTION.sub(user, text)
    text = LINK.sub(link, text)
    text = SPECIAL_MENTION.sub(lambda m: f'@{m.group(1)}', text)

    # 슬랙은 & < > 를 이스케이프해서 보낸다. 마크업을 먼저 처리한 뒤에 풀어야
    # 본문에 있던 &lt; 가 새 태그처럼 보이는 일이 없다.
    return html.unescape(text).strip()


# 채팅에 실수로 붙여넣는 자격증명들. 임베딩은 외부로 나가는 경로라 그 전에 지운다.
# 한 번 나가면 회수할 수 없으므로, 놓치는 것보다 과하게 가리는 쪽을 택한다.
SECRET_PATTERNS = [
    re.compile(r'xox[baprse]-[A-Za-z0-9-]{10,}'),          # 슬랙 토큰
    re.compile(r'sk-[A-Za-z0-9_-]{20,}'),                   # OpenAI 키
    re.compile(r'AKIA[0-9A-Z]{16}'),                        # AWS 액세스 키 ID
    re.compile(r'ghp_[A-Za-z0-9]{20,}'),                    # GitHub 토큰
    re.compile(r'eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+'),  # JWT
    re.compile(
        r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----',
        re.DOTALL,
    ),
]
REDACTED = '[비밀정보 제거됨]'


# (가린 텍스트, 가린 것이 있었는지) 반환.
def redact_secrets(text):
    if not text:
        return text, False

    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)

    return redacted, redacted != text
