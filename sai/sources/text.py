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
