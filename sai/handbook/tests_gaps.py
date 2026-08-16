from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from openai import OpenAIError

from companies.models import Company

from .gaps import record_gap
from .models import CompanyScope, HandbookEntry
from .retrieval import search_rules

NEAR = [0.1] * 1536
FAR = [0.1] * 1535 + [-9.0]

# scope=None 을 그대로 넘기는 경우와 인자를 생략한 경우를 구분한다.
UNSET = object()


def stub(vector):
    return SimpleNamespace(
        embeddings=SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(
                data=[SimpleNamespace(embedding=vector)]
            )
        )
    )


def broken_stub():
    def fail(**kwargs):
        raise OpenAIError('down')

    return SimpleNamespace(embeddings=SimpleNamespace(create=fail))


@override_settings(OPENAI_API_KEY='test-key')
class GapTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.company_scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY, name='Company',
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='payment-api'
        )

    def record(self, question, vector=NEAR, scope=UNSET, asked=True):
        target = self.project if scope is UNSET else scope
        with patch('handbook.gaps.OpenAI', return_value=stub(vector)):
            return record_gap(self.company, target, question, asked=asked)

    def blanks(self):
        return HandbookEntry.objects.filter(
            company=self.company, status=HandbookEntry.Status.BLANK
        )

    # --- 만들기 ---

    def test_records_the_question_as_a_blank_entry(self):
        entry = self.record('Should I write tests with the fix?')

        self.assertEqual(entry.status, HandbookEntry.Status.BLANK)
        self.assertEqual(entry.title, 'Should I write tests with the fix?')
        self.assertEqual(entry.scope, self.project)
        self.assertEqual(entry.ask_count, 1)

    # 어느 프로젝트도 아닌 질문은 회사 전반에 남는다.
    def test_falls_back_to_the_company_scope(self):
        entry = self.record('How do I return the laptop?', scope=None)

        self.assertEqual(entry.scope, self.company_scope)

    def test_blank_question_is_ignored(self):
        self.assertIsNone(self.record('   '))
        self.assertEqual(self.blanks().count(), 0)

    # --- 같은 질문 ---

    def test_the_same_question_only_raises_the_count(self):
        self.record('Should I write tests with the fix?')
        entry = self.record('Do you want a test alongside the fix?')

        self.assertEqual(self.blanks().count(), 1)
        self.assertEqual(entry.ask_count, 2)

    def test_a_different_question_is_a_separate_gap(self):
        self.record('Should I write tests with the fix?')
        self.record('How deep should the investigation go?', vector=FAR)

        self.assertEqual(self.blanks().count(), 2)

    # --- 세는 것과 안 세는 것 ---

    # 사람이 물은 것이 아니면 자리만 잡는다.
    def test_not_asked_creates_the_gap_without_counting(self):
        entry = self.record('Should I write tests with the fix?', asked=False)

        self.assertEqual(self.blanks().count(), 1)
        self.assertEqual(entry.ask_count, 0)

    def test_not_asked_does_not_raise_an_existing_count(self):
        self.record('Should I write tests with the fix?')
        entry = self.record('Do you want a test alongside the fix?', asked=False)

        self.assertEqual(entry.ask_count, 1)

    # 자리만 잡아 둔 빈칸을 사람이 실제로 물으면 그때부터 센다.
    def test_asking_later_starts_the_count(self):
        self.record('Should I write tests with the fix?', asked=False)
        entry = self.record('Do you want a test alongside the fix?')

        self.assertEqual(self.blanks().count(), 1)
        self.assertEqual(entry.ask_count, 1)

    # 프로젝트마다 따로 센다. 같은 질문이라도 다른 프로젝트에서는 다른 빈칸이다.
    def test_gaps_are_kept_per_scope(self):
        self.record('Should I write tests with the fix?')
        self.record('Should I write tests with the fix?', scope=self.company_scope)

        self.assertEqual(self.blanks().count(), 2)

    # --- 답변 근거로 쓰이지 않는다 ---

    def test_a_gap_is_never_used_as_an_answer_source(self):
        self.record('Should I write tests with the fix?')

        found = search_rules(NEAR, self.company)

        self.assertEqual(found, [])

    # --- 실패 ---

    # 빈 항목은 기록이지 답변이 아니다. OpenAI 가 죽어도 질문 처리를 막지 않는다.
    def test_embedding_failure_records_nothing(self):
        with patch('handbook.gaps.OpenAI', return_value=broken_stub()):
            self.assertIsNone(record_gap(self.company, self.project, 'anything'))

        self.assertEqual(self.blanks().count(), 0)

    @override_settings(OPENAI_API_KEY='')
    def test_missing_api_key_records_nothing(self):
        self.assertIsNone(record_gap(self.company, self.project, 'anything'))
        self.assertEqual(self.blanks().count(), 0)
