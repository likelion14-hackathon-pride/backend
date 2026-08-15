from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company

from .models import Connection, IngestionJob, Item
from .worker import STALE_AFTER, claim_job, drain, reap_stale_jobs, run_job, work_forever


class WorkerTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev',
        )

    def job(self, **extra):
        return IngestionJob.objects.create(
            company=self.company, item_ids=[self.item.id], **extra
        )

    # --- 작업 선점 ---

    def test_claims_queued_job_and_marks_running(self):
        job = self.job()

        claimed = claim_job()

        self.assertEqual(claimed, job)
        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.RUNNING)

    def test_nothing_to_claim(self):
        self.assertIsNone(claim_job())

    def test_claim_records_start_time(self):
        self.job()

        self.assertIsNotNone(claim_job().started_at)

    # --- 작업 종류 ---

    # PROCESS 는 웹훅이 이미 받아 둔 원문만 처리한다. 슬랙을 다시 읽으면 낭비다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_process_job_does_not_call_slack(self):
        job = self.job(kind=IngestionJob.Kind.PROCESS)

        with patch('sources.ingestion.SlackClient') as slack, \
             patch('sources.ingestion.has_pending_work', return_value=True), \
             patch('sources.ingestion.classify_documents', return_value=(0, [])), \
             patch('sources.ingestion.sync_chunks', return_value=(0, [])), \
             patch('sources.ingestion.draft_entries', return_value=([], [])), \
             patch('sources.ingestion.generate_cards', return_value=([], [])):
            run_job(job)

        self.assertFalse(slack.called)
        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.SUCCEEDED)

    # 초안 생성은 매번 규칙 문서 전체를 다시 부른다. 바뀐 게 없으면 부르면 안 된다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_nothing_pending_skips_the_ai_stages(self):
        job = self.job(kind=IngestionJob.Kind.PROCESS)

        with patch('sources.ingestion.has_pending_work', return_value=False), \
             patch('sources.ingestion.classify_documents') as classify, \
             patch('sources.ingestion.draft_entries') as draft:
            run_job(job)

        self.assertFalse(classify.called)
        self.assertFalse(draft.called)
        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.SUCCEEDED)
        self.assertEqual(job.progress, 100)

    # 확정만 되고 검색되지 않는 규칙을 여기서 거둔다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_unfinished_entries_are_finalized(self):
        job = self.job(kind=IngestionJob.Kind.PROCESS)

        with patch('sources.ingestion.has_pending_work', return_value=True), \
             patch('sources.ingestion.unfinished_entries', return_value=['entry']), \
             patch('sources.ingestion.finalize_entries',
                   return_value={'translated': 1, 'embedded': 1, 'errors': []}) as finalize, \
             patch('sources.ingestion.classify_documents', return_value=(0, [])), \
             patch('sources.ingestion.sync_chunks', return_value=(0, [])), \
             patch('sources.ingestion.draft_entries', return_value=([], [])), \
             patch('sources.ingestion.generate_cards', return_value=([], [])):
            run_job(job)

        self.assertEqual(finalize.call_args.args[0], ['entry'])

    @override_settings(OPENAI_API_KEY='test-key')
    def test_finalize_failure_is_recorded(self):
        job = self.job(kind=IngestionJob.Kind.PROCESS)

        with patch('sources.ingestion.has_pending_work', return_value=True), \
             patch('sources.ingestion.unfinished_entries', return_value=['entry']), \
             patch('sources.ingestion.finalize_entries', return_value={
                 'translated': 0, 'embedded': 0,
                 'errors': [{'step': 'embed', 'code': 'RateLimitError'}],
             }), \
             patch('sources.ingestion.classify_documents', return_value=(0, [])), \
             patch('sources.ingestion.sync_chunks', return_value=(0, [])), \
             patch('sources.ingestion.draft_entries', return_value=([], [])), \
             patch('sources.ingestion.generate_cards', return_value=([], [])):
            run_job(job)

        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.PARTIAL)
        self.assertEqual(job.errors[0]['scope'], 'finalize')

    # --- 죽은 워커가 남긴 작업 ---

    # 워커가 중간에 내려가면 RUNNING 인 채로 남아 화면에서 영원히 진행 중으로 보인다.
    def test_stale_running_job_is_failed(self):
        job = self.job(
            status=IngestionJob.Status.RUNNING,
            started_at=timezone.now() - STALE_AFTER - timedelta(minutes=1),
        )

        self.assertEqual(reap_stale_jobs(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.FAILED)
        self.assertEqual(job.errors[0]['code'], 'worker_died')
        self.assertIsNotNone(job.completed_at)

    # 아직 돌고 있는 작업을 죽이면 안 된다.
    def test_running_job_within_the_window_is_left_alone(self):
        job = self.job(status=IngestionJob.Status.RUNNING, started_at=timezone.now())

        self.assertEqual(reap_stale_jobs(), 0)
        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.RUNNING)

    # 큐가 빌 때마다 정리한다.
    def test_worker_loop_reaps(self):
        job = self.job(
            status=IngestionJob.Status.RUNNING,
            started_at=timezone.now() - STALE_AFTER - timedelta(minutes=1),
        )

        work_forever(idle_seconds=0, stop_after_idle=1)

        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.FAILED)

    # 이미 잡힌 작업을 다른 워커가 또 잡으면 안 된다.
    def test_running_job_is_not_claimed_again(self):
        self.job(status=IngestionJob.Status.RUNNING)

        self.assertIsNone(claim_job())

    def test_claims_oldest_first(self):
        first = self.job()
        self.job()

        self.assertEqual(claim_job(), first)

    # --- 실행 ---

    def test_runs_job(self):
        job = self.job()

        with patch('sources.worker.run_ingestion') as run:
            run_job(claim_job())

        run.assert_called_once()
        self.assertEqual(run.call_args[0][0].id, job.id)
        self.assertEqual(run.call_args[0][1], self.connection)

    # 워커가 예외로 죽으면 큐 전체가 멈춘다. 작업만 실패시키고 살아남아야 한다.
    def test_unexpected_error_fails_only_the_job(self):
        job = self.job()

        with patch('sources.worker.run_ingestion', side_effect=RuntimeError('boom')):
            run_job(claim_job())

        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.FAILED)
        self.assertEqual(job.errors[0]['code'], 'unexpected_error')
        self.assertIsNotNone(job.completed_at)

    def test_missing_connection_fails_job(self):
        self.connection.delete()
        job = self.job()

        run_job(claim_job())

        job.refresh_from_db()
        self.assertEqual(job.status, IngestionJob.Status.FAILED)
        self.assertEqual(job.errors[0]['code'], 'slack_not_connected')

    # --- 큐 비우기 ---

    def test_drain_processes_all(self):
        self.job()
        self.job()

        with patch('sources.worker.run_ingestion'):
            processed = drain()

        self.assertEqual(processed, 2)
        self.assertFalse(
            IngestionJob.objects.filter(status=IngestionJob.Status.QUEUED).exists()
        )

    def test_drain_respects_limit(self):
        self.job()
        self.job()

        with patch('sources.worker.run_ingestion'):
            processed = drain(limit=1)

        self.assertEqual(processed, 1)
        self.assertEqual(
            IngestionJob.objects.filter(status=IngestionJob.Status.QUEUED).count(), 1
        )

    def test_drain_on_empty_queue(self):
        self.assertEqual(drain(), 0)

    def test_work_forever_stops_when_idle(self):
        self.job()

        with patch('sources.worker.run_ingestion'), patch('sources.worker.time.sleep'):
            work_forever(idle_seconds=0, stop_after_idle=1)

        self.assertFalse(
            IngestionJob.objects.filter(status=IngestionJob.Status.QUEUED).exists()
        )


class IngestionJobQueueingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        Item.objects.create(
            company=self.company, connection=connection, external_id='C001', label='#dev'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.url = f'/api/companies/{self.company.id}/ingestion-jobs'

    # 요청은 즉시 돌아오고 실제 처리는 워커가 한다.
    def test_returns_202_without_running(self):
        with patch('sources.worker.run_ingestion') as run:
            response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['status'], 'QUEUED')
        self.assertEqual(response.data['progress'], 0)
        run.assert_not_called()

    def test_queued_job_is_picked_up_by_worker(self):
        self.client.post(self.url, {}, format='json')

        with patch('sources.worker.run_ingestion') as run:
            drain()

        run.assert_called_once()

    # 워커 없이 로컬에서 확인할 때를 위한 우회로.
    @override_settings(INGESTION_RUN_INLINE=True)
    def test_inline_mode_runs_immediately(self):
        with patch('sources.worker.run_ingestion') as run:
            response = self.client.post(self.url, {}, format='json')

        self.assertEqual(response.status_code, 202)
        run.assert_called_once()
