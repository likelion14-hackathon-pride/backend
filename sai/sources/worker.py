import logging
import time

from django.db import transaction
from django.utils import timezone

from .ingestion import run_ingestion
from .models import Connection, IngestionJob

logger = logging.getLogger(__name__)

# 큐가 비었을 때 다시 확인하기까지 기다리는 시간.
IDLE_SECONDS = 3


# 대기 중인 작업 하나를 잡아 RUNNING 으로 바꾼다.
# skip_locked 덕분에 워커를 여러 개 띄워도 같은 작업을 두 번 잡지 않는다.
def claim_job():
    with transaction.atomic():
        job = (
            IngestionJob.objects.select_for_update(skip_locked=True)
            .filter(status=IngestionJob.Status.QUEUED)
            .order_by('id')
            .first()
        )
        if job is None:
            return None

        job.status = IngestionJob.Status.RUNNING
        job.save(update_fields=['status'])

    return job


def _fail(job, code):
    job.status = IngestionJob.Status.FAILED
    job.errors = [{'scope': 'worker', 'code': code}]
    job.progress = 100
    job.completed_at = timezone.now()
    job.save(update_fields=['status', 'errors', 'progress', 'completed_at'])

    return job


# 작업 하나를 끝까지 실행한다.
# 여기서 예외가 새어 나가면 워커 루프가 죽고 큐가 멈춘다. 전부 잡아서 작업만 실패시킨다.
def run_job(job):
    connection = Connection.objects.filter(
        company_id=job.company_id,
        kind=Connection.Kind.SLACK,
        disconnected_at__isnull=True,
    ).first()
    if connection is None:
        return _fail(job, 'slack_not_connected')

    try:
        return run_ingestion(job, connection)
    except Exception:
        logger.exception('수집 작업 실패 job=%s', job.id)
        return _fail(job, 'unexpected_error')


# 큐에 쌓인 것을 다 처리하고 돌아온다. 처리한 작업 수 반환.
def drain(limit=None):
    processed = 0
    while limit is None or processed < limit:
        job = claim_job()
        if job is None:
            break
        run_job(job)
        processed += 1

    return processed


# 워커 프로세스 본체.
def work_forever(idle_seconds=IDLE_SECONDS, stop_after_idle=None):
    idle_rounds = 0
    while True:
        if drain():
            idle_rounds = 0
            continue

        idle_rounds += 1
        if stop_after_idle is not None and idle_rounds >= stop_after_idle:
            return

        time.sleep(idle_seconds)
