from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from qna.models import Citation, Message, Thread
from sources.ingestion import unfinished_entries
from sources.models import Connection, Identity, Item, RawDocument

from .drafting import DraftResult, DraftRule, draft_entries
from .models import CompanyScope, HandbookEntry, HandbookEvidence
from .queries import live_entries
from .retrieval import search_rules
from .services import seed_default_scopes

VECTOR = [0.1] * 1536


class DeleteEntryTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.scope = CompanyScope.objects.get(
            company=self.company, area_key=CompanyScope.AreaKey.PRODUCT_ENG
        )
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='팀원'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )

        self.entry = self._entry('금요일 오후 배포 금지')
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}/handbook/entries'

    def _entry(self, title, status=HandbookEntry.Status.CONFIRMED, **kwargs):
        confirmed = status == HandbookEntry.Status.CONFIRMED

        return HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title=title,
            body_ko='금요일 오후에는 운영 배포를 하지 않습니다.', status=status,
            origin=HandbookEntry.Origin.SLACK,
            confirmed_at=timezone.now() if confirmed else None,
            **kwargs,
        )

    def delete(self, entry=None):
        return self.client.delete(f'{self.base}/{(entry or self.entry).id}')

    # --- 지우면 어디에서도 보이지 않는다 ---

    def test_owner_deletes_confirmed_entry(self):
        response = self.delete()

        self.assertEqual(response.status_code, 204)
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.deleted_at)

    def test_deleted_entry_is_gone_from_the_list(self):
        self.delete()
        response = self.client.get(self.base)

        self.assertEqual(response.data['items'], [])

    def test_deleted_entry_detail_is_404(self):
        self.delete()

        self.assertEqual(self.client.get(f'{self.base}/{self.entry.id}').status_code, 404)

    def test_deleted_entry_evidence_is_404(self):
        HandbookEvidence.objects.create(
            company=self.company, entry=self.entry,
            quote='배포는 금요일 오후에는 하지 않는 걸로 합시다',
            tag=HandbookEvidence.Tag.SLACK,
        )
        self.delete()

        self.assertEqual(
            self.client.get(f'{self.base}/{self.entry.id}/evidence').status_code, 404
        )

    # 지운 규칙이 팀원 답변의 근거로 계속 나가면 지운 것이 아니다.
    def test_deleted_entry_is_not_searchable(self):
        entry = self._entry('배포 승인', embedding_ko=VECTOR, embedded_at=timezone.now())
        self.assertEqual(len(search_rules(VECTOR, self.company)), 1)

        self.delete(entry)

        self.assertEqual(search_rules(VECTOR, self.company), [])

    def test_scope_count_drops(self):
        self.delete()
        response = self.client.get(f'/api/companies/{self.company.id}/handbook/scopes')
        counts = {item['name']: item['entryCount'] for item in response.data['items']}

        self.assertEqual(counts['Product / Engineering'], 0)

    # 지운 항목을 번역/임베딩 대상으로 계속 붙잡으면 파이프라인이 매번 헛일을 한다.
    def test_pipeline_ignores_deleted_entry(self):
        self.assertEqual(unfinished_entries(self.company), [self.entry])

        self.delete()

        self.assertEqual(unfinished_entries(self.company), [])

    # --- 핸드북에 올라간 것만 지운다 ---

    def test_draft_cannot_be_deleted(self):
        draft = self._entry('초안', status=HandbookEntry.Status.DRAFT)
        response = self.delete(draft)

        self.assertEqual(response.status_code, 400)
        draft.refresh_from_db()
        self.assertIsNone(draft.deleted_at)

    def test_blank_cannot_be_deleted(self):
        blank = self._entry('빈 항목', status=HandbookEntry.Status.BLANK)

        self.assertEqual(self.delete(blank).status_code, 400)

    def test_archived_cannot_be_deleted(self):
        archived = self._entry('보관됨', status=HandbookEntry.Status.ARCHIVED)

        self.assertEqual(self.delete(archived).status_code, 400)

    def test_deleting_twice_is_404(self):
        self.delete()

        self.assertEqual(self.delete().status_code, 404)

    # --- 권한 ---

    def test_member_cannot_delete(self):
        self.client.force_authenticate(user=self.member)

        self.assertEqual(self.delete().status_code, 403)

    def test_other_company_entry_is_404(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_owner = User.objects.create_user(
            email='o@example.com', password='pw', display_name='다른대표'
        )
        Membership.objects.create(
            user=other_owner, company=other, role=Membership.Role.OWNER
        )
        self.client.force_authenticate(user=other_owner)
        response = self.client.delete(
            f'/api/companies/{other.id}/handbook/entries/{self.entry.id}'
        )

        self.assertEqual(response.status_code, 404)

    # --- 지난 답변의 인용은 그대로 ---

    # 행째로 지우면 인용이 null 이 되어 '무엇을 근거로 답했는지'가 소급해서 사라진다.
    def test_past_citation_still_points_at_the_entry(self):
        thread = Thread.objects.create(company=self.company, user=self.member)
        message = Message.objects.create(
            company=self.company, thread=thread, role=Message.Role.AI, body_ko='답변'
        )
        citation = Citation.objects.create(
            company=self.company, message=message, entry=self.entry
        )

        self.delete()
        citation.refresh_from_db()

        self.assertEqual(citation.entry_id, self.entry.id)


@override_settings(OPENAI_API_KEY='test-key')
class DeletedEntryRedraftTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev',
        )
        self.identity = Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U001', external_handle='조상원',
        )
        RawDocument.objects.create(
            company=self.company, item=self.item, external_ref='100.1',
            author_identity=self.identity,
            raw_text='배포는 금요일 오후에는 하지 않는 걸로 합시다',
            content_hash='100.1'.ljust(64, '0'), classified_as='INSTRUCTION',
            occurred_at=timezone.now(), permalink='https://slack/100.1',
        )
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}/handbook/entries'

    def draft(self):
        parsed = DraftResult(rules=[DraftRule(
            title='금요일 오후에는 배포하지 않습니다.',
            title_en='Deployments do not happen on Friday afternoons.',
            body='배포는 금요일 오후에 하지 않습니다.',
            confidence='HIGH',
            citations=[{
                'index': 0, 'quote': '배포는 금요일 오후에는 하지 않는 걸로 합시다',
            }],
        )])
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )
        with patch('handbook.drafting.OpenAI') as client:
            client.return_value.chat.completions.parse.return_value = completion

            return draft_entries(self.company)

    # 원문 슬랙 메시지는 지워도 그대로 남는다.
    # 항목을 행째로 지웠다면 다음 초안 생성이 같은 규칙을 다시 만들어 되살아난다.
    def test_deleted_rule_is_not_drafted_again(self):
        entries, _ = self.draft()
        entry = entries[0]
        entry.status = HandbookEntry.Status.CONFIRMED
        entry.confirmed_at = timezone.now()
        entry.save(update_fields=['status', 'confirmed_at'])

        self.assertEqual(self.client.delete(f'{self.base}/{entry.id}').status_code, 204)

        self.draft()

        self.assertEqual(live_entries(self.company).count(), 0)
        # 되살아나지 않는 이유는 지운 자리가 그대로 남아 있기 때문이다.
        self.assertEqual(HandbookEntry.objects.filter(company=self.company).count(), 1)
