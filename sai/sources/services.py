from django.db import transaction
from django.utils import timezone

from .models import Connection, Item
from .slack import SlackClient, SlackError


# 봇이 참여 중인 슬랙 채널을 Item으로 동기화한다.
# 재실행해도 안전하며, 대표가 지정해 둔 scope는 건드리지 않는다.
@transaction.atomic
def sync_slack_channels(connection):
    channels = SlackClient(connection.bot_token).joined_channels()

    created = 0
    seen_ids = []
    for channel in channels:
        seen_ids.append(channel['id'])
        _, is_created = Item.objects.update_or_create(
            connection=connection,
            external_id=channel['id'],
            defaults={
                'company_id': connection.company_id,
                'label': f'#{channel["name"]}',
                # 봇이 나갔다가 다시 초대된 경우 되살린다.
                'removed_at': None,
            },
        )
        created += is_created

    # 봇이 나갔거나 삭제된 채널은 지우지 않고 표시만 한다. 이미 수집한 문서가 딸려 있기 때문.
    removed = (
        Item.objects.filter(connection=connection, removed_at__isnull=True)
        .exclude(external_id__in=seen_ids)
        .update(removed_at=timezone.now())
    )

    return {'created': created, 'updated': len(seen_ids) - created, 'removed': removed}


# 연결 직후와 수동 재동기화에서 함께 쓴다.
# 채널 조회 실패로 연결 자체를 되돌리지는 않는다. 토큰은 이미 auth.test를 통과했고,
# 대표가 스코프를 고친 뒤 다시 시도하면 되기 때문. 대신 상태를 ERROR로 남겨 화면에 드러낸다.
def sync_channels_recording_error(connection):
    try:
        result = sync_slack_channels(connection)
    except SlackError as exc:
        connection.status = Connection.Status.ERROR
        connection.error_message = exc.code
        connection.save(update_fields=['status', 'error_message'])
        return None

    if connection.status != Connection.Status.CONNECTED or connection.error_message:
        connection.status = Connection.Status.CONNECTED
        connection.error_message = None
        connection.save(update_fields=['status', 'error_message'])

    return result
