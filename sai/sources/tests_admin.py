from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company

from .models import Connection, IngestionJob, Item


class SourceAdminTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Alex'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test',
            status=Connection.Status.CONNECTED, external_workspace_id='T001',
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}'

    def job(self, kind=IngestionJob.Kind.COLLECT, job_status=IngestionJob.Status.SUCCEEDED):
        return IngestionJob.objects.create(
            company=self.company, kind=kind, status=job_status, item_ids=[self.item.id]
        )

    # --- 수집 작업 목록 ---

    # 주기 실행이 작업을 자동으로 만든다. 언제 마지막으로 돌았는지 볼 수 있어야 한다.
    def test_jobs_are_listed_newest_first(self):
        first = self.job()
        second = self.job(kind=IngestionJob.Kind.PROCESS)

        response = self.client.get(f'{self.base}/ingestion-jobs')

        self.assertEqual(response.status_code, 200)
        self.assertEqual([i['id'] for i in response.data['items']], [second.id, first.id])
        self.assertEqual(response.data['items'][0]['kind'], 'PROCESS')

    def test_jobs_filtered_by_status(self):
        self.job()
        failed = self.job(job_status=IngestionJob.Status.FAILED)

        response = self.client.get(f'{self.base}/ingestion-jobs?status=FAILED')

        self.assertEqual([i['id'] for i in response.data['items']], [failed.id])

    def test_invalid_job_status(self):
        response = self.client.get(f'{self.base}/ingestion-jobs?status=NOPE')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['field'], 'status')

    def test_jobs_are_paginated(self):
        for _ in range(3):
            self.job()

        response = self.client.get(f'{self.base}/ingestion-jobs?limit=2')

        self.assertEqual(len(response.data['items']), 2)
        self.assertIsNotNone(response.data['nextCursor'])

    def test_other_company_jobs_are_hidden(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        IngestionJob.objects.create(company=other, item_ids=[])

        response = self.client.get(f'{self.base}/ingestion-jobs')

        self.assertEqual(response.data['items'], [])

    def test_member_cannot_list_jobs(self):
        self.client.force_authenticate(user=self.member)

        response = self.client.get(f'{self.base}/ingestion-jobs')

        self.assertEqual(response.status_code, 403)

    # --- 연결 해제 ---

    # 토큰이 바뀌거나 워크스페이스를 옮기면 끊을 방법이 있어야 한다.
    def test_disconnect(self):
        response = self.client.delete(
            f'{self.base}/source-connections/{self.connection.id}'
        )

        self.assertEqual(response.status_code, 204)
        self.connection.refresh_from_db()
        self.assertIsNotNone(self.connection.disconnected_at)

    def test_disconnected_connection_is_not_listed(self):
        self.client.delete(f'{self.base}/source-connections/{self.connection.id}')

        response = self.client.get(f'{self.base}/source-connections')

        self.assertEqual(response.data['items'], [])

    # 끊어도 모아 둔 원문과 채널은 남는다. 다시 이으면 그대로 쓴다.
    def test_disconnect_keeps_channels(self):
        self.client.delete(f'{self.base}/source-connections/{self.connection.id}')

        self.assertTrue(Item.objects.filter(id=self.item.id).exists())

    def test_disconnect_twice_is_404(self):
        self.client.delete(f'{self.base}/source-connections/{self.connection.id}')

        response = self.client.delete(
            f'{self.base}/source-connections/{self.connection.id}'
        )

        self.assertEqual(response.status_code, 404)

    def test_member_cannot_disconnect(self):
        self.client.force_authenticate(user=self.member)

        response = self.client.delete(
            f'{self.base}/source-connections/{self.connection.id}'
        )

        self.assertEqual(response.status_code, 403)

    def test_other_company_connection_is_404(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        Connection.objects.filter(id=self.connection.id).update(company=other)

        response = self.client.delete(
            f'{self.base}/source-connections/{self.connection.id}'
        )

        self.assertEqual(response.status_code, 404)
