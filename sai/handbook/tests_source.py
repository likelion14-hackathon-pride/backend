from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company

from .models import CompanyScope, HandbookEntry, HandbookEvidence


class EntrySourceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh', ui_language='en'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.url = f'/api/companies/{self.company.id}/handbook/entries'

    def entry(self, title='금요일 오후 배포 금지'):
        return HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title=title,
            body_ko='금요일 오후에는 배포하지 않습니다.',
            body_en='No deploys on Friday afternoon.',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
        )

    def evidence(self, entry, label='#dev', when=None, quote='금요일 배포는 다음 주로'):
        return HandbookEvidence.objects.create(
            company=self.company, entry=entry, tag=HandbookEvidence.Tag.SLACK,
            quote=quote, source_label=label, speaker_name='조상원',
            permalink=f'https://slack/{label}', occurred_at=when or timezone.now(),
        )

    def listed(self):
        return self.client.get(self.url).data['items']

    # --- 목록에 붙는 출처 ---

    # 접힌 행에도 출처가 보인다. 항목마다 근거를 따로 부르지 않아도 되어야 한다.
    def test_entry_carries_its_source(self):
        self.evidence(self.entry())

        source = self.listed()[0]['source']

        self.assertEqual(source['label'], '#dev')
        self.assertEqual(source['tag'], 'SLACK')
        self.assertEqual(source['speakerName'], '조상원')
        self.assertEqual(source['permalink'], 'https://slack/#dev')
        self.assertEqual(source['count'], 1)

    def test_source_counts_every_evidence(self):
        entry = self.entry()
        self.evidence(entry)
        self.evidence(entry, quote='핫픽스는 예외로')

        self.assertEqual(self.listed()[0]['source']['count'], 2)

    # 근거 상세와 같은 순서를 쓴다. 두 화면이 다른 출처를 가리키면 안 된다.
    def test_the_earliest_evidence_represents_the_entry(self):
        entry = self.entry()
        self.evidence(entry, label='#late', when=timezone.now())
        self.evidence(
            entry, label='#early', when=timezone.now() - timezone.timedelta(days=2)
        )

        self.assertEqual(self.listed()[0]['source']['label'], '#early')

    def test_entry_without_evidence_has_no_source(self):
        self.entry()

        self.assertIsNone(self.listed()[0]['source'])

    def test_detail_carries_the_source_too(self):
        entry = self.entry()
        self.evidence(entry)

        response = self.client.get(f'{self.url}/{entry.id}')

        self.assertEqual(response.data['source']['label'], '#dev')

    # 항목마다 근거를 찾으면 목록 한 번에 쿼리가 항목 수만큼 늘어난다.
    def test_query_count_does_not_grow(self):
        self.evidence(self.entry('규칙 0'))
        with CaptureQueriesContext(connection) as one:
            self.listed()

        for index in range(1, 5):
            self.evidence(self.entry(f'규칙 {index}'))
        with CaptureQueriesContext(connection) as many:
            self.listed()

        self.assertEqual(len(many.captured_queries), len(one.captured_queries))

    # --- 원문으로 가는 길 ---

    # 팀원이 원문을 열 수 있어야 이중 언어 열람이 성립한다.
    def test_a_member_can_open_the_original(self):
        entry = self.entry()
        self.evidence(entry)

        response = self.client.get(f'{self.url}/{entry.id}/evidence')

        self.assertEqual(response.status_code, 200)
        item = response.data['items'][0]
        self.assertEqual(item['quote'], '금요일 배포는 다음 주로')
        self.assertEqual(item['permalink'], 'https://slack/#dev')
