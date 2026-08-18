from datetime import timedelta

from django.utils import timezone

from companies.models import Company

from .ingestion import has_pending_work
from .local_files import LocalFileStorageError, object_exists
from .models import Connection, IngestionJob, Item

# 아침에 받은 지시가 점심때 카드로 뜨면 늦다. 처리할 것이 있을 때만 돈다.
PROCESS_EVERY = timedelta(minutes=10)

# 웹훅이 멈춰 있던 동안의 메시지를 메우려고 슬랙을 다시 읽는다.
COLLECT_EVERY = timedelta(hours=12)

# OpenAI 한도에 걸린 상태에서 10분마다 재시도하면 한도만 계속 태운다.
RETRY_AFTER = timedelta(hours=1)

FAILED_STATUSES = (IngestionJob.Status.FAILED, IngestionJob.Status.PARTIAL)

SWEPT_KINDS = (Connection.Kind.SLACK, Connection.Kind.GITHUB, Connection.Kind.LOCAL)


def _last_job(company, kind, connection):
    return (
        IngestionJob.objects.filter(company=company, kind=kind, connection=connection)
        .order_by('-id')
        .first()
    )


def _is_due(last, every, now):
    if last is None:
        return True

    wait = RETRY_AFTER if last.status in FAILED_STATUSES else every

    return last.created_at + wait <= now


def _enqueue(company, connection, kind, item_ids):
    return IngestionJob.objects.create(
        company=company, connection=connection, kind=kind, item_ids=item_ids
    )


def _active_connection(company, kind):
    return Connection.objects.filter(
        company=company, kind=kind, disconnected_at__isnull=True
    ).first()


# 실패한 파일이 60초마다 되살아나면 S3와 큐만 계속 두드린다.
def _attempted_recently(company, item_id, now):
    last = (
        IngestionJob.objects.filter(company=company, item_ids__contains=[item_id])
        .order_by('-id')
        .first()
    )

    return last is not None and last.created_at + RETRY_AFTER > now


def _live_items(connection):
    return Item.objects.filter(connection=connection, removed_at__isnull=True)


def _never_synced(connection):
    return _live_items(connection).filter(last_synced_at__isnull=True)


def _uploaded_local_items(company, connection, now):
    pending = (
        _never_synced(connection)
        .exclude(storage_key__isnull=True)
        .exclude(storage_key='')
    )

    item_ids = []
    for item in pending:
        if _attempted_recently(company, item.id, now):
            continue
        try:
            if object_exists(item.storage_key):
                item_ids.append(item.id)
        except LocalFileStorageError:
            continue

    return item_ids


# 올린 파일은 한 번만 읽으면 된다. 주기가 아니라 아직 읽지 않았는지로 판단한다.
def _local_due_job(company, now):
    connection = _active_connection(company, Connection.Kind.LOCAL)
    if connection is None:
        return None

    item_ids = _uploaded_local_items(company, connection, now)
    if not item_ids:
        return None

    return _enqueue(company, connection, IngestionJob.Kind.COLLECT, item_ids)


def _github_due_job(company, now):
    connection = _active_connection(company, Connection.Kind.GITHUB)
    if connection is None:
        return None

    item_ids = list(_live_items(connection).values_list('id', flat=True))
    if not item_ids:
        return None

    # 웹훅은 등록한 뒤에 생긴 변경만 알려 준다. 방금 담은 레포의 README·이슈·PR 은 여기서 읽는다.
    fresh_ids = [
        item_id
        for item_id in _never_synced(connection).values_list('id', flat=True)
        if not _attempted_recently(company, item_id, now)
    ]
    if fresh_ids:
        return _enqueue(company, connection, IngestionJob.Kind.COLLECT, fresh_ids)

    # 웹훅 설정을 건너뛴 회사에는 이 주기가 유일한 갱신 경로다.
    if _is_due(_last_job(company, IngestionJob.Kind.COLLECT, connection), COLLECT_EVERY, now):
        return _enqueue(company, connection, IngestionJob.Kind.COLLECT, item_ids)

    if not _is_due(_last_job(company, IngestionJob.Kind.PROCESS, connection), PROCESS_EVERY, now):
        return None

    if not has_pending_work(company):
        return None

    return _enqueue(company, connection, IngestionJob.Kind.PROCESS, item_ids)


def _slack_due_job(company, now):
    connection = _active_connection(company, Connection.Kind.SLACK)
    if connection is None:
        return None

    item_ids = list(_live_items(connection).values_list('id', flat=True))
    if not item_ids:
        return None

    fresh_ids = [
        item_id
        for item_id in _never_synced(connection).values_list('id', flat=True)
        if not _attempted_recently(company, item_id, now)
    ]
    if fresh_ids:
        return _enqueue(company, connection, IngestionJob.Kind.COLLECT, fresh_ids)

    # 수집이 먼저다. 새 원문을 가져오면 어차피 뒤이어 처리까지 한다.
    if _is_due(_last_job(company, IngestionJob.Kind.COLLECT, connection), COLLECT_EVERY, now):
        return _enqueue(company, connection, IngestionJob.Kind.COLLECT, item_ids)

    if not _is_due(_last_job(company, IngestionJob.Kind.PROCESS, connection), PROCESS_EVERY, now):
        return None

    # 처리할 것이 없으면 만들지 않는다. 빈 작업이 10분마다 쌓이면 로그만 지저분해진다.
    if not has_pending_work(company):
        return None

    return _enqueue(company, connection, IngestionJob.Kind.PROCESS, item_ids)


def _due_job(company, now):
    # 이미 대기 중이거나 도는 작업이 있으면 쌓지 않는다.
    if IngestionJob.objects.filter(
        company=company,
        status__in=(IngestionJob.Status.QUEUED, IngestionJob.Status.RUNNING),
    ).exists():
        return None

    # 방금 올린 파일이 슬랙 주기 뒤로 밀리면 화면에서는 업로드가 실패한 것으로 보인다.
    local_job = _local_due_job(company, now)
    if local_job is not None:
        return local_job

    github_job = _github_due_job(company, now)
    if github_job is not None:
        return github_job

    return _slack_due_job(company, now)


# 웹훅은 원문만 저장하고, 업로드는 S3에서 끝난다. 큐에 넣는 것은 여기뿐이다.
def enqueue_due_jobs(now=None):
    now = now or timezone.now()
    companies = Company.objects.filter(
        connections__kind__in=SWEPT_KINDS,
        connections__disconnected_at__isnull=True,
    ).distinct()

    jobs = []
    for company in companies:
        job = _due_job(company, now)
        if job is not None:
            jobs.append(job)

    return jobs
