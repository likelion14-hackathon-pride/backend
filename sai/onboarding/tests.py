from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry, HandbookEvidence
from handbook.services import seed_default_scopes

from . import questions
from .models import Question
from .services import DAY0_SOURCE


class OnboardingQuestionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}/onboarding'

    def answer(self, key, value, scope=None):
        payload = {'answerKo': value}
        if scope is not None:
            payload['scopeId'] = scope.id

        return self.client.patch(f'{self.base}/questions/{key}', payload, format='json')

    def skip(self, key, scope=None):
        payload = {'status': 'SKIPPED'}
        if scope is not None:
            payload['scopeId'] = scope.id

        return self.client.patch(f'{self.base}/questions/{key}', payload, format='json')

    def entry(self, title):
        return HandbookEntry.objects.get(company=self.company, title=title)

    # --- 목록 ---

    # 시딩하지 않는다. 답하기 전에도 질문은 나와야 한다.
    def test_questions_are_listed_without_any_rows(self):
        response = self.client.get(self.base)

        self.assertEqual(response.status_code, 200)
        items = response.data['questions']
        self.assertEqual(len(items), len(questions.COMPANY_QUESTIONS))
        self.assertEqual(Question.objects.count(), 0)
        self.assertTrue(all(i['status'] == 'PENDING' for i in items))

    def test_project_scope_lists_project_questions(self):
        response = self.client.get(f'{self.base}?scopeId={self.project.id}')

        items = response.data['questions']
        self.assertEqual(len(items), len(questions.PROJECT_QUESTIONS))
        self.assertEqual(items[0]['templateKey'], 'pq1')

    def test_options_are_served(self):
        items = self.client.get(self.base).data['questions']
        merge = next(i for i in items if i['templateKey'] == 'fq16')

        self.assertEqual(len(merge['options']), 3)
        self.assertEqual(merge['title'], '머지 승인 조건')

    # 자유 입력만 받는 질문은 선택지가 비어 있다.
    def test_free_text_question_has_no_options(self):
        items = self.client.get(self.base).data['questions']
        first = next(i for i in items if i['templateKey'] == 'fq1')

        self.assertEqual(first['options'], [])
        self.assertIsNotNone(first['placeholder'])

    def test_company_scope_is_rejected_as_project(self):
        company_scope = CompanyScope.objects.filter(
            company=self.company, kind=CompanyScope.Kind.COMPANY
        ).first()

        response = self.client.get(f'{self.base}?scopeId={company_scope.id}')

        self.assertEqual(response.status_code, 400)

    # --- 답변이 규칙이 된다 ---

    # 선택지를 고르면 미리 써 둔 한국어와 영어가 그대로 규칙이 된다. AI를 부르지 않는다.
    def test_choice_becomes_a_confirmed_rule(self):
        spec = questions.find('fq16')
        response = self.answer('fq16', spec.choices[0].label)

        self.assertEqual(response.status_code, 200)
        entry = self.entry(spec.choices[0].body_ko)
        self.assertEqual(entry.status, HandbookEntry.Status.CONFIRMED)
        self.assertEqual(entry.origin, HandbookEntry.Origin.ONBOARDING)
        self.assertEqual(entry.title_en, spec.choices[0].body_en)
        self.assertEqual(entry.body_ko, spec.choices[0].body_ko)
        self.assertEqual(entry.body_en, spec.choices[0].body_en)

    # 질문마다 지정한 자리로 가야 한다. 머지 규칙이 People Group 에 들어가면 안 된다.
    def test_answer_goes_to_the_assigned_scope(self):
        self.answer('fq16', questions.find('fq16').choices[0].label)
        self.answer('fq10', questions.find('fq10').choices[0].label)

        merge = questions.find('fq16').choices[0]
        leave = questions.find('fq10').choices[0]
        self.assertEqual(self.entry(merge.body_ko).scope.area_key, 'PRODUCT_ENG')
        self.assertEqual(self.entry(leave.body_ko).scope.area_key, 'PEOPLE')

    def test_ai_question_goes_to_security(self):
        choice = questions.find('fq14').choices[1]
        self.answer('fq14', choice.label)

        self.assertEqual(self.entry(choice.body_ko).scope.area_key, 'SECURITY')

    # 직접 입력한 답은 미리 쓸 수 없다. 영어는 확정 시 번역이 채운다.
    def test_free_text_answer_has_no_english_yet(self):
        self.answer('fq1', '원격 개발팀이 사수 없이도 같은 기준으로 판단하게 만든다')

        entry = self.entry('원격 개발팀이 사수 없이도 같은 기준으로 판단하게 만든다')
        self.assertEqual(entry.body_ko, '원격 개발팀이 사수 없이도 같은 기준으로 판단하게 만든다')
        self.assertIsNone(entry.body_en)
        self.assertIsNone(entry.translated_at)

    # 선택지에 없는 문구를 보내면 직접 입력으로 본다.
    def test_edited_choice_is_treated_as_free_text(self):
        self.answer('fq16', '리드 2명 승인 후 병합합니다')

        entry = self.entry('리드 2명 승인 후 병합합니다')
        self.assertEqual(entry.body_ko, '리드 2명 승인 후 병합합니다')
        self.assertIsNone(entry.body_en)

    def test_answering_twice_overwrites_one_rule(self):
        spec = questions.find('fq16')
        self.answer('fq16', spec.choices[0].label)
        self.answer('fq16', spec.choices[1].label)

        self.assertEqual(HandbookEntry.objects.filter(company=self.company).count(), 1)
        self.assertEqual(self.entry(spec.choices[1].body_ko).body_ko, spec.choices[1].body_ko)
        self.assertEqual(Question.objects.count(), 1)

    # --- 출처 ---

    # 소스에서 뽑은 것이 아니라 대표가 직접 답한 것이다.
    # 근거를 안 남기면 팀원이 출처를 눌렀을 때 빈 화면이 뜬다.
    def test_an_answer_leaves_its_source(self):
        spec = questions.find('fq16')
        self.answer('fq16', spec.choices[0].label)

        evidence = HandbookEvidence.objects.get(entry=self.entry(spec.choices[0].body_ko))
        self.assertEqual(evidence.tag, HandbookEvidence.Tag.OWNER)
        self.assertEqual(evidence.source_label, DAY0_SOURCE)
        self.assertEqual(evidence.quote, spec.choices[0].body_ko)

    def test_answering_twice_keeps_one_source(self):
        spec = questions.find('fq16')
        self.answer('fq16', spec.choices[0].label)
        self.answer('fq16', spec.choices[1].label)

        evidence = HandbookEvidence.objects.get()
        self.assertEqual(evidence.quote, spec.choices[1].body_ko)

    # 목록에서 바로 출처가 보여야 한다.
    def test_the_entry_reports_its_source(self):
        self.answer('fq16', questions.find('fq16').choices[0].label)

        listed = self.client.get(
            f'/api/companies/{self.company.id}/handbook/entries'
        ).data['items']
        title = questions.find('fq16').choices[0].body_ko
        source = next(i['source'] for i in listed if i['title'] == title)

        self.assertEqual(source['label'], DAY0_SOURCE)
        self.assertEqual(source['tag'], 'OWNER')

    # --- 넘어가기 ---

    def test_skip_leaves_no_rule(self):
        response = self.skip('fq16')

        self.assertEqual(response.data['status'], 'SKIPPED')
        self.assertFalse(HandbookEntry.objects.filter(company=self.company).exists())

    # 답한 뒤 마음을 바꿔 넘어가면 만들어 둔 규칙도 사라져야 한다.
    def test_skip_after_answering_removes_the_rule(self):
        self.answer('fq16', questions.find('fq16').choices[0].label)
        self.skip('fq16')

        self.assertFalse(HandbookEntry.objects.filter(company=self.company).exists())
        self.assertIsNone(Question.objects.get().created_entry)

    # 만든 규칙을 핸드북에서 지우면 목록의 entryId 도 비어야 한다.
    # 남겨 두면 화면이 그 id 로 규칙을 열다 404 를 받는다.
    def test_a_deleted_rule_is_not_reported_as_the_created_entry(self):
        self.answer('fq16', questions.find('fq16').choices[0].label)
        entry = self.entry(questions.find('fq16').choices[0].body_ko)
        entry.deleted_at = timezone.now()
        entry.save(update_fields=['deleted_at'])

        items = self.client.get(self.base).data['questions']
        item = next(i for i in items if i['templateKey'] == 'fq16')

        self.assertEqual(item['status'], 'ANSWERED')
        self.assertIsNone(item['entryId'])

    # --- 프로젝트 질문 ---

    def test_project_answer_goes_to_the_project_scope(self):
        spec = questions.find('pq7')
        self.answer('pq7', spec.choices[0].label, scope=self.project)

        self.assertEqual(self.entry(spec.choices[0].body_ko).scope, self.project)

    # 같은 질문을 프로젝트마다 따로 답할 수 있어야 한다.
    def test_same_question_answered_per_project(self):
        other = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='관리자 도구'
        )
        spec = questions.find('pq7')
        self.answer('pq7', spec.choices[0].label, scope=self.project)
        self.answer('pq7', spec.choices[1].label, scope=other)

        self.assertEqual(Question.objects.count(), 2)
        self.assertEqual(HandbookEntry.objects.filter(company=self.company).count(), 2)

    # '예외 없음' 은 회사 규칙을 그대로 쓴다는 뜻이다. 규칙을 또 만들면 같은 내용이 두 벌 생긴다.
    def test_no_exception_answer_creates_no_rule(self):
        spec = questions.find('pq5')
        response = self.answer('pq5', spec.choices[0].label, scope=self.project)

        self.assertEqual(response.data['status'], 'ANSWERED')
        self.assertFalse(HandbookEntry.objects.filter(company=self.company).exists())

    def test_project_question_requires_scope(self):
        response = self.answer('pq7', questions.find('pq7').choices[0].label)

        self.assertEqual(response.status_code, 400)

    # --- 그 밖 ---

    def test_unknown_question_is_rejected(self):
        response = self.answer('nope', '아무거나')

        self.assertEqual(response.status_code, 400)

    def test_member_cannot_answer(self):
        member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Alex'
        )
        Membership.objects.create(
            user=member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client.force_authenticate(user=member)

        self.assertEqual(self.client.get(self.base).status_code, 403)


class OnboardingCompleteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.base = f'/api/companies/{self.company.id}/onboarding'

    # 질문마다 임베딩을 부르면 19번이다. 완료 시점에 한 번만 묶는다.
    def test_finalize_runs_once_at_completion(self):
        spec = questions.find('fq16')
        with patch('onboarding.views.finalize_entries') as finalize:
            self.client.patch(
                f'{self.base}/questions/fq16', {'answerKo': spec.choices[0].label},
                format='json',
            )
            self.assertFalse(finalize.called)

            response = self.client.post(f'{self.base}/complete')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(finalize.call_args.args[0]), 1)

    def test_summary_counts_confirmed_rules(self):
        self.client.patch(
            f'{self.base}/questions/fq16',
            {'answerKo': questions.find('fq16').choices[0].label}, format='json',
        )
        with patch('onboarding.views.finalize_entries'):
            response = self.client.post(f'{self.base}/complete')

        self.assertEqual(response.data['summary']['handbookEntryCount'], 1)
        self.assertEqual(response.data['onboardingStep'], 4)
