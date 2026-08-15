from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone
from openai import OpenAIError
from rest_framework.test import APIClient

from accounts.models import Membership, User
from accounts.serializers import OwnerSignupSerializer
from companies.models import Company
from sources.models import Connection, Identity, Item, RawDocument

from .drafting import DraftResult, DraftRule, draft_entries
from .finalizing import Translation, TranslationResult, finalize_entries
from .models import CompanyScope, HandbookEntry, HandbookEvidence
from .services import DEFAULT_COMPANY_SCOPES, seed_default_scopes


class SeedDefaultScopesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')

    def test_creates_all_company_scopes(self):
        seed_default_scopes(self.company)

        scopes = CompanyScope.objects.filter(company=self.company)
        self.assertEqual(scopes.count(), len(DEFAULT_COMPANY_SCOPES))
        self.assertEqual(
            set(scopes.values_list('area_key', flat=True)),
            {area_key for area_key, _, _ in DEFAULT_COMPANY_SCOPES},
        )
        self.assertTrue(all(scope.kind == CompanyScope.Kind.COMPANY for scope in scopes))

    # 회사 생성 경로가 여러 곳이 되어도 중복이 생기지 않아야 한다.
    def test_is_idempotent(self):
        seed_default_scopes(self.company)
        seed_default_scopes(self.company)

        self.assertEqual(
            CompanyScope.objects.filter(company=self.company).count(),
            len(DEFAULT_COMPANY_SCOPES),
        )

    def test_area_key_is_unique_per_company_only(self):
        seed_default_scopes(self.company)
        other = Company.objects.create(name='다른회사', code='TESTCODE2')

        # 다른 회사는 같은 area_key를 가질 수 있다.
        seed_default_scopes(other)
        self.assertEqual(
            CompanyScope.objects.filter(company=other).count(),
            len(DEFAULT_COMPANY_SCOPES),
        )

        # 같은 회사 안에서는 중복 불가.
        with self.assertRaises(IntegrityError):
            CompanyScope.objects.create(
                company=self.company,
                kind=CompanyScope.Kind.COMPANY,
                area_key=CompanyScope.AreaKey.SECURITY,
                name='중복 Security',
            )

    # PROJECT 범위는 area_key가 null이라 unique 제약에 걸리지 않아야 한다.
    def test_project_scopes_are_not_limited(self):
        seed_default_scopes(self.company)

        for name in ('payment-api', 'admin-web', 'landing'):
            CompanyScope.objects.create(
                company=self.company, kind=CompanyScope.Kind.PROJECT, name=name
            )

        self.assertEqual(
            CompanyScope.objects.filter(
                company=self.company, kind=CompanyScope.Kind.PROJECT
            ).count(),
            3,
        )


class CreateProjectScopeTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)

        self.owner = User.objects.create_user(email='owner@example.com', password='pw', display_name='대표')
        Membership.objects.create(user=self.owner, company=self.company, role=Membership.Role.OWNER)

        self.member = User.objects.create_user(email='member@example.com', password='pw', display_name='팀원')
        Membership.objects.create(user=self.member, company=self.company, role=Membership.Role.MEMBER)

        self.url = f'/api/companies/{self.company.id}/handbook/scopes'
        self.client = APIClient()

    def post(self, user, payload):
        self.client.force_authenticate(user=user)
        return self.client.post(self.url, payload, format='json')

    def test_owner_creates_project_scope(self):
        response = self.post(self.owner, {'kind': 'PROJECT', 'name': 'payment-api', 'description': '결제 시스템'})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['kind'], 'PROJECT')
        self.assertEqual(response.data['name'], 'payment-api')
        # 프로젝트 범위는 회사규칙 카테고리를 갖지 않는다.
        self.assertIsNone(response.data['areaKey'])

    def test_multiple_project_scopes_allowed(self):
        for name in ('payment-api', 'admin-web', 'landing'):
            self.assertEqual(self.post(self.owner, {'kind': 'PROJECT', 'name': name}).status_code, 201)

        self.assertEqual(
            CompanyScope.objects.filter(company=self.company, kind=CompanyScope.Kind.PROJECT).count(), 3
        )

    def test_company_scope_cannot_be_created(self):
        response = self.post(self.owner, {'kind': 'COMPANY', 'name': '새 회사 규칙'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(CompanyScope.objects.filter(company=self.company).count(), len(DEFAULT_COMPANY_SCOPES))

    def test_duplicate_name_rejected(self):
        self.post(self.owner, {'kind': 'PROJECT', 'name': 'payment-api'})
        response = self.post(self.owner, {'kind': 'PROJECT', 'name': 'Payment-API'})

        self.assertEqual(response.status_code, 400)

    # 시딩된 회사 규칙 범위와 같은 이름도 막혀야 한다.
    def test_name_colliding_with_seeded_scope_rejected(self):
        response = self.post(self.owner, {'kind': 'PROJECT', 'name': 'Security'})

        self.assertEqual(response.status_code, 400)

    def test_member_cannot_create(self):
        response = self.post(self.member, {'kind': 'PROJECT', 'name': 'payment-api'})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(CompanyScope.objects.filter(name='payment-api').exists())

    def test_outsider_cannot_create(self):
        outsider = User.objects.create_user(email='out@example.com', password='pw', display_name='외부인')
        response = self.post(outsider, {'kind': 'PROJECT', 'name': 'payment-api'})

        self.assertEqual(response.status_code, 403)


class DraftEntriesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev', scope=self.project,
        )
        self.identity = Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U001', external_handle='조상원',
        )
        self.document = self._document('100.1', '배포는 금요일 오후에는 하지 않는 걸로 합시다')

    def _document(self, ref, text, item=None, classified='INSTRUCTION'):
        return RawDocument.objects.create(
            company=self.company, item=item or self.item, external_ref=ref,
            author_identity=self.identity, raw_text=text,
            content_hash=ref.ljust(64, '0'), classified_as=classified,
            occurred_at=timezone.now(), permalink=f'https://slack/{ref}',
        )

    def draft(self, rules):
        parsed = DraftResult(rules=[DraftRule(**rule) for rule in rules])
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )
        with patch('handbook.drafting.OpenAI') as client:
            client.return_value.chat.completions.parse.return_value = completion
            return draft_entries(self.company)

    @override_settings(OPENAI_API_KEY='test-key')
    def test_creates_draft_with_evidence(self):
        entries, errors = self.draft([{
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다.',
            'confidence': 'HIGH',
            'citations': [{'index': 0, 'quote': '배포는 금요일 오후에는 하지 않는 걸로 합시다'}],
        }])

        self.assertEqual((len(entries), errors), (1, []))
        entry = entries[0]
        self.assertEqual(entry.status, HandbookEntry.Status.DRAFT)
        self.assertEqual(entry.origin, HandbookEntry.Origin.SLACK)
        self.assertEqual(entry.confidence, 'HIGH')
        # 채널에 연결된 지식공간을 그대로 물려받는다.
        self.assertEqual(entry.scope_id, self.project.id)

        evidence = entry.evidences.get()
        self.assertEqual(evidence.tag, HandbookEvidence.Tag.SLACK)
        self.assertEqual(evidence.source_label, '#dev')
        self.assertEqual(evidence.speaker_name, '조상원')
        self.assertEqual(evidence.document_id, self.document.id)

    # 모델이 지어낸 인용은 버린다. 근거 없는 규칙을 만들지 않기 위함.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_fabricated_quote_is_dropped(self):
        entries, _ = self.draft([{
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다.',
            'confidence': 'HIGH',
            'citations': [{'index': 0, 'quote': '이런 말은 원문에 없습니다'}],
        }])

        self.assertEqual(entries, [])
        self.assertFalse(HandbookEntry.objects.exists())

    # 일부만 지어낸 경우 검증을 통과한 인용만 남는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_partially_valid_citations(self):
        self._document('100.2', '핫픽스는 예외로 하겠습니다')
        entries, _ = self.draft([{
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다. 핫픽스는 예외입니다.',
            'confidence': 'HIGH',
            'citations': [
                {'index': 0, 'quote': '금요일 오후에는 하지 않는'},
                {'index': 1, 'quote': '없는 인용'},
            ],
        }])

        self.assertEqual(entries[0].evidences.count(), 1)

    # 따옴표로 감싸서 돌려주는 경우가 있어 벗겨내고 대조한다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_quote_surrounded_by_quotation_marks(self):
        entries, _ = self.draft([{
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다.',
            'confidence': 'HIGH',
            'citations': [{'index': 0, 'quote': '"금요일 오후에는 하지 않는"'}],
        }])

        self.assertEqual(entries[0].evidences.get().quote, '금요일 오후에는 하지 않는')

    # 같은 제목이면 새로 만들지 않고 기존 초안을 갱신한다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_rerun_updates_existing_draft(self):
        rule = {
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다.',
            'confidence': 'HIGH',
            'citations': [{'index': 0, 'quote': '배포는 금요일 오후에는 하지 않는 걸로 합시다'}],
        }
        self.draft([rule])
        self.draft([{**rule, 'body': '금요일 오후 배포는 금지입니다.'}])

        self.assertEqual(HandbookEntry.objects.count(), 1)
        entry = HandbookEntry.objects.get()
        self.assertEqual(entry.body_ko, '금요일 오후 배포는 금지입니다.')
        # 근거도 중복되지 않는다.
        self.assertEqual(entry.evidences.count(), 1)

    # 대표가 확정한 항목은 재실행이 덮어쓰지 않는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_confirmed_entry_is_never_overwritten(self):
        rule = {
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다.',
            'confidence': 'HIGH',
            'citations': [{'index': 0, 'quote': '배포는 금요일 오후에는 하지 않는 걸로 합시다'}],
        }
        self.draft([rule])
        HandbookEntry.objects.update(status=HandbookEntry.Status.CONFIRMED, body_ko='대표가 고친 내용')

        self.draft([{**rule, 'body': 'AI가 다시 쓴 내용'}])

        entry = HandbookEntry.objects.get()
        self.assertEqual(entry.body_ko, '대표가 고친 내용')
        self.assertEqual(entry.status, HandbookEntry.Status.CONFIRMED)

    # 채널에 지식공간이 없으면 회사 전반 규칙으로 보낸다. scope는 NOT NULL이라 대안이 없다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_unmapped_channel_falls_back_to_company_scope(self):
        self.item.scope = None
        self.item.save()

        entries, _ = self.draft([{
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다.',
            'confidence': 'HIGH',
            'citations': [{'index': 0, 'quote': '배포는 금요일 오후에는 하지 않는 걸로 합시다'}],
        }])

        self.assertEqual(entries[0].scope.area_key, CompanyScope.AreaKey.COMPANY)

    # INSTRUCTION이 아닌 원문은 초안 재료가 아니다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_only_instruction_documents_are_used(self):
        RawDocument.objects.update(classified_as='CONTEXT')

        entries, errors = self.draft([])

        self.assertEqual((entries, errors), ([], []))

    # 채널 범위를 바꾸면 dedupe_key가 달라져 예전 범위에 초안이 남는다.
    # 재생성 때 정리되어야 같은 규칙이 두 범위에 중복으로 보이지 않는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_scope_change_moves_draft_instead_of_duplicating(self):
        rule = {
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다.',
            'confidence': 'HIGH',
            'citations': [{'index': 0, 'quote': '배포는 금요일 오후에는 하지 않는 걸로 합시다'}],
        }
        self.draft([rule])
        self.assertEqual(HandbookEntry.objects.get().scope_id, self.project.id)

        other_scope = CompanyScope.objects.get(
            company=self.company, area_key=CompanyScope.AreaKey.PRODUCT_ENG
        )
        self.item.scope = other_scope
        self.item.save()

        self.draft([rule])

        self.assertEqual(HandbookEntry.objects.count(), 1)
        self.assertEqual(HandbookEntry.objects.get().scope_id, other_scope.id)

    # 사람이 만든 항목과 확정된 항목은 정리 대상이 아니다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_prune_spares_manual_and_confirmed_entries(self):
        manual = HandbookEntry.objects.create(
            company=self.company, scope=self.project, title='직접 등록한 규칙',
            status=HandbookEntry.Status.DRAFT, origin=HandbookEntry.Origin.DIRECT_ENTRY,
        )
        confirmed = HandbookEntry.objects.create(
            company=self.company, scope=self.project, title='확정된 규칙',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
        )

        self.draft([{
            'title': '금요일 오후 배포 금지',
            'body': '배포는 금요일 오후에 하지 않습니다.',
            'confidence': 'HIGH',
            'citations': [{'index': 0, 'quote': '배포는 금요일 오후에는 하지 않는 걸로 합시다'}],
        }])

        self.assertTrue(HandbookEntry.objects.filter(id=manual.id).exists())
        self.assertTrue(HandbookEntry.objects.filter(id=confirmed.id).exists())

    @override_settings(OPENAI_API_KEY='')
    def test_missing_api_key_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            draft_entries(self.company)


class HandbookReviewTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.scope = CompanyScope.objects.get(
            company=self.company, area_key=CompanyScope.AreaKey.PRODUCT_ENG
        )
        self.owner = User.objects.create_user(email='owner@example.com', password='pw', display_name='대표')
        Membership.objects.create(user=self.owner, company=self.company, role=Membership.Role.OWNER)
        self.member = User.objects.create_user(email='m@example.com', password='pw', display_name='팀원')
        Membership.objects.create(user=self.member, company=self.company, role=Membership.Role.MEMBER)

        self.entry = self._entry('금요일 오후 배포 금지')
        HandbookEvidence.objects.create(
            company=self.company, entry=self.entry, quote='배포는 금요일 오후에는 하지 않는 걸로 합시다',
            tag=HandbookEvidence.Tag.SLACK, source_label='#dev', speaker_name='조상원',
            permalink='https://slack/1', occurred_at=timezone.now(),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}/handbook/entries'

    def _entry(self, title, status=HandbookEntry.Status.DRAFT):
        return HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title=title,
            body_ko='본문', status=status, origin=HandbookEntry.Origin.SLACK,
        )

    # --- 근거 조회 ---

    def test_member_can_read_evidence(self):
        self.client.force_authenticate(user=self.member)
        response = self.client.get(f'{self.base}/{self.entry.id}/evidence')

        self.assertEqual(response.status_code, 200)
        item = response.data['items'][0]
        self.assertEqual(item['quote'], '배포는 금요일 오후에는 하지 않는 걸로 합시다')
        self.assertEqual(item['sourceLabel'], '#dev')
        self.assertEqual(item['speakerName'], '조상원')
        self.assertEqual(item['tag'], 'SLACK')

    def test_other_company_entry_evidence_is_404(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_owner = User.objects.create_user(email='o@example.com', password='pw', display_name='다른대표')
        Membership.objects.create(user=other_owner, company=other, role=Membership.Role.OWNER)

        self.client.force_authenticate(user=other_owner)
        response = self.client.get(
            f'/api/companies/{other.id}/handbook/entries/{self.entry.id}/evidence'
        )

        self.assertEqual(response.status_code, 404)

    # --- 단건 검토 ---

    def test_approve_confirms_entry(self):
        response = self.client.post(
            f'{self.base}/{self.entry.id}/review', {'decision': 'APPROVE'}, format='json'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['status'], 'CONFIRMED')
        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.confirmed_at)

    def test_reject_archives_entry(self):
        response = self.client.post(
            f'{self.base}/{self.entry.id}/review', {'decision': 'REJECT'}, format='json'
        )

        self.assertEqual(response.data['status'], 'ARCHIVED')
        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.confirmed_at)

    # 보류를 담을 컬럼이 없어 DRAFT 그대로 둔다.
    def test_hold_leaves_draft(self):
        response = self.client.post(
            f'{self.base}/{self.entry.id}/review', {'decision': 'HOLD'}, format='json'
        )

        self.assertEqual(response.data['status'], 'DRAFT')

    # 내용이 없는 항목을 확정하면 빈 규칙이 핸드북에 올라간다.
    def test_blank_entry_cannot_be_approved(self):
        blank = self._entry('테스트 정책', status=HandbookEntry.Status.BLANK)

        response = self.client.post(
            f'{self.base}/{blank.id}/review', {'decision': 'APPROVE'}, format='json'
        )

        self.assertEqual(response.status_code, 400)
        blank.refresh_from_db()
        self.assertEqual(blank.status, HandbookEntry.Status.BLANK)

    def test_invalid_decision_rejected(self):
        response = self.client.post(
            f'{self.base}/{self.entry.id}/review', {'decision': 'MAYBE'}, format='json'
        )

        self.assertEqual(response.status_code, 400)

    def test_member_cannot_review(self):
        self.client.force_authenticate(user=self.member)
        response = self.client.post(
            f'{self.base}/{self.entry.id}/review', {'decision': 'APPROVE'}, format='json'
        )

        self.assertEqual(response.status_code, 403)

    # --- 일괄 승인 ---

    def test_bulk_approve(self):
        second = self._entry('PR 승인 규칙')

        response = self.client.post(
            f'{self.base}/review-all',
            {'entryIds': [self.entry.id, second.id]},
            format='json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['approvedCount'], 2)
        self.assertEqual(response.data['skipped'], [])
        self.assertEqual(
            HandbookEntry.objects.filter(status=HandbookEntry.Status.CONFIRMED).count(), 2
        )

    # 승인 못 하는 항목은 전체를 실패시키지 않고 사유와 함께 건너뛴다.
    def test_bulk_approve_reports_skipped(self):
        blank = self._entry('테스트 정책', status=HandbookEntry.Status.BLANK)

        response = self.client.post(
            f'{self.base}/review-all',
            {'entryIds': [self.entry.id, blank.id, 99999]},
            format='json',
        )

        self.assertEqual(response.data['approvedCount'], 1)
        self.assertEqual(
            response.data['skipped'],
            [{'entryId': blank.id, 'reason': 'blank_entry'},
             {'entryId': 99999, 'reason': 'not_found'}],
        )

    # 다른 회사 항목이 id로 섞여 들어와도 승인되면 안 된다.
    def test_bulk_approve_ignores_other_company_entries(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_scope = CompanyScope.objects.create(
            company=other, kind=CompanyScope.Kind.PROJECT, name='남의 프로젝트'
        )
        foreign = HandbookEntry.objects.create(
            company=other, scope=other_scope, title='남의 규칙',
            status=HandbookEntry.Status.DRAFT, origin=HandbookEntry.Origin.SLACK,
        )

        response = self.client.post(
            f'{self.base}/review-all', {'entryIds': [foreign.id]}, format='json'
        )

        self.assertEqual(response.data['approvedCount'], 0)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, HandbookEntry.Status.DRAFT)


class FinalizeEntriesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.scope = CompanyScope.objects.get(
            company=self.company, area_key=CompanyScope.AreaKey.PRODUCT_ENG
        )
        self.entry = HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title='금요일 오후 배포 금지',
            body_ko='배포는 금요일 오후에 하지 않습니다.', original_lang='ko',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
        )

    def finalize(self, entries=None, translation='We do not deploy on Friday afternoons.',
                 translate_error=None, embed_error=None):
        parsed = TranslationResult(translations=[Translation(index=0, text=translation)])
        chat = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
        )
        embeddings = SimpleNamespace(data=[SimpleNamespace(embedding=[0.1] * 1536) for _ in range(4)])

        with patch('handbook.finalizing.OpenAI') as client:
            client.return_value.chat.completions.parse.return_value = chat
            client.return_value.chat.completions.parse.side_effect = translate_error
            client.return_value.embeddings.create.return_value = embeddings
            client.return_value.embeddings.create.side_effect = embed_error
            return finalize_entries(entries if entries is not None else [self.entry])

    @override_settings(OPENAI_API_KEY='test-key')
    def test_translates_and_embeds(self):
        result = self.finalize()

        self.assertEqual(result['errors'], [])
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.body_en, 'We do not deploy on Friday afternoons.')
        self.assertIsNotNone(self.entry.translated_at)
        self.assertIsNotNone(self.entry.embedded_at)
        self.assertEqual(len(self.entry.embedding_ko), 1536)
        self.assertEqual(len(self.entry.embedding_en), 1536)
        self.assertEqual(self.entry.embedding_model, settings.OPENAI_EMBEDDING_MODEL)

    # 확정되지 않은 항목은 검색 대상이 아니다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_draft_is_not_finalized(self):
        self.entry.status = HandbookEntry.Status.DRAFT
        self.entry.save()

        result = self.finalize()

        self.assertEqual(result, {'translated': 0, 'embedded': 0, 'errors': []})

    # 이미 번역된 항목은 다시 번역하지 않는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_existing_translation_is_kept(self):
        self.entry.body_en = '사람이 고친 번역'
        self.entry.save()

        self.finalize()

        self.entry.refresh_from_db()
        self.assertEqual(self.entry.body_en, '사람이 고친 번역')

    # OpenAI 장애로 확정을 되돌리지는 않는다. 나중에 다시 부르면 이어서 처리된다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_translate_failure_does_not_raise(self):
        result = self.finalize(translate_error=OpenAIError('down'))

        self.assertEqual(result['errors'][0]['step'], 'translate')
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.status, HandbookEntry.Status.CONFIRMED)
        self.assertIsNone(self.entry.translated_at)
        # 번역이 없어도 한국어 본문은 임베딩된다.
        self.assertIsNotNone(self.entry.embedded_at)

    @override_settings(OPENAI_API_KEY='')
    def test_missing_key_is_reported_not_raised(self):
        result = self.finalize()

        self.assertEqual(result['errors'], [{'code': 'openai_not_configured'}])


class ReviewFinalizeIntegrationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.scope = CompanyScope.objects.get(
            company=self.company, area_key=CompanyScope.AreaKey.PRODUCT_ENG
        )
        self.owner = User.objects.create_user(email='owner@example.com', password='pw', display_name='대표')
        Membership.objects.create(user=self.owner, company=self.company, role=Membership.Role.OWNER)
        self.entry = HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title='금요일 오후 배포 금지',
            body_ko='배포는 금요일 오후에 하지 않습니다.', original_lang='ko',
            status=HandbookEntry.Status.DRAFT, origin=HandbookEntry.Origin.SLACK,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}/handbook/entries'

    def test_approve_triggers_finalize(self):
        with patch('handbook.views.finalize_entries') as finalize:
            self.client.post(f'{self.base}/{self.entry.id}/review', {'decision': 'APPROVE'}, format='json')

        finalize.assert_called_once()
        self.assertEqual(finalize.call_args[0][0][0].id, self.entry.id)

    def test_reject_does_not_finalize(self):
        with patch('handbook.views.finalize_entries') as finalize:
            self.client.post(f'{self.base}/{self.entry.id}/review', {'decision': 'REJECT'}, format='json')

        finalize.assert_not_called()

    # 일괄 승인은 항목마다 부르지 않고 한 번에 묶어야 한다.
    def test_bulk_approve_finalizes_once(self):
        second = HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title='PR 승인 규칙',
            body_ko='승인 1명', status=HandbookEntry.Status.DRAFT,
            origin=HandbookEntry.Origin.SLACK,
        )

        with patch('handbook.views.finalize_entries') as finalize:
            self.client.post(
                f'{self.base}/review-all',
                {'entryIds': [self.entry.id, second.id]},
                format='json',
            )

        finalize.assert_called_once()
        self.assertEqual(len(finalize.call_args[0][0]), 2)

    # 본문을 고치면 기존 번역과 벡터는 낡는다. 남겨 두면 검색이 옛 문장을 물어온다.
    def test_editing_body_clears_translation_and_embedding(self):
        self.entry.status = HandbookEntry.Status.CONFIRMED
        self.entry.body_en = 'old translation'
        self.entry.translated_at = timezone.now()
        self.entry.embedding_ko = [0.1] * 1536
        self.entry.embedded_at = timezone.now()
        self.entry.save()

        self.client.patch(
            f'{self.base}/{self.entry.id}', {'originalKo': '배포는 금요일에 하지 않습니다.'}, format='json'
        )

        self.entry.refresh_from_db()
        self.assertIsNone(self.entry.body_en)
        self.assertIsNone(self.entry.translated_at)
        self.assertIsNone(self.entry.embedded_at)
        self.assertIsNone(self.entry.embedding_ko)

    def test_editing_title_only_keeps_embedding(self):
        self.entry.status = HandbookEntry.Status.CONFIRMED
        self.entry.embedded_at = timezone.now()
        self.entry.save()

        self.client.patch(f'{self.base}/{self.entry.id}', {'title': '새 제목'}, format='json')

        self.entry.refresh_from_db()
        self.assertIsNotNone(self.entry.embedded_at)


class OwnerSignupScopeTests(TestCase):
    # 대표 가입만으로 핸드북 항목을 만들 수 있는 상태가 되어야 한다.
    def test_owner_signup_seeds_scopes(self):
        serializer = OwnerSignupSerializer(
            data={
                'email': 'owner@example.com',
                'password': 'sai-test-pw-2026',
                'displayName': '조상원',
                'companyName': '에코랩',
            }
        )
        serializer.is_valid(raise_exception=True)
        membership = serializer.save()

        self.assertEqual(
            CompanyScope.objects.filter(
                company=membership.company, kind=CompanyScope.Kind.COMPANY
            ).count(),
            len(DEFAULT_COMPANY_SCOPES),
        )
