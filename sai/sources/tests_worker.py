from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company

from .models import Connection, IngestionJob, Item
from .worker import claim_job, drain, run_job, work_forever


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
