from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import Connection, Item
from .github import GitHubClient, GitHubError
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


def _github_client():
    return GitHubClient(
        settings.GITHUB_APP_ID,
        settings.GITHUB_PRIVATE_KEY,
        settings.GITHUB_INSTALLATION_ID,
    )


# 미리 설치한 GitHub App을 회사 소스로 연결한다.
def connect_github(company):
    try:
        installation = _github_client().installation()
    except GitHubError as exc:
        raise ValidationError({'github': [exc.code]})

    installation_id = str(installation['id'])
    account = installation.get('account') or {}
    display_name = account.get('login') or account.get('name') or 'GitHub'

    # 한 App 설치가 두 회사에 붙으면 웹훅 수신 회사를 특정할 수 없다.
    taken = (
        Connection.objects.filter(
            external_workspace_id=installation_id, disconnected_at__isnull=True
        )
        .exclude(company=company)
        .exists()
    )
    if taken:
        raise ValidationError({'github': ['installation already connected to another company']})

    connection = Connection.objects.filter(
        company=company, kind=Connection.Kind.GITHUB, disconnected_at__isnull=True
    ).first()
    is_created = connection is None
    if is_created:
        connection = Connection(company=company, kind=Connection.Kind.GITHUB)

    connection.status = Connection.Status.CONNECTED
    connection.external_workspace_id = installation_id
    connection.display_name = display_name
    connection.workspace_url = account.get('html_url')
    connection.credential_ref = f'github-app-installation:{installation_id}'
    connection.error_message = None
    connection.save()

    return connection, is_created


# 슬랙 워크스페이스를 연결한다. (연결, 새로 만들었는지) 반환.
# 이미 연결된 회사가 다시 호출하면 자격증명을 교체한다.
def connect_slack(company, bot_token, signing_secret):
    # 저장 전에 슬랙에 직접 물어본다. 잘못된 키를 DB에 남기지 않기 위함.
    try:
        auth = SlackClient(bot_token).auth_test()
    except SlackError as exc:
        raise ValidationError({'botToken': [exc.code]})

    workspace_id = auth.get('team_id')
    # 한 워크스페이스가 두 회사에 붙으면 웹훅의 team_id로 회사를 특정할 수 없다.
    taken = (
        Connection.objects.filter(
            external_workspace_id=workspace_id, disconnected_at__isnull=True
        )
        .exclude(company=company)
        .exists()
    )
    if taken:
        raise ValidationError({'botToken': ['workspace already connected to another company']})

    connection = Connection.objects.filter(
        company=company, kind=Connection.Kind.SLACK, disconnected_at__isnull=True
    ).first()
    is_created = connection is None
    if is_created:
        connection = Connection(company=company, kind=Connection.Kind.SLACK)

    connection.status = Connection.Status.CONNECTED
    connection.external_workspace_id = workspace_id
    connection.display_name = auth.get('team')
    # 웹훅에서 permalink를 조립할 때 쓴다. 여기서 받아 두면 나중에 부를 일이 없다.
    connection.workspace_url = auth.get('url')
    connection.bot_token = bot_token
    connection.signing_secret = signing_secret
    connection.error_message = None
    connection.save()

    if is_created:
        register_channels_recording_error(connection)

    return connection, is_created


def disconnect(connection):
    connection.disconnected_at = timezone.now()
    connection.status = Connection.Status.ERROR
    connection.save(update_fields=['disconnected_at', 'status'])

    return connection


def _repository_label(repository):
    return repository['full_name']


# 아직 수집 대상으로 등록되지 않은 GitHub App 접근 가능 레포
def list_available_repositories(connection):
    registered = _registered_ids(connection)
    repositories = _github_client().repositories()

    return [
        {
            'externalId': str(repository['id']),
            'label': _repository_label(repository),
            'isPrivate': bool(repository.get('private')),
        }
        for repository in repositories
        if str(repository['id']) not in registered
    ]


# 레포를 수집 대상으로 추가한다. App 설치 범위 밖의 레포는 등록할 수 없다.
def add_repository(connection, external_id):
    repositories = _github_client().repositories()
    repository = next(
        (repository for repository in repositories if str(repository['id']) == external_id),
        None,
    )
    if repository is None:
        raise GitHubError('repository_not_found')

    item, _ = Item.objects.update_or_create(
        connection=connection,
        external_id=external_id,
        defaults={
            'company_id': connection.company_id,
            'label': _repository_label(repository),
            # 이전에 제외했던 레포를 다시 추가하는 경우 되살린다.
            'removed_at': None,
        },
    )

    return item


# 레포를 제외해도 이후 원문·근거를 보존하기 위해 행은 삭제하지 않는다.
def remove_repository(item):
    item.removed_at = timezone.now()
    item.save(update_fields=['removed_at'])

    return item
