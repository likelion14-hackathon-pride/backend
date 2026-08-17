import hashlib
import hmac
import logging
import time

from django.conf import settings

from qna.escalation import mark_reply_pending

from .ingestion import is_collectable, save_document
from .models import Connection, Identity, Item
from .slack import SlackClient, SlackError

logger = logging.getLogger(__name__)

# 슬랙 권장값. 이보다 오래된 요청은 재전송 공격으로 본다.
MAX_TIMESTAMP_SKEW = 300


def _sign(secret, timestamp, body):
    base = b'v0:' + timestamp.encode() + b':' + body

    return 'v0=' + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


# 슬랙 시그니처 검증. 시크릿 하나에 대해 참/거짓만 돌려준다.
def verify_signature(secret, timestamp, signature, body):
    if not secret or not timestamp or not signature:
        return False

    try:
        if abs(time.time() - int(timestamp)) > MAX_TIMESTAMP_SKEW:
            return False
    except ValueError:
        return False

    try:
        return hmac.compare_digest(_sign(secret, timestamp, body), signature)
    except (UnicodeEncodeError, TypeError):
        return False


# 이벤트 본문의 team_id로 회사를 찾는다.
# 본문은 아직 검증 전이라 신뢰할 수 없다. 여기서 얻은 연결의 시크릿으로 서명을 확인한 뒤에야
# 본문을 신뢰한다. 남의 team_id를 적어 보내도 그 회사의 시크릿을 모르면 검증을 통과할 수 없다.
def find_connection(team_id):
    if not team_id:
        return None

    return Connection.objects.filter(
        kind=Connection.Kind.SLACK,
        external_workspace_id=team_id,
        disconnected_at__isnull=True,
    ).select_related('company').first()


# URL 검증 요청에는 team_id가 없어서 회사를 특정할 수 없다.
# 앱 설정 시 한 번뿐인 요청이므로 알고 있는 시크릿을 차례로 시도한다.
def verify_url_verification(timestamp, signature, body):
    secrets = [settings.SLACK_SIGNING_SECRET]
    secrets += list(
        Connection.objects.filter(
            kind=Connection.Kind.SLACK, disconnected_at__isnull=True
        ).exclude(signing_secret=None).values_list('signing_secret', flat=True)
    )

    return any(verify_signature(secret, timestamp, signature, body) for secret in secrets)


# 슬랙 user_id에 해당하는 Identity를 찾고 없으면 최소한으로 만든다.
# 이름을 채우려면 users.list를 불러야 하는데 웹훅은 3초 안에 끝나야 하므로 하지 않는다.
# 다음 수집 작업이 external_handle을 채운다.
def resolve_identity(connection, event):
    external_user_id = event.get('user') or event.get('bot_id')
    if not external_user_id:
        return None

    identity, _ = Identity.objects.get_or_create(
        connection=connection,
        external_user_id=external_user_id,
        defaults={
            'company_id': connection.company_id,
            'is_bot': bool(event.get('bot_id')) and not event.get('user'),
        },
    )

    return identity


# permalink 조립용 워크스페이스 URL. 없으면 한 번만 받아와 저장한다.
def get_workspace_url(connection):
    if connection.workspace_url:
        return connection.workspace_url

    try:
        url = SlackClient(connection.bot_token).auth_test().get('url')
    except SlackError:
        return None

    if url:
        connection.workspace_url = url
        connection.save(update_fields=['workspace_url'])

    return url


# 검증을 마친 이벤트를 저장한다. 저장했으면 RawDocument, 아니면 None.
# AI는 여기서 돌리지 않는다. 슬랙은 3초 안에 200을 받지 못하면 재시도한다.
def handle_event(connection, event):
    if event.get('type') != 'message':
        return None
    # 봇 메시지와 시스템 메시지 제외. 수집 경로와 같은 규칙을 쓴다.
    if not is_collectable(event):
        return None

    item = Item.objects.filter(
        connection=connection, external_id=event.get('channel'), removed_at__isnull=True
    ).first()
    # 대표가 수집 대상으로 등록하지 않은 채널은 무시한다.
    if item is None:
        return None

    thread_ts = event.get('thread_ts')
    # 슬랙은 최상위 메시지에도 thread_ts를 채워 보낼 때가 있다. 자기 자신은 부모가 아니다.
    parent_ts = thread_ts if thread_ts and thread_ts != event['ts'] else None
    save_document(
        item=item,
        message=event,
        author_identity=resolve_identity(connection, event),
        workspace_url=get_workspace_url(connection),
        thread_ref=parent_ts,
    )

    Item.objects.filter(id=item.id).update(
        item_count=item.documents.count(),
    )

    # 대표가 확인 질문에 답장했을 수 있다. 표시만 남기고 회수는 워커에 맡긴다.
    mark_reply_pending(connection.company_id, item.external_id, event['ts'], parent_ts)

    return item
