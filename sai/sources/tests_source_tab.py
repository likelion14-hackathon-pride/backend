from django.db import connection as db
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry

from .models import Connection, Item


class SourceTabTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.owner = User.objects.create_user(
            email='o@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.slack = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.url = f'/api/companies/{self.company.id}/source-connections'

    def item(self, external_id, connection=None, synced=None, removed=None):
        return Item.objects.create(
            company=self.company, connection=connection or self.slack,
            external_id=external_id, label=f'#{external_id}',
            last_synced_at=synced, removed_at=removed,
        )

    def entry(self, origin, status=HandbookEntry.Status.CONFIRMED, title='규칙'):
        return HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title=title, body_ko='본문',
            status=status, origin=origin,
        )

    def sources(self):
        return {s['provider']: s for s in self.client.get(self.url).data['items']}

    # --- 수집 대상 수 ---

    def test_counts_collection_targets(self):
        self.item('C001')
        self.item('C002')

        self.assertEqual(self.sources()['SLACK']['resourceCount'], 2)

    def test_removed_targets_are_not_counted(self):
        self.item('C001')
        self.item('C002', removed=timezone.now())

        self.assertEqual(self.sources()['SLACK']['resourceCount'], 1)

    def test_last_sync_is_the_most_recent(self):
        self.item('C001', synced=timezone.now() - timezone.timedelta(days=1))
        self.item('C002', synced=timezone.now())

        self.assertIsNotNone(self.sources()['SLACK']['lastSyncedAt'])

    def test_never_synced_has_no_time(self):
        self.item('C001')

        self.assertIsNone(self.sources()['SLACK']['lastSyncedAt'])

    # --- 추출된 항목 수 ---

    def test_counts_entries_made_from_the_source(self):
        self.entry(HandbookEntry.Origin.SLACK, title='슬랙 규칙 1')
        self.entry(HandbookEntry.Origin.SLACK, title='슬랙 규칙 2')

        self.assertEqual(self.sources()['SLACK']['extractedCount'], 2)

    def test_drafts_are_still_extracted(self):
        self.entry(HandbookEntry.Origin.SLACK, status=HandbookEntry.Status.DRAFT)

        self.assertEqual(self.sources()['SLACK']['extractedCount'], 1)

    # 대표가 거절한 것을 세면 소스가 실제보다 쓸모 있어 보인다.
    def test_rejected_entries_are_not_counted(self):
        self.entry(HandbookEntry.Origin.SLACK, status=HandbookEntry.Status.ARCHIVED)

        self.assertEqual(self.sources()['SLACK']['extractedCount'], 0)

    def test_entries_of_another_source_do_not_leak(self):
        github = Connection.objects.create(
            company=self.company, kind=Connection.Kind.GITHUB, github_app_id='1'
        )
        self.item('repo', connection=github)
        self.entry(HandbookEntry.Origin.GITHUB, title='깃허브 규칙')

        sources = self.sources()

        self.assertEqual(sources['GITHUB']['extractedCount'], 1)
        self.assertEqual(sources['SLACK']['extractedCount'], 0)

    # 로컬 파일에서 만든 항목은 origin 이 FILE 이다.
    def test_local_files_map_to_file_origin(self):
        Connection.objects.create(company=self.company, kind=Connection.Kind.LOCAL)
        self.entry(HandbookEntry.Origin.FILE, title='업로드 규칙')

        self.assertEqual(self.sources()['LOCAL']['extractedCount'], 1)

    # Day 0 답변으로 만든 항목은 어느 소스에서도 나온 것이 아니다.
    def test_owner_written_entries_belong_to_no_source(self):
        self.entry(HandbookEntry.Origin.ONBOARDING, title='Day 0 규칙')

        self.assertEqual(self.sources()['SLACK']['extractedCount'], 0)

    # --- 쿼리 수 ---

    # 소스마다 세면 소스 수만큼 쿼리가 늘어난다.
    def test_query_count_does_not_grow(self):
        self.item('C001')
        with CaptureQueriesContext(db) as one:
            self.sources()

        Connection.objects.create(
            company=self.company, kind=Connection.Kind.GITHUB, github_app_id='1'
        )
        Connection.objects.create(company=self.company, kind=Connection.Kind.LOCAL)
        with CaptureQueriesContext(db) as many:
            self.sources()

        self.assertEqual(len(many.captured_queries), len(one.captured_queries))

    def test_member_cannot_see_sources(self):
        member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh'
        )
        Membership.objects.create(
            user=member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client.force_authenticate(user=member)

        self.assertEqual(self.client.get(self.url).status_code, 403)
