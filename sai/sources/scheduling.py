from datetime import timedelta

from django.utils import timezone

from companies.models import Company

from .ingestion import has_pending_work
from .models import Connection, IngestionJob, Item

# 아침에 받은 지시가 점심때 카드로 뜨면 늦다. 처리할 것이 있을 때만 돈다.
PROCESS_EVERY = timedelta(minutes=10)

# 웹훅이 멈춰 있던 동안의 메시지를 메우려고 슬랙을 다시 읽는다.
COLLECT_EVERY = timedelta(hours=12)

# OpenAI 한도에 걸린 상태에서 10분마다 재시도하면 한도만 계속 태운다.
RETRY_AFTER = timedelta(hours=1)

FAILED_STATUSES = (IngestionJob.Status.FAILED, IngestionJob.Status.PARTIAL)


def _last_job(company, kind):
    return (
        IngestionJob.objects.filter(company=company, kind=kind).order_by('-id').first()
    )


def _is_due(last, every, now):
    if last is None:
        return True

    wait = RETRY_AFTER if last.status in FAILED_STATUSES else every

    return last.created_at + wait <= now


def _enqueue(company, kind, item_ids):
    return IngestionJob.objects.create(company=company, kind=kind, item_ids=item_ids)


def _due_job(company, now):
    # 이미 대기 중이거나 도는 작업이 있으면 쌓지 않는다.
    if IngestionJob.objects.filter(
        company=company,
        status__in=(IngestionJob.Status.QUEUED, IngestionJob.Status.RUNNING),
    ).exists():
        return None

    connection = Connection.objects.filter(
        company=company, kind=Connection.Kind.SLACK, disconnected_at__isnull=True
    ).first()
    if connection is None:
        return None

    item_ids = list(
        Item.objects.filter(connection=connection, removed_at__isnull=True)
        .values_list('id', flat=True)
    )
    if not item_ids:
        return None

    # 수집이 먼저다. 새 원문을 가져오면 어차피 뒤이어 처리까지 한다.
    if _is_due(_last_job(company, IngestionJob.Kind.COLLECT), COLLECT_EVERY, now):
        return _enqueue(company, IngestionJob.Kind.COLLECT, item_ids)

    if not _is_due(_last_job(company, IngestionJob.Kind.PROCESS), PROCESS_EVERY, now):
        return None

    # 처리할 것이 없으면 만들지 않는다. 빈 작업이 10분마다 쌓이면 로그만 지저분해진다.
    if not has_pending_work(company):
        return None

    return _enqueue(company, IngestionJob.Kind.PROCESS, item_ids)


# 웹훅은 원문만 저장한다. 아무도 수집 버튼을 누르지 않으면 카드도 규칙도 생기지 않는다.
def enqueue_due_jobs(now=None):
    now = now or timezone.now()
    companies = Company.objects.filter(
        connections__kind=Connection.Kind.SLACK,
        connections__disconnected_at__isnull=True,
    ).distinct()

    jobs = []
    for company in companies:
        job = _due_job(company, now)
        if job is not None:
            jobs.append(job)

    return jobs
