import logging
import time
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from cards.todos import purge_done
from qna.services import collect_pending_answers

from .github_ingestion import run_github_ingestion
from .ingestion import run_ingestion
from .local_ingestion import run_local_ingestion
from .models import Connection, IngestionJob, Item
from .scheduling import enqueue_due_jobs

logger = logging.getLogger(__name__)

IDLE_SECONDS = 3

# 밀린 작업을 큐에 넣는 주기. 큐 확인만큼 자주 할 일이 아니다.
SCHEDULE_EVERY = timedelta(seconds=60)

# 이보다 오래 RUNNING 인 작업은 워커가 죽은 것으로 본다.
# 채널이 많으면 수집만 몇 분 걸리므로 넉넉히 잡는다.
STALE_AFTER = timedelta(minutes=30)


# 워커가 중간에 내려가면 status 가 RUNNING 에 멈춘 채 아무도 손대지 않는다.
# 화면에서는 영원히 진행 중으로 보이고 다시 실행할 방법도 없다.
def reap_stale_jobs(now=None):
    cutoff = (now or timezone.now()) - STALE_AFTER
    # started_at 이 비어 있는 작업도 거둔다. started_at 컬럼이 생기기 전에 만들어진 행이
    # RUNNING 으로 남아 있으면 started_at__lt 는 NULL 을 걸러 내므로 영원히 정리되지 않고,
    # 그 회사의 주기 작업이 RUNNING 가드에 막혀 통째로 멈춘다.
    stale = list(
        IngestionJob.objects.filter(
            Q(started_at__lt=cutoff) | Q(started_at__isnull=True, created_at__lt=cutoff),
            status=IngestionJob.Status.RUNNING,
        )
    )
    for job in stale:
        logger.warning('멈춘 작업을 실패로 정리합니다 job=%s', job.id)
        _fail(job, 'worker_died')

    return len(stale)


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


# 여기서 예외가 새어 나가면 워커 루프가 죽고 큐가 멈춘다. 전부 잡아서 작업만 실패시킨다.
def run_job(job):
    items = list(
        Item.objects.filter(
            company_id=job.company_id,
            id__in=job.item_ids or [],
            removed_at__isnull=True,
        ).select_related('connection')
    )
    if not items:
        # 기존 Slack 작업이 연결 삭제로 실패한 경우의 오류 코드는 유지한다.
        connection_exists = Connection.objects.filter(
            company_id=job.company_id,
            kind=Connection.Kind.SLACK,
            disconnected_at__isnull=True,
        ).exists()
        return _fail(job, 'source_item_not_found' if connection_exists else 'slack_not_connected')

    connection_ids = {item.connection_id for item in items}
    if len(connection_ids) != 1:
        return _fail(job, 'mixed_source_connections')

    connection = items[0].connection
    if connection.disconnected_at is not None:
        return _fail(job, 'source_not_connected')

    if connection.kind == Connection.Kind.GITHUB:
        runner = run_github_ingestion
    elif connection.kind == Connection.Kind.SLACK:
        runner = run_ingestion
    elif connection.kind == Connection.Kind.LOCAL:
        runner = run_local_ingestion
    else:
        return _fail(job, 'source_not_supported')

    active_connection = Connection.objects.filter(
        id=connection.id,
        company_id=job.company_id,
        disconnected_at__isnull=True,
    ).first()
    if active_connection is None:
        return _fail(job, 'source_not_connected')

    # 시작만 있고 종료가 없는 job id 가 곧 멈춘 지점이다.
    logger.info(
        '작업 시작 job=%s kind=%s company=%s source=%s items=%d',
        job.id, job.kind, job.company_id, connection.kind, len(items),
    )
    started = time.monotonic()
    try:
        result = runner(job, active_connection)
    except Exception:
        logger.exception(
            '수집 작업 실패 job=%s 소요=%.1fs', job.id, time.monotonic() - started
        )
        return _fail(job, 'unexpected_error')

    logger.info(
        '작업 종료 job=%s status=%s 소요=%.1fs',
        job.id, getattr(result, 'status', None), time.monotonic() - started,
    )

    return result


def drain(limit=None):
    processed = 0
    while limit is None or processed < limit:
        job = claim_job()
        if job is None:
            break
        run_job(job)
        processed += 1

    return processed


# 답장 회수가 터져도 큐잉까지 같이 멈추면 안 된다.
def _collect_answers(now):
    try:
        return collect_pending_answers(now)
    except Exception:
        logger.exception('답장 회수 실패')

        return 0


# 주기 정리와 큐잉. 새로 넣은 작업 목록을 돌려준다.
def _run_schedule(now):
    started = time.monotonic()
    reaped = reap_stale_jobs(now)
    purged = purge_done(now)
    answers = _collect_answers(now)
    jobs = enqueue_due_jobs(now)
    for job in jobs:
        logger.info('주기 작업을 큐에 넣었습니다 job=%s kind=%s', job.id, job.kind)

    elapsed = time.monotonic() - started
    logger.info(
        '스케줄링 완료 reap=%d purge=%d answer=%d enqueue=%d 소요=%.1fs',
        reaped, purged, answers, len(jobs), elapsed,
    )
    # 한 바퀴가 주기보다 오래 걸리면 정리가 큐잉을 따라가지 못한다는 뜻이다.
    # 예전에는 이 상태에서 sleep 에 영영 닿지 못하고 CPU 를 100% 물고 돌았다.
    if elapsed >= SCHEDULE_EVERY.total_seconds():
        logger.warning(
            '스케줄링이 주기(%ds)보다 오래 걸립니다 소요=%.1fs',
            SCHEDULE_EVERY.total_seconds(), elapsed,
        )

    return jobs


def work_forever(idle_seconds=IDLE_SECONDS, stop_after_idle=None):
    idle_rounds = 0
    next_schedule = timezone.now()
    while True:
        if drain():
            idle_rounds = 0
            continue

        # 큐가 빈 김에 처리한다. 바쁠 때 끼어들지 않는다.
        if timezone.now() >= next_schedule:
            jobs = _run_schedule(timezone.now())
            # 다음 주기는 일을 마친 시각부터 센다. 시작 시각으로 재면 스케줄링이 걸린
            # 시간만큼 대기가 줄고, 한 바퀴가 주기를 넘기는 순간 sleep 에 닿지 못한다.
            next_schedule = timezone.now() + SCHEDULE_EVERY
            if jobs:
                # 방금 넣은 작업을 다음 바퀴에서 바로 집는다.
                # 아무것도 넣지 않았으면 쉬어야 한다. 여기서 무조건 continue 하면
                # 큐가 빈 채로 루프만 도는 구간이 생긴다.
                idle_rounds = 0
                continue

        idle_rounds += 1
        if stop_after_idle is not None and idle_rounds >= stop_after_idle:
            return

        time.sleep(idle_seconds)
