import hashlib
from datetime import datetime, timezone as dt_timezone

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from .classifier import classify_documents
from .models import Identity, IngestionJob, Item, RawDocument
from .slack import SlackClient, SlackError


# 실수로 거대한 워크스페이스를 붙였을 때 요청이 무한정 길어지지 않게 하는 상한.
# 지금은 수집을 요청 안에서 동기로 처리하므로 반드시 필요하다.
MAX_MESSAGES_PER_CHANNEL = 1000

# 사람의 발언이 아닌 시스템 메시지. 규칙 추출에 쓸모가 없어 저장하지 않는다.
SKIPPED_SUBTYPES = {
    'channel_join',
    'channel_leave',
    'channel_topic',
    'channel_purpose',
    'channel_name',
    'channel_archive',
    'channel_unarchive',
}


def _occurred_at(ts):
    return datetime.fromtimestamp(float(ts), tz=dt_timezone.utc)


def _content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


# https://<workspace>.slack.com/archives/<channel>/p<ts에서 점 제거>
def _permalink(workspace_url, channel_id, ts):
    if not workspace_url:
        return None

    return f'{workspace_url.rstrip("/")}/archives/{channel_id}/p{ts.replace(".", "")}'


def _is_collectable(message):
    if message.get('subtype') in SKIPPED_SUBTYPES:
        return False

    return bool((message.get('text') or '').strip())


# 슬랙 사용자를 Identity로 등록하고 {slack_user_id: Identity} 맵을 돌려준다.
# 메시지마다 조회하지 않도록 수집 시작 시 한 번만 만든다.
def build_identity_map(connection):
    members = SlackClient(connection.bot_token).users_list()

    identities = {}
    for member in members:
        if member.get('deleted'):
            continue

        profile = member.get('profile') or {}
        handle = profile.get('display_name') or profile.get('real_name') or member.get('name')
        identity, _ = Identity.objects.update_or_create(
            connection=connection,
            external_user_id=member['id'],
            defaults={
                'company_id': connection.company_id,
                'external_handle': handle,
                'is_bot': bool(member.get('is_bot')),
                # 게스트가 아닌 정식 멤버를 내부인으로 본다.
                'is_internal': not member.get('is_restricted') and not member.get('is_ultra_restricted'),
            },
        )
        identities[member['id']] = identity

    return identities


# 메시지 한 건을 RawDocument로 저장한다. content_hash가 같으면 내용이 안 바뀐 것이다.
def _save_message(item, message, identity_map, workspace_url, thread_ref=None):
    text = message['text'].strip()
    external_ref = message['ts']

    document, is_created = RawDocument.objects.update_or_create(
        item=item,
        external_ref=external_ref,
        defaults={
            'company_id': item.company_id,
            'thread_ref': thread_ref,
            'author_identity': identity_map.get(message.get('user')),
            'occurred_at': _occurred_at(external_ref),
            'permalink': _permalink(workspace_url, item.external_id, external_ref),
            'raw_text': text,
            'content_hash': _content_hash(text),
        },
    )

    return is_created


# 채널 하나를 수집한다. (신규, 갱신) 반환.
def ingest_channel(item, identity_map, workspace_url, client):
    messages = client.channel_history(item.external_id, max_messages=MAX_MESSAGES_PER_CHANNEL)

    created = 0
    total = 0
    for message in messages:
        if _is_collectable(message):
            created += _save_message(item, message, identity_map, workspace_url)
            total += 1

        # 스레드 답글에 실제 논의가 담기는 경우가 많아 함께 가져온다.
        if not message.get('reply_count'):
            continue

        replies = client.thread_replies(item.external_id, message['ts'])
        for reply in replies:
            # 첫 항목은 부모 메시지라 중복이다.
            if reply['ts'] == message['ts'] or not _is_collectable(reply):
                continue
            created += _save_message(
                item, reply, identity_map, workspace_url, thread_ref=message['ts']
            )
            total += 1

    item.item_count = RawDocument.objects.filter(item=item).count()
    item.last_synced_at = timezone.now()
    item.save(update_fields=['item_count', 'last_synced_at'])

    return created, total


# 수집 작업 실행. 지금은 요청 안에서 동기로 돌지만,
# job만 넘기면 되도록 만들어 두어 나중에 워커로 그대로 옮길 수 있다.
def run_ingestion(job, connection):
    job.status = IngestionJob.Status.RUNNING
    job.save(update_fields=['status'])

    items = list(
        Item.objects.filter(
            connection=connection, removed_at__isnull=True, id__in=job.item_ids
        ).order_by('id')
    )

    client = SlackClient(connection.bot_token)
    errors = []
    created_total = 0

    try:
        workspace_url = client.auth_test().get('url')
        identity_map = build_identity_map(connection)
    except SlackError as exc:
        job.status = IngestionJob.Status.FAILED
        job.errors = [{'scope': 'workspace', 'code': exc.code}]
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'errors', 'completed_at'])
        return job

    for index, item in enumerate(items, start=1):
        try:
            created, _ = ingest_channel(item, identity_map, workspace_url, client)
            created_total += created
        except SlackError as exc:
            errors.append({'itemId': item.id, 'label': item.label, 'code': exc.code})

        job.progress = int(index / len(items) * 100)
        job.save(update_fields=['progress'])

    collection_failed = bool(errors) and len(errors) == len(items)

    # 수집한 원문을 바로 분류한다. 분류가 실패해도 수집 결과는 남긴다.
    if not collection_failed:
        try:
            _, classify_errors = classify_documents(job.company_id)
            errors += classify_errors
        except ImproperlyConfigured:
            errors.append({'scope': 'classify', 'code': 'openai_not_configured'})

    if collection_failed:
        job.status = IngestionJob.Status.FAILED
    elif errors:
        job.status = IngestionJob.Status.PARTIAL
    else:
        job.status = IngestionJob.Status.SUCCEEDED

    job.progress = 100
    job.errors = errors or None
    job.completed_at = timezone.now()
    job.save(update_fields=['status', 'progress', 'errors', 'completed_at'])

    return job
