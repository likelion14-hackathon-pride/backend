import hashlib
import hmac

from .models import Connection, IngestionJob, Item


SUPPORTED_ACTIONS = {
    'issues': {'opened', 'edited', 'closed', 'reopened', 'deleted'},
    'issue_comment': {'created', 'edited', 'deleted'},
    'pull_request': {'opened', 'edited', 'closed', 'reopened', 'synchronize'},
    'pull_request_review_comment': {'created', 'edited', 'deleted'},
}


# GitHub가 보낸 원문 body를 같은 시크릿으로 서명해 요청 헤더와 비교한다.
def verify_github_signature(secret, signature, body):
    if not secret or not signature:
        return False
    if not signature.startswith('sha256='):
        return False

    expected = 'sha256=' + hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def _needs_collection(event_name, data):
    if event_name == 'push':
        return True

    return data.get('action') in SUPPORTED_ACTIONS.get(event_name, set())


def find_github_connection(data):
    installation_id = (data.get('installation') or {}).get('id')
    if installation_id is None:
        return None

    return Connection.objects.filter(
        kind=Connection.Kind.GITHUB,
        external_workspace_id=str(installation_id),
        disconnected_at__isnull=True,
    ).first()


def _find_item(data):
    connection = find_github_connection(data)
    repository_id = (data.get('repository') or {}).get('id')
    if connection is None or repository_id is None:
        return None

    return Item.objects.filter(
        connection=connection,
        external_id=str(repository_id),
        removed_at__isnull=True,
    ).first()


# 웹훅에서는 긴 GitHub 조회와 AI 처리를 하지 않고 작업만 큐에 넣는다.
def handle_github_event(event_name, data):
    if not _needs_collection(event_name, data):
        return None

    item = _find_item(data)
    if item is None:
        return None

    # 같은 레포의 이벤트가 짧은 시간에 여러 개 오면 대기 중인 작업 하나로 합친다.
    queued = IngestionJob.objects.filter(
        company=item.company,
        kind=IngestionJob.Kind.COLLECT,
        status=IngestionJob.Status.QUEUED,
        item_ids=[item.id],
    ).first()
    if queued:
        return queued

    return IngestionJob.objects.create(
        company=item.company,
        connection=item.connection,
        kind=IngestionJob.Kind.COLLECT,
        item_ids=[item.id],
    )
