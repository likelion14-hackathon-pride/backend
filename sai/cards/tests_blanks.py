from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from qna.tests import openai_stub
from sources.models import Connection, Item, RawDocument

from .blanks import answer_blanks
from .models import Blank, InstructionCard

VECTOR = [0.1] * 1536


@override_settings(OPENAI_API_KEY='test-key')
class BlankAnsweringTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.company_scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY, name='Company',
        )
        self.eng = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='payment-api'
        )
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=connection,
            external_id='C001', label='#payment-api', scope=self.project,
        )
        document = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref='1.1',
            raw_text='결제 로그 좀 봐주세요', content_hash='1'.ljust(64, '0'),
            occurred_at=timezone.now(),
        )
        self.card = InstructionCard.objects.create(
            company=self.company, scope=self.project, document=document,
            purpose='결제 실패 로그의 원인을 파악한다', embedding=VECTOR,
        )
        HandbookEntry.objects.create(
            company=self.company, scope=self.eng, title='에러 로그 위치',
            body_ko='결제 에러는 Sentry 에 모입니다.',
            body_en='Payment errors are collected in Sentry.',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
            embedding_ko=VECTOR,
        )

    def blank(self, question='How deep should the investigation go?'):
        return Blank.objects.create(
            company=self.company, card=self.card, question_en=question
        )

    def run_answering(self, verdict='GROUNDED', answer='Start from the Sentry issue.'):
        stub = openai_stub(verdict=verdict, answer=answer, cited=(0,))
        with (
            patch('qna.answering.OpenAI', return_value=stub),
            patch('handbook.gaps.OpenAI', return_value=stub),
        ):
            answer_blanks(self.card)

    def gaps(self):
        return HandbookEntry.objects.filter(
            company=self.company, status=HandbookEntry.Status.BLANK
        )

    # --- 핸드북으로 답이 되는 경우 ---

    def test_grounded_blank_is_answered_by_sai(self):
        blank = self.blank()

        self.run_answering()

        blank.refresh_from_db()
        self.assertEqual(blank.answered_by, Blank.AnsweredBy.SAI)
        self.assertEqual(blank.sai_answer_en, 'Start from the Sentry issue.')

    # 출처 없는 답은 화면에 띄우지 않는다. 근거를 함께 남겨야 한다.
    def test_answer_keeps_its_sources(self):
        blank = self.blank()

        self.run_answering()

        blank.refresh_from_db()
        self.assertEqual(blank.answer_citations[0]['title'], '에러 로그 위치')

    def test_answered_blank_leaves_no_gap(self):
        self.blank()

        self.run_answering()

        self.assertEqual(self.gaps().count(), 0)

    # 확정 규칙이 없어도 과거 대화로 답했으면 대표를 부르지 않는다.
    def test_grounded_by_cases_counts_as_answered(self):
        blank = self.blank()

        self.run_answering(verdict='GROUNDED_BY_CASES', answer='This is what people did.')

        blank.refresh_from_db()
        self.assertEqual(blank.answered_by, Blank.AnsweredBy.SAI)

    # --- 답이 안 되는 경우 ---

    def test_unanswerable_blank_stays_open(self):
        blank = self.blank()

        self.run_answering(verdict='NO_SOURCE', answer='')

        blank.refresh_from_db()
        self.assertIsNone(blank.answered_by)
        self.assertIsNone(blank.sai_answer_en)

    def test_unanswerable_blank_becomes_a_gap(self):
        self.blank()

        self.run_answering(verdict='NO_SOURCE', answer='')

        gap = self.gaps().get()
        self.assertEqual(gap.title, 'How deep should the investigation go?')
        self.assertEqual(gap.scope, self.project)

    # 카드를 다시 만들 때마다 세면 '질문 N회 발생'이 재생성 횟수가 된다.
    # 미정 항목은 사람이 물은 것이 아니므로 자리만 잡는다.
    def test_a_blank_does_not_count_as_a_question(self):
        self.blank()

        self.run_answering(verdict='NO_SOURCE', answer='')
        self.run_answering(verdict='NO_SOURCE', answer='')

        self.assertEqual(self.gaps().count(), 1)
        self.assertEqual(self.gaps().get().ask_count, 0)

    # 판정이 필요한 질문도 사람 몫이다.
    def test_needs_decision_becomes_a_gap(self):
        self.blank()

        self.run_answering(verdict='NEEDS_DECISION', answer='')

        self.assertEqual(self.gaps().count(), 1)

    # 판정만 GROUNDED 고 본문이 비면 답한 것이 아니다.
    def test_empty_answer_is_not_an_answer(self):
        blank = self.blank()

        self.run_answering(verdict='GROUNDED', answer='')

        blank.refresh_from_db()
        self.assertIsNone(blank.answered_by)
        self.assertEqual(self.gaps().count(), 1)

    # --- 재실행 ---

    def test_already_answered_blank_is_left_alone(self):
        blank = self.blank()
        blank.answered_by = Blank.AnsweredBy.OWNER
        blank.sai_answer_en = 'The owner said so.'
        blank.save()

        self.run_answering()

        blank.refresh_from_db()
        self.assertEqual(blank.sai_answer_en, 'The owner said so.')

    # --- 실패 ---

    # 빈칸 답변은 카드 생성의 부수 작업이다. 실패해도 답을 지어내지 않고 사람에게 넘긴다.
    def test_failure_leaves_the_blank_for_a_person(self):
        blank = self.blank()

        with patch('qna.answering.OpenAI', side_effect=RuntimeError('boom')):
            answer_blanks(self.card)

        blank.refresh_from_db()
        self.assertIsNone(blank.answered_by)
