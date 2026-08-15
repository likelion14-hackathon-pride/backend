import logging
import time
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .ingestion import run_ingestion
from .models import Connection, IngestionJob
from .scheduling import enqueue_due_jobs

logger = logging.getLogger(__name__)

# 큐가 비었을 때 다시 확인하기까지 기다리는 시간.
IDLE_SECONDS = 3

# 밀린 작업을 큐에 넣고 멈춘 작업을 정리하는 주기.
# 큐 확인만큼 자주 할 일이 아니다.
SCHEDULE_EVERY = timedelta(seconds=60)

# 이보다 오래 RUNNING 인 작업은 워커가 죽은 것으로 본다.
# 채널이 많으면 수집만 몇 분 걸리므로 넉넉히 잡는다.
STALE_AFTER = timedelta(minutes=30)


# 잡은 채로 죽은 작업을 정리한다.
# 워커가 중간에 내려가면 status 가 RUNNING 에 멈춘 채 아무도 손대지 않는다.
# 화면에서는 영원히 진행 중으로 보이고 다시 실행할 방법도 없다.
def reap_stale_jobs(now=None):
    cutoff = (now or timezone.now()) - STALE_AFTER
    stale = list(
        IngestionJob.objects.filter(
            status=IngestionJob.Status.RUNNING, started_at__lt=cutoff
        )
    )
    for job in stale:
        logger.warning('멈춘 작업을 실패로 정리합니다 job=%s', job.id)
        _fail(job, 'worker_died')

    return len(stale)


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
        job.started_at = timezone.now()
        job.save(update_fields=['status', 'started_at'])

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
    next_schedule = timezone.now()
    while True:
        if drain():
            idle_rounds = 0
            continue

        # 큐가 빈 김에 처리한다. 바쁠 때 끼어들지 않는다.
        now = timezone.now()
        if now >= next_schedule:
            reap_stale_jobs(now)
            for job in enqueue_due_jobs(now):
                logger.info('주기 작업을 큐에 넣었습니다 job=%s kind=%s', job.id, job.kind)
            next_schedule = now + SCHEDULE_EVERY
            # 방금 넣은 작업을 다음 바퀴에서 바로 집는다.
            continue

        idle_rounds += 1
        if stop_after_idle is not None and idle_rounds >= stop_after_idle:
            return

        time.sleep(idle_seconds)
