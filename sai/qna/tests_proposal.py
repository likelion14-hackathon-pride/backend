from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from sources.models import Connection, Item

from .models import Escalation


class ProposalTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.company_scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY, name='Company',
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='payment-api'
        )
        self.owner = User.objects.create_user(
            email='o@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=connection,
            external_id='C001', label='#payment', scope=self.project,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}/questions'

    def answered(self, scope=None, title='payment-api PR은 지훈님을 리뷰어로 지정합니다.'):
        return Escalation.objects.create(
            company=self.company, asked_by=self.member, scope=scope,
            question_en='Who should I assign as the reviewer?',
            draft_ko='PR 리뷰어는 누구로 지정할까요?',
            status=Escalation.Status.ANSWERED,
            answer_ko='결제 쪽은 지훈님을 리뷰어로 넣어주세요.',
            answer_en='Assign 지훈 as the reviewer on payment-api pull requests.',
            proposed_title=title,
            slack_thread_ref='C001:1700000000.000100',
            answered_at=timezone.now(),
        )

    def detail(self, escalation):
        return self.client.get(f'{self.base}/{escalation.id}').data

    def approve(self, escalation, payload=None):
        # 번역·임베딩은 별도로 검증한다. 여기서 풀어 두면 실제 OpenAI를 부른다.
        with patch('qna.services.finalize_entries'):
            return self.client.post(
                f'{self.base}/{escalation.id}/approve', payload or {}, format='json'
            )

    # --- 미리보기 ---

    def test_an_answered_question_shows_what_will_be_saved(self):
        proposal = self.detail(self.answered(scope=self.project))['proposal']

        self.assertEqual(proposal['title'], 'payment-api PR은 지훈님을 리뷰어로 지정합니다.')
        self.assertEqual(proposal['bodyKo'], '결제 쪽은 지훈님을 리뷰어로 넣어주세요.')
        self.assertEqual(proposal['scopeName'], 'payment-api')
        self.assertEqual(proposal['scopeKind'], 'PROJECT')
        self.assertEqual(proposal['sourceLabel'], '#payment')

    # 프로젝트가 없는 질문은 회사 전반으로 간다.
    def test_a_question_without_a_project_lands_company_wide(self):
        proposal = self.detail(self.answered())['proposal']

        self.assertEqual(proposal['scopeId'], self.company_scope.id)
        self.assertEqual(proposal['scopeKind'], 'COMPANY')

    def test_an_unanswered_question_has_nothing_to_propose(self):
        pending = Escalation.objects.create(
            company=self.company, asked_by=self.member,
            question_en='?', draft_ko='?', status=Escalation.Status.SENT,
        )

        self.assertIsNone(self.detail(pending)['proposal'])

    # 판정이 제목을 못 만든 옛 질문은 영어 질문을 제목으로 쓴다.
    def test_a_missing_title_falls_back_to_the_question(self):
        proposal = self.detail(self.answered(title=None))['proposal']

        self.assertEqual(proposal['title'], 'Who should I assign as the reviewer?')

    # --- 승인 ---

    def test_approving_saves_the_proposal_as_written(self):
        escalation = self.answered(scope=self.project)

        response = self.approve(escalation)

        self.assertEqual(response.status_code, 201)
        entry = HandbookEntry.objects.get()
        self.assertEqual(entry.title, 'payment-api PR은 지훈님을 리뷰어로 지정합니다.')
        self.assertEqual(entry.scope, self.project)
        self.assertEqual(entry.status, HandbookEntry.Status.CONFIRMED)

    # 승인 버튼이 곧 '핸드북에 넣겠다'는 결정이다. 확인보관함에서 또 승인하게 두면
    # 대표는 같은 결정을 두 번 한다.
    def test_approved_answer_does_not_wait_in_the_review_queue(self):
        self.approve(self.answered())

        response = self.client.get(
            f'/api/companies/{self.company.id}/handbook/entries?reviewStatus=PENDING'
        )

        self.assertEqual(response.data['items'], [])

    # 미리보기에서 고친 값으로 저장할 수 있어야 한다.
    def test_approving_takes_the_edited_title(self):
        escalation = self.answered()

        self.approve(escalation, {'title': 'PR 리뷰어는 대표가 지정합니다.'})

        self.assertEqual(HandbookEntry.objects.get().title, 'PR 리뷰어는 대표가 지정합니다.')

    def test_approving_takes_the_edited_english(self):
        escalation = self.answered()

        self.approve(escalation, {'ruleEn': 'Owner rewrote this.'})

        self.assertEqual(HandbookEntry.objects.get().body_en, 'Owner rewrote this.')

    # 회사 전반으로 갈 규칙을 프로젝트로 내려보낼 수 있어야 한다.
    def test_approving_takes_the_edited_layer(self):
        escalation = self.answered()

        self.approve(escalation, {'scopeId': self.project.id})

        self.assertEqual(HandbookEntry.objects.get().scope, self.project)

    def test_another_companys_scope_is_rejected(self):
        other = Company.objects.create(name='다른회사', code='OTHERCODE')
        stranger = CompanyScope.objects.create(
            company=other, kind=CompanyScope.Kind.PROJECT, name='x'
        )

        response = self.approve(self.answered(), {'scopeId': stranger.id})

        self.assertEqual(response.status_code, 400)
        self.assertFalse(HandbookEntry.objects.exists())

    def test_an_unanswered_question_cannot_be_approved(self):
        pending = Escalation.objects.create(
            company=self.company, asked_by=self.member,
            question_en='?', draft_ko='?', status=Escalation.Status.SENT,
        )

        self.assertEqual(self.approve(pending).status_code, 400)

    def test_member_cannot_approve(self):
        self.client.force_authenticate(user=self.member)

        self.assertEqual(self.approve(self.answered()).status_code, 403)
