from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from policy.models import RiskKeyword
from qna.escalation import AnswerJudgement
from qna.models import Escalation, Message, Thread
from qna.services import collect_answer
from sources.models import Connection, Identity, Item, RawDocument

from .models import CompanyScope, HandbookEntry, HandbookEvidence
from .promotion import evaluate_and_promote
from .services import seed_default_scopes


class PromotionPolicyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='PROMOTE1')
        seed_default_scopes(self.company)
        self.company_scope = CompanyScope.objects.get(
            company=self.company, area_key=CompanyScope.AreaKey.COMPANY
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='payment-api'
        )
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company,
            connection=self.connection,
            external_id='C001',
            label='#project',
            scope=self.project,
            is_scope_confirmed=True,
        )
        self.identity = Identity.objects.create(
            company=self.company,
            connection=self.connection,
            external_user_id='U001',
            external_handle='팀원',
        )

    def candidate(self, *, count=3, body='코드 리뷰어는 두 명을 지정합니다.',
                  scope=None, confidence=HandbookEntry.Confidence.HIGH,
                  recent=True, item=None):
        scope = scope or self.project
        item = item or self.item
        entry = HandbookEntry.objects.create(
            company=self.company,
            scope=scope,
            title=body,
            body_ko=body,
            status=HandbookEntry.Status.DRAFT,
            origin=HandbookEntry.Origin.SLACK,
            confidence=confidence,
        )
        for index in range(count):
            occurred_at = timezone.now() - timedelta(
                days=index if recent else 45 + index
            )
            document = RawDocument.objects.create(
                company=self.company,
                item=item,
                external_ref=f'{entry.id}-{index}',
                author_identity=self.identity,
                occurred_at=occurred_at,
                raw_text=body,
                content_hash=f'{entry.id}-{index}'.ljust(64, '0'),
                classified_as=RawDocument.ClassifiedAs.INSTRUCTION,
            )
            HandbookEvidence.objects.create(
                company=self.company,
                entry=entry,
                document=document,
                quote=body,
                tag=HandbookEvidence.Tag.SLACK,
                occurred_at=occurred_at,
            )
        return entry

    def evaluate(self, entry):
        return evaluate_and_promote(
            entry,
            method=HandbookEntry.AutoPromotionMethod.REPEATED_EVIDENCE,
        )[0]

    def test_three_low_risk_project_evidences_are_auto_promoted(self):
        entry = self.candidate()

        with patch('handbook.finalizing.finalize_entries'):
            entry = self.evaluate(entry)

        self.assertEqual(entry.status, HandbookEntry.Status.CONFIRMED)
        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.AUTO_PROMOTED)
        self.assertEqual(
            entry.auto_promotion_method,
            HandbookEntry.AutoPromotionMethod.REPEATED_EVIDENCE,
        )
        self.assertEqual(entry.evidence_count, 3)
        self.assertIsNotNone(entry.auto_promoted_at)

    def test_insufficient_evidence_stays_pending_review(self):
        entry = self.evaluate(self.candidate(count=2))

        self.assertEqual(entry.status, HandbookEntry.Status.DRAFT)
        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.PENDING_REVIEW)
        self.assertIn('insufficient_evidence', entry.promotion_reason['codes'])

    def test_evidence_older_than_policy_window_stays_pending_review(self):
        entry = self.evaluate(self.candidate(recent=False))

        self.assertEqual(entry.status, HandbookEntry.Status.DRAFT)
        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.PENDING_REVIEW)
        self.assertIn('no_recent_evidence', entry.promotion_reason['codes'])

    def test_ai_inferred_candidate_requires_manual_review(self):
        entry = self.evaluate(
            self.candidate(confidence=HandbookEntry.Confidence.MEDIUM)
        )

        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertIn('ai_inference_possible', entry.promotion_reason['codes'])

    def test_configured_risk_keyword_requires_manual_review(self):
        RiskKeyword.objects.create(
            company=self.company, word='리뷰어', level=RiskKeyword.Level.DANGER
        )

        entry = self.evaluate(self.candidate())

        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertEqual(entry.detected_risk_keywords[0]['keyword'], '리뷰어')

    def test_built_in_high_risk_area_requires_manual_review(self):
        entry = self.evaluate(self.candidate(body='프로덕션 배포는 매주 월요일에 합니다.'))

        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertIn('risk_keyword_detected', entry.promotion_reason['codes'])

    def test_company_wide_rule_is_never_auto_promoted(self):
        company_item = Item.objects.create(
            company=self.company,
            connection=self.connection,
            external_id='C002',
            label='#company',
            scope=self.company_scope,
            is_scope_confirmed=True,
        )
        entry = self.evaluate(
            self.candidate(scope=self.company_scope, item=company_item)
        )

        self.assertEqual(entry.status, HandbookEntry.Status.DRAFT)
        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertIn('company_wide_rule', entry.promotion_reason['codes'])

    def test_unconfirmed_scope_requires_manual_review(self):
        self.item.is_scope_confirmed = False
        self.item.save(update_fields=['is_scope_confirmed'])

        entry = self.evaluate(self.candidate())

        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertIn('scope_unconfirmed', entry.promotion_reason['codes'])

    def test_removed_source_requires_manual_review(self):
        entry = self.candidate()
        entry.evidences.first().document.__class__.objects.filter(
            id=entry.evidences.first().document_id
        ).update(sync_state=RawDocument.SyncState.REMOVED)

        entry = self.evaluate(entry)

        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertIn('source_invalid', entry.promotion_reason['codes'])

    def test_possible_conflict_requires_manual_review(self):
        existing = HandbookEntry.objects.create(
            company=self.company,
            scope=self.project,
            title='기존 규칙',
            body_ko='코드 리뷰어는 한 명만 지정합니다.',
            status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.DIRECT_ENTRY,
        )
        entry = self.candidate()

        with patch('handbook.promotion._similarity_result', return_value={
            'kind': 'CONFLICT', 'entry_id': existing.id, 'score': 0.94,
        }):
            entry = self.evaluate(entry)

        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertTrue(entry.conflict_detected)
        self.assertEqual(entry.similar_entry_id, existing.id)

    def test_same_rule_is_marked_as_duplicate_candidate(self):
        existing = HandbookEntry.objects.create(
            company=self.company,
            scope=self.project,
            title='기존 규칙',
            body_ko='코드 리뷰어는 두 명을 지정합니다.',
            status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.DIRECT_ENTRY,
        )
        entry = self.candidate()

        entry = self.evaluate(entry)

        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertFalse(entry.conflict_detected)
        self.assertEqual(entry.similar_entry_id, existing.id)
        self.assertIn('duplicate_candidate', entry.promotion_reason['codes'])

    def test_similarity_api_failure_requires_manual_review(self):
        entry = self.candidate()

        with patch('handbook.promotion._similarity_result', side_effect=RuntimeError('down')):
            entry = self.evaluate(entry)

        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.MANUAL_REQUIRED)
        self.assertIn('similarity_check_failed', entry.promotion_reason['codes'])

    def test_auto_promotion_uses_existing_finalizer_and_is_idempotent(self):
        entry = self.candidate()

        with patch('handbook.finalizing.finalize_entries', return_value={
            'translated': 1, 'embedded': 1, 'errors': [],
        }) as finalize:
            first, _ = evaluate_and_promote(
                entry,
                method=HandbookEntry.AutoPromotionMethod.REPEATED_EVIDENCE,
            )
            second, _ = evaluate_and_promote(
                entry,
                method=HandbookEntry.AutoPromotionMethod.REPEATED_EVIDENCE,
            )

        self.assertEqual(first.id, second.id)
        finalize.assert_called_once()
        self.assertEqual(finalize.call_args.args[0][0].id, entry.id)

    def test_owner_slack_message_is_not_owner_decision_auto_promotion(self):
        owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=owner, company=self.company, role=Membership.Role.OWNER
        )
        self.identity.user = owner
        self.identity.save(update_fields=['user'])

        entry = self.evaluate(self.candidate(count=1))

        self.assertEqual(entry.status, HandbookEntry.Status.DRAFT)
        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.PENDING_REVIEW)

    def test_list_api_exposes_promotion_fields(self):
        owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=owner, company=self.company, role=Membership.Role.OWNER
        )
        entry = self.candidate(count=2)
        self.evaluate(entry)
        client = APIClient()
        client.force_authenticate(owner)

        response = client.get(f'/api/companies/{self.company.id}/handbook/entries')

        item = next(item for item in response.data['items'] if item['id'] == entry.id)
        for field_name in (
            'promotionType', 'isAutoPromoted', 'autoPromotionMethod', 'promotionReason',
            'evidenceCount', 'riskKeywords', 'hasConflict', 'hasSimilarRule',
            'autoPromotedAt', 'promotionPolicyVersion',
        ):
            self.assertIn(field_name, item)


class OwnerAnswerAutoPromotionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='OWNERPROMOTE')
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='checkout-ui'
        )
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.member = User.objects.create_user(
            email='member@example.com', password='pw', display_name='팀원'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        Item.objects.create(
            company=self.company,
            connection=connection,
            external_id='C001',
            label='#checkout',
            scope=self.project,
            is_scope_confirmed=True,
        )
        Identity.objects.create(
            company=self.company,
            connection=connection,
            external_user_id='UOWNER',
            user=self.owner,
        )
        thread = Thread.objects.create(
            company=self.company, user=self.member, scope=self.project
        )
        Message.objects.create(
            company=self.company,
            thread=thread,
            role=Message.Role.USER,
            body_en='How many reviewers?',
        )
        origin = Message.objects.create(
            company=self.company,
            thread=thread,
            role=Message.Role.AI,
            verdict=Message.Verdict.NO_SOURCE,
            body_ko='리뷰어를 몇 명 지정할까요?',
        )
        self.escalation = Escalation.objects.create(
            company=self.company,
            asked_by=self.member,
            scope=self.project,
            origin_message=origin,
            question_en='How many reviewers?',
            draft_ko='리뷰어를 몇 명 지정할까요?',
            status=Escalation.Status.SENT,
            slack_thread_ref='C001:100.1',
        )

    def judgement(self):
        return AnswerJudgement(
            reason='Owner가 반복 적용할 기준을 명확히 정했습니다.',
            is_answer=True,
            needs_review=False,
            answer_ko='코드 리뷰어는 두 명을 지정합니다.',
            answer_en='Assign two code reviewers.',
            title_ko='코드 리뷰어는 두 명을 지정합니다.',
        )

    def test_verified_owner_escalation_answer_is_auto_promoted(self):
        with patch('qna.services.fetch_reply', return_value=(
            {'user': 'UOWNER', 'ts': '100.2'}, '리뷰어는 두 명으로 하세요',
        )), patch('qna.services.judge_reply', return_value=self.judgement()), \
             patch('handbook.finalizing.finalize_entries', return_value={
                 'translated': 0, 'embedded': 1, 'errors': [],
             }) as finalize:
            escalation = collect_answer(self.escalation)

        self.assertEqual(escalation.status, Escalation.Status.APPROVED)
        entry = escalation.proposed_entry
        self.assertEqual(entry.status, HandbookEntry.Status.CONFIRMED)
        self.assertEqual(entry.promotion_type, HandbookEntry.PromotionType.AUTO_PROMOTED)
        self.assertEqual(
            entry.auto_promotion_method,
            HandbookEntry.AutoPromotionMethod.OWNER_DECISION,
        )
        self.assertEqual(entry.evidences.get().tag, HandbookEvidence.Tag.OWNER)
        finalize.assert_called_once()

    def test_verified_owner_answer_can_use_an_explicit_company_scope(self):
        company_scope = CompanyScope.objects.create(
            company=self.company,
            kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY,
            name='Company',
        )
        self.escalation.scope = company_scope
        self.escalation.save(update_fields=['scope'])

        with patch('qna.services.fetch_reply', return_value=(
            {'user': 'UOWNER', 'ts': '100.2'}, '리뷰어는 두 명으로 하세요',
        )), patch('qna.services.judge_reply', return_value=self.judgement()), \
             patch('handbook.finalizing.finalize_entries', return_value={
                 'translated': 0, 'embedded': 1, 'errors': [],
             }):
            escalation = collect_answer(self.escalation)

        self.assertEqual(escalation.status, Escalation.Status.APPROVED)
        self.assertEqual(
            escalation.proposed_entry.promotion_type,
            HandbookEntry.PromotionType.AUTO_PROMOTED,
        )

    def test_owner_answer_without_explicit_scope_is_not_auto_promoted(self):
        self.escalation.scope = None
        self.escalation.save(update_fields=['scope'])

        with patch('qna.services.fetch_reply', return_value=(
            {'user': 'UOWNER', 'ts': '100.2'}, '리뷰어는 두 명으로 하세요',
        )), patch('qna.services.judge_reply', return_value=self.judgement()), \
             patch('handbook.finalizing.finalize_entries') as finalize:
            escalation = collect_answer(self.escalation)

        self.assertEqual(escalation.status, Escalation.Status.ANSWERED)
        self.assertIsNone(escalation.proposed_entry_id)
        finalize.assert_not_called()

    def test_unverified_reply_is_not_auto_promoted(self):
        with patch('qna.services.fetch_reply', return_value=(
            {'user': 'UOTHER', 'ts': '100.2'}, '리뷰어는 두 명으로 하세요',
        )), patch('qna.services.judge_reply', return_value=self.judgement()), \
             patch('handbook.finalizing.finalize_entries') as finalize:
            escalation = collect_answer(self.escalation)

        self.assertEqual(escalation.status, Escalation.Status.ANSWERED)
        self.assertIsNone(escalation.proposed_entry_id)
        finalize.assert_not_called()


class BulkReviewExtensionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='BULKPROMOTE')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='project'
        )
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.base = f'/api/companies/{self.company.id}/handbook/entries/review-all'

    def entry(self, **extra):
        values = {
            'company': self.company,
            'scope': self.scope,
            'title': '규칙',
            'body_ko': '코드 리뷰어는 두 명을 지정합니다.',
            'status': HandbookEntry.Status.DRAFT,
            'origin': HandbookEntry.Origin.SLACK,
        }
        values.update(extra)
        return HandbookEntry.objects.create(**values)

    def test_bulk_reject_returns_per_entry_results(self):
        first = self.entry(title='첫 규칙')
        second = self.entry(title='둘째 규칙')

        response = self.client.post(self.base, {
            'entryIds': [first.id, second.id], 'decision': 'REJECT',
        }, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['processedCount'], 2)
        self.assertEqual(response.data['rejectedCount'], 2)
        self.assertEqual(response.data['approvedCount'], 0)
        self.assertEqual(
            HandbookEntry.objects.filter(status=HandbookEntry.Status.ARCHIVED).count(),
            2,
        )

    def test_manual_required_entry_is_skipped_by_bulk_review(self):
        entry = self.entry(promotion_type=HandbookEntry.PromotionType.MANUAL_REQUIRED)

        response = self.client.post(self.base, {'entryIds': [entry.id]}, format='json')

        self.assertEqual(response.data['approvedCount'], 0)
        self.assertEqual(
            response.data['skipped'],
            [{'entryId': entry.id, 'reason': 'individual_review_required'}],
        )

    def test_already_processed_entry_is_idempotently_skipped(self):
        entry = self.entry(status=HandbookEntry.Status.CONFIRMED)

        with patch('handbook.views.finalize_entries') as finalize:
            response = self.client.post(self.base, {'entryIds': [entry.id]}, format='json')

        self.assertEqual(response.data['processedCount'], 0)
        self.assertEqual(response.data['skipped'][0]['reason'], 'already_approved')
        finalize.assert_called_once_with([])
