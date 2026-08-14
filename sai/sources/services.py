from django.utils import timezone

from .models import Connection, Item
from .slack import SlackClient, SlackError


def _label(channel):
    return f'#{channel["name"]}'


def _registered_ids(connection):
    return set(
        Item.objects.filter(connection=connection, removed_at__isnull=True)
        .values_list('external_id', flat=True)
    )


# 봇이 이미 참여 중인 채널을 수집 대상으로 등록한다. 슬랙 연동 직후 한 번만 호출한다.
# 대표가 나중에 제외한 채널을 되살리지 않도록, 여기서는 아무것도 제거하지 않는다.
def register_joined_channels(connection):
    channels = [c for c in SlackClient(connection.bot_token).list_channels() if c.get('is_member')]

    created = 0
    for channel in channels:
        _, is_created = Item.objects.get_or_create(
            connection=connection,
            external_id=channel['id'],
            defaults={
                'company_id': connection.company_id,
                'label': _label(channel),
            },
        )
        created += is_created

    return {'created': created, 'skipped': len(channels) - created}


# 연동 직후 호출용. 채널 조회 실패로 연결 자체를 되돌리지는 않는다.
# 토큰은 이미 auth.test를 통과했고, 스코프를 고친 뒤 다시 시도하면 되기 때문.
def register_channels_recording_error(connection):
    try:
        result = register_joined_channels(connection)
    except SlackError as exc:
        connection.status = Connection.Status.ERROR
        connection.error_message = exc.code
        connection.save(update_fields=['status', 'error_message'])
        return None

    clear_connection_error(connection)

    return result


def clear_connection_error(connection):
    if connection.status != Connection.Status.CONNECTED or connection.error_message:
        connection.status = Connection.Status.CONNECTED
        connection.error_message = None
        connection.save(update_fields=['status', 'error_message'])


# 아직 수집 대상으로 등록되지 않은 워크스페이스 채널.
# 비공개 채널은 봇이 이미 멤버인 것만 보인다. 나머지는 슬랙에서 직접 초대해야 한다.
def list_available_channels(connection):
    registered = _registered_ids(connection)
    channels = SlackClient(connection.bot_token).list_channels()

    return [
        {
            'externalId': channel['id'],
            'label': _label(channel),
            'isPrivate': bool(channel.get('is_private')),
            'isMember': bool(channel.get('is_member')),
        }
        for channel in channels
        if channel['id'] not in registered
    ]


# 채널을 수집 대상으로 추가한다. 봇이 아직 없으면 공개 채널에 한해 스스로 참여한다.
def add_channel(connection, external_id):
    client = SlackClient(connection.bot_token)
    channel = next((c for c in client.list_channels() if c['id'] == external_id), None)

    if channel is None:
        raise SlackError('channel_not_found')

    if not channel.get('is_member'):
        # 비공개 채널은 봇이 스스로 들어갈 수 없다. 사람이 슬랙에서 초대해야 한다.
        if channel.get('is_private'):
            raise SlackError('cannot_join_private_channel')
        client.join_channel(external_id)

    item, _ = Item.objects.update_or_create(
        connection=connection,
        external_id=external_id,
        defaults={
            'company_id': connection.company_id,
            'label': _label(channel),
            # 이전에 제외했던 채널을 다시 추가하는 경우 되살린다.
            'removed_at': None,
        },
    )

    return item


# 수집 대상에서 제외한다. 봇은 채널에 그대로 둔다.
# 행을 지우면 이미 수집한 RawDocument가 CASCADE로 함께 사라지기 때문.
def remove_channel(item):
    item.removed_at = timezone.now()
    item.save(update_fields=['removed_at'])

    return item
