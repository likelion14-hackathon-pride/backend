from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from handbook.services import scopes_in_view, seed_default_scopes

from .answering import AnswerResult
from .models import Thread
from .tests import openai_stub

VECTOR = [0.1] * 1536
# VECTOR 에서 거리는 있지만 상한(0.85) 안에 드는 벡터. 검색 순서를 갈라 놓을 때 쓴다.
NEARBY = [-0.1] * 300 + [0.1] * 1236


def _completion(verdict, answer='', cited=(), draft_ko=''):
    parsed = AnswerResult(
        verdict=verdict, answer=answer, cited_indexes=list(cited), draft_ko=draft_ko
    )

    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40),
    )


class ScopesInViewTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )

    # 화면의 '회사 전반'은 범위 하나가 아니라 넷이다.
    def test_no_selection_covers_all_company_scopes(self):
        ids = scopes_in_view(self.company)

        self.assertEqual(len(ids), 4)
        self.assertNotIn(self.project.id, ids)

    # 프로젝트 규칙은 회사 규칙 위에 얹히는 것이지 대체하는 것이 아니다.
    def test_project_selection_keeps_company_scopes(self):
        ids = scopes_in_view(self.company, self.project)

        self.assertEqual(len(ids), 5)
        self.assertIn(self.project.id, ids)

    def test_other_company_scopes_are_excluded(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        seed_default_scopes(other)

        self.assertEqual(len(scopes_in_view(self.company)), 4)


@override_settings(OPENAI_API_KEY='test-key')
class AskScopeTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)
        self.eng = CompanyScope.objects.get(
            company=self.company, area_key=CompanyScope.AreaKey.PRODUCT_ENG
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Lee', ui_language='en'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.url = f'/api/companies/{self.company.id}/ask'

    def rule(self, scope, title, embedding=None):
        vector = embedding or VECTOR

        return HandbookEntry.objects.create(
            company=self.company, scope=scope, title=title, body_ko='본문',
            body_en='body', status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.SLACK, embedding_ko=vector, embedding_en=vector,
        )

    def ask(self, payload=None, **stub):
        client = openai_stub(**stub)
        with (
            patch('qna.answering.OpenAI', return_value=client),
            patch('handbook.gaps.OpenAI', return_value=client),
        ):
            return self.client.post(
                self.url, payload or {'question': 'How many approvals?'}, format='json'
            )

    # 고른 범위에서 답을 못 내면 회사 전체로 넓혀 한 번 더 묻는다.
    # 두 번째 호출에 다른 응답을 주려면 parse 자체를 갈아 끼워야 한다.
    def ask_staged(self, payload, *stages):
        parse = Mock(side_effect=[_completion(**stage) for stage in stages])
        client = openai_stub()
        client.chat.completions.parse = parse
        with (
            patch('qna.answering.OpenAI', return_value=client),
            patch('handbook.gaps.OpenAI', return_value=openai_stub()),
        ):
            response = self.client.post(self.url, payload, format='json')

        return response, parse.call_count

    def titles(self, response):
        return [c['title'] for c in response.data['citations']]

    # 프로젝트를 골라도 회사 규칙을 찾아야 한다.
    def test_project_selection_still_finds_company_rules(self):
        self.rule(self.eng, 'PR 승인 규칙')

        response = self.ask({'question': 'How many approvals?', 'scopeId': self.project.id})

        self.assertEqual(self.titles(response), ['PR 승인 규칙'])

    # 아무것도 고르지 않으면 회사 전반 네 개를 모두 본다.
    def test_no_selection_covers_every_company_area(self):
        self.rule(self.eng, 'PR 승인 규칙')

        self.assertEqual(self.titles(self.ask()), ['PR 승인 규칙'])

    # 회사 전반을 골라 두고 프로젝트 이야기를 물어도 막다른 길이 되면 안 된다.
    # 회사 범위에도 규칙이 있어 검색 결과는 비지 않는다. 답을 못 냈을 때 넓혀야 한다.
    def test_widens_when_the_selected_area_cannot_answer(self):
        # 넓힌 뒤 프로젝트 규칙이 1순위가 되도록 회사 규칙을 조금 멀리 둔다.
        self.rule(self.eng, 'PR 승인 규칙', embedding=NEARBY)
        self.rule(self.project, '배포 전 QA 승인')

        response, calls = self.ask_staged(
            {'question': 'Do I need QA approval?'},
            {'verdict': 'NO_SOURCE', 'answer': '', 'cited': ()},
            {'verdict': 'GROUNDED', 'answer': 'Yes, in the veritas project.', 'cited': (0,)},
        )

        self.assertEqual(calls, 2)
        self.assertEqual(response.data['verdict'], 'GROUNDED')
        self.assertEqual(self.titles(response), ['배포 전 QA 승인'])

    # 넓혀도 더 나올 것이 없으면 두 번 묻지 않는다.
    def test_no_second_call_when_widening_adds_nothing(self):
        self.rule(self.eng, 'PR 승인 규칙')

        response, calls = self.ask_staged(
            {'question': '근거 없는 질문'},
            {'verdict': 'NO_SOURCE', 'answer': '', 'cited': ()},
        )

        self.assertEqual(calls, 1)
        self.assertEqual(response.data['verdict'], 'NO_SOURCE')

    # 다른 프로젝트 규칙은 넓히기 전까지 닿지 않는다.
    def test_other_project_rule_is_out_of_view(self):
        other = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='관리자 도구'
        )
        self.rule(other, '남의 프로젝트 규칙')

        response = self.ask({'question': 'How many approvals?', 'scopeId': self.project.id},
                            verdict='NO_SOURCE', answer='', cited=())

        self.assertEqual(response.data['citations'], [])

    def test_other_company_rule_is_never_reached(self):
        other = Company.objects.create(name='다른회사', code='TESTCODE2')
        seed_default_scopes(other)
        HandbookEntry.objects.create(
            company=other,
            scope=CompanyScope.objects.filter(company=other).first(),
            title='남의 회사 규칙', body_ko='본문', status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.SLACK, embedding_ko=VECTOR,
        )

        response = self.ask(verdict='NO_SOURCE', answer='', cited=())

        self.assertEqual(response.data['citations'], [])

    # 후속 질문에 scopeId 를 다시 안 보내면 선택이 조용히 풀렸다.
    def test_follow_up_keeps_the_thread_scope(self):
        self.rule(self.eng, 'PR 승인 규칙')
        first = self.ask({'question': '첫 질문', 'scopeId': self.project.id})
        thread_id = first.data['threadId']

        self.ask({'question': '후속 질문', 'threadId': thread_id})

        self.assertEqual(Thread.objects.get(id=thread_id).scope, self.project)
