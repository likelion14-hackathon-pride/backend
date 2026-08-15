from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from sources.models import Chunk, Connection, Identity, Item, RawDocument

from .generation import (
    GENERATOR_VERSION,
    CardBlank,
    CardDraft,
    CardStep,
    Judgement,
    JudgementResult,
    ToneCase,
    generate_cards,
    resolve_assignee,
)
from .models import Blank, InstructionCard, Step, ToneEvidence

VECTOR = [0.1] * 1536
PAST_CASE = '급한 건 아닌데 시간 되실 때 배포 스크립트 한번 봐주세요'


def draft(**overrides):
    base = {
        'purpose': '결제 실패 로그의 원인을 파악한다',
        'purpose_en': 'Find out why the payment failures are happening',
        'deliverable': '원인 정리 문서',
        'deliverable_en': 'A short write-up of the cause',
        'deadline_text': '내일 오전까지',
        'deadline_text_en': 'by tomorrow morning',
        'deadline_at': '2026-08-16T12:00:00',
        'is_deadline_inferred': False,
        'urgency': 'SOON',
        'tone_note': '완곡하게 말했지만 내일 오전이 실제 기한입니다.',
        'tone_note_en': 'Phrased softly, but tomorrow morning is a real deadline.',
        'steps': [CardStep(
            text='Sentry에서 결제 실패 로그 확인',
            text_en='Check the payment failure logs in Sentry',
            rule_index=0,
        )],
        'blanks': [CardBlank(question_en='Which environment should I check?')],
        'tone_cases': [ToneCase(case_index=0, quote='시간 되실 때')],
    }
    base.update(overrides)
    return CardDraft(**base)


class CardGenerationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev', scope=self.scope,
        )
        self.assignee = User.objects.create_user(
            email='sang@example.com', password='pw', display_name='조상원'
        )
        Membership.objects.create(
            user=self.assignee, company=self.company, role=Membership.Role.MEMBER
        )
        self.identity = Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U001', external_handle='조상원', user=self.assignee,
        )
        self.requester = Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U002', external_handle='홍길동',
        )
        self.rule = HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title='로그는 Sentry에서 확인',
            body_ko='에러 로그는 Sentry에 모입니다.', status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.SLACK, embedding_ko=VECTOR,
        )
        # 판정 배치는 occurred_at 순으로 들어간다. 과거 사례가 먼저(index 0), 지시가 나중(index 1).
        self.past = self._document(
            '0.9', PAST_CASE, occurred_at=timezone.now() - timedelta(days=7)
        )
        self.document = self._document(
            '1.1', '<@U001> 결제 실패 로그 좀 봐주실 수 있을까요? 내일 오전까지면 좋겠어요'
        )
        Chunk.objects.create(
            company=self.company, document=self.past, ord=0,
            text=PAST_CASE, embedding=VECTOR,
        )

    def _document(self, ref, text, occurred_at=None):
        return RawDocument.objects.create(
            company=self.company, item=self.item, external_ref=ref,
            author_identity=self.requester, raw_text=text,
            content_hash=ref.ljust(64, '0'), occurred_at=occurred_at or timezone.now(),
            permalink=f'https://slack/{ref}',
        )

    def generate(self, judgements=None, card=None):
        judged = judgements if judgements is not None else [
            Judgement(index=0, asked_of='', reason='상시 규칙입니다', is_instruction=False),
            Judgement(index=1, asked_of='조상원', reason='끝나는 일입니다', is_instruction=True),
        ]
        chat_results = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                parsed=JudgementResult(judgements=judged)))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                parsed=card if card is not None else draft()))]),
        ]
        with patch('cards.generation.OpenAI') as client:
            client.return_value.chat.completions.parse.side_effect = chat_results
            client.return_value.embeddings.create.return_value = SimpleNamespace(
                data=[SimpleNamespace(embedding=VECTOR)]
            )
            return generate_cards(self.company)

    # --- 판정 ---

    @override_settings(OPENAI_API_KEY='test-key')
    def test_creates_card_for_instruction_only(self):
        cards, errors = self.generate()

        self.assertEqual((len(cards), errors), (1, []))
        self.assertEqual(InstructionCard.objects.get().document, self.document)

    @override_settings(OPENAI_API_KEY='test-key')
    def test_no_card_when_nothing_is_an_instruction(self):
        cards, _ = self.generate(judgements=[
            Judgement(index=0, asked_of='', reason='잡담입니다', is_instruction=False),
            Judgement(index=1, asked_of='', reason='상태 공유입니다', is_instruction=False),
        ])

        self.assertEqual(cards, [])
        self.assertFalse(InstructionCard.objects.exists())

    # 상시 규칙은 핸드북이 맡는다. 카드로 만들면 할 일 목록이 규칙으로 채워진다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_rule_documents_are_not_candidates(self):
        RawDocument.objects.filter(id=self.document.id).update(
            classified_as=RawDocument.ClassifiedAs.INSTRUCTION
        )

        cards, _ = self.generate(judgements=[
            Judgement(index=0, asked_of='조상원', reason='끝나는 일입니다', is_instruction=True),
        ])

        self.assertFalse(InstructionCard.objects.filter(document=self.document).exists())

    # --- 카드 내용 ---

    @override_settings(OPENAI_API_KEY='test-key')
    def test_card_fields(self):
        self.generate()
        card = InstructionCard.objects.get()

        self.assertEqual(card.purpose, '결제 실패 로그의 원인을 파악한다')
        self.assertEqual(card.deliverable, '원인 정리 문서')
        self.assertEqual(card.deadline_text, '내일 오전까지')
        self.assertIsNotNone(card.deadline_at)
        self.assertFalse(card.is_deadline_inferred)
        self.assertEqual(card.status, InstructionCard.Status.NEW)
        self.assertEqual(card.scope, self.scope)

    # 카드를 읽는 사람은 외국인 직원이다. 한국어만 저장되면 읽을 수가 없다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_card_is_stored_in_english_too(self):
        self.generate()
        card = InstructionCard.objects.get()

        self.assertEqual(card.purpose_en, 'Find out why the payment failures are happening')
        self.assertEqual(card.deliverable_en, 'A short write-up of the cause')
        self.assertEqual(card.deadline_text_en, 'by tomorrow morning')
        self.assertEqual(
            card.tone_note_en, 'Phrased softly, but tomorrow morning is a real deadline.'
        )
        self.assertEqual(
            card.steps.get().text_en, 'Check the payment failure logs in Sentry'
        )

    # 영어가 비어 오면 한국어만 남긴다. 빈 문자열을 저장하면 화면이 빈 칸을 그린다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_missing_english_is_stored_as_null(self):
        self.generate(card=draft(
            purpose_en='', deliverable_en='', deadline_text_en='', tone_note_en='',
            steps=[CardStep(text='로그 확인', text_en='', rule_index=-1)],
        ))
        card = InstructionCard.objects.get()

        self.assertIsNone(card.purpose_en)
        self.assertIsNone(card.deliverable_en)
        self.assertIsNone(card.deadline_text_en)
        self.assertIsNone(card.tone_note_en)
        self.assertIsNone(card.steps.get().text_en)

    @override_settings(OPENAI_API_KEY='test-key')
    def test_urgency_is_stored(self):
        self.generate(card=draft(urgency='WHENEVER'))

        self.assertEqual(InstructionCard.objects.get().urgency, 'WHENEVER')

    # '로그 확인' 같은 조각글과 붙여넣은 명령어가 카드가 되고 있었다.
    # 대상이 비었는데 지시라고 답하면 코드에서 막는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_instruction_without_a_target_is_rejected(self):
        cards, _ = self.generate(judgements=[
            Judgement(index=0, asked_of='', reason='상시 규칙입니다', is_instruction=False),
            Judgement(index=1, asked_of='  ', reason='끝나는 일입니다', is_instruction=True),
        ])

        self.assertEqual(cards, [])
        self.assertFalse(InstructionCard.objects.exists())
        # 판정은 끝났으므로 다음 실행에서 다시 묻지 않는다.
        self.document.refresh_from_db()
        self.assertEqual(self.document.card_version, GENERATOR_VERSION)

    # 멘션이 없어도 요청하는 말투면 채널 전체에 대한 요청으로 본다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_request_to_the_channel_still_makes_a_card(self):
        cards, _ = self.generate(judgements=[
            Judgement(index=0, asked_of='', reason='상시 규칙입니다', is_instruction=False),
            Judgement(index=1, asked_of='the channel', reason='끝나는 일입니다', is_instruction=True),
        ])

        self.assertEqual(len(cards), 1)

    # 모델이 일부 인덱스를 통째로 빼고 답하는 일이 실제로 있었다.
    # 그대로 두면 진짜 지시가 카드가 되지 못한 채 조용히 사라진다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_missing_judgements_are_asked_again(self):
        first = JudgementResult(
            judgements=[Judgement(index=0, asked_of='', reason='상시 규칙입니다', is_instruction=False)]
        )
        # 재요청에는 빠졌던 것 하나만 넘어가므로 인덱스가 0으로 다시 매겨진다.
        retry = JudgementResult(
            judgements=[Judgement(index=0, asked_of='조상원', reason='끝나는 일입니다', is_instruction=True)]
        )
        results = [
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=first))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=retry))]),
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=draft()))]),
        ]
        with patch('cards.generation.OpenAI') as client:
            client.return_value.chat.completions.parse.side_effect = results
            client.return_value.embeddings.create.return_value = SimpleNamespace(
                data=[SimpleNamespace(embedding=VECTOR)]
            )
            cards, _ = generate_cards(self.company)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].document, self.document)

    # 지시가 아니라고 본 문서에 표시를 남기지 않으면 실행할 때마다 과거 전체를 다시 판정한다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_rejected_documents_are_not_judged_again(self):
        self.generate()
        self.past.refresh_from_db()
        self.document.refresh_from_db()

        self.assertEqual(self.past.card_version, GENERATOR_VERSION)
        # 지시인 것에는 남기지 않는다. 카드 생성이 실패하면 다시 시도해야 한다.
        self.assertIsNone(self.document.card_version)

        with patch('cards.generation.OpenAI') as client:
            generate_cards(self.company)

        self.assertFalse(client.return_value.chat.completions.parse.called)

    # 프롬프트를 고쳐 버전을 올리면 다시 판정한다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_new_generator_version_judges_again(self):
        self.generate()
        RawDocument.objects.filter(id=self.past.id).update(card_version='card-v1')

        with patch('cards.generation.OpenAI') as client:
            client.return_value.chat.completions.parse.return_value = SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=JudgementResult(
                    judgements=[Judgement(index=0, asked_of='', reason='상시 규칙입니다', is_instruction=False)]
                )))]
            )
            generate_cards(self.company)

        self.assertTrue(client.return_value.chat.completions.parse.called)

    # --- 중복 요청 ---

    # 같은 요청을 슬랙에 두 번 올리면 화면에 같은 카드가 두 장 뜬다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_repeated_request_is_linked_to_the_first(self):
        self.generate()
        original = InstructionCard.objects.get()

        again = self._document('2.1', '<@U001> 결제 실패 로그 원인 좀 봐주세요. 내일 오전까지 부탁드려요')
        # 앞선 실행에서 self.past 는 판정이 끝나 후보에서 빠진다. 남은 후보는 이것 하나다.
        self.generate(judgements=[Judgement(index=0, asked_of='조상원', reason='끝나는 일입니다', is_instruction=True)])

        duplicate = InstructionCard.objects.get(document=again)
        self.assertEqual(duplicate.duplicate_of, original)
        self.assertEqual(duplicate.purpose, original.purpose)
        self.assertEqual(duplicate.purpose_en, original.purpose_en)

    # 같은 말이라도 다른 사람에게 시켰으면 다른 일이다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_same_request_to_another_person_is_a_new_card(self):
        self.generate()
        other = User.objects.create_user(
            email='mina@example.com', password='pw', display_name='민아'
        )
        Membership.objects.create(
            user=other, company=self.company, role=Membership.Role.MEMBER
        )
        Identity.objects.create(
            company=self.company, connection=self.connection,
            external_user_id='U003', external_handle='민아', user=other,
        )

        again = self._document('2.1', '<@U003> 결제 실패 로그 좀 봐주실 수 있을까요?')
        # 앞선 실행에서 self.past 는 판정이 끝나 후보에서 빠진다. 남은 후보는 이것 하나다.
        self.generate(judgements=[Judgement(index=0, asked_of='조상원', reason='끝나는 일입니다', is_instruction=True)])

        card = InstructionCard.objects.get(document=again)
        self.assertIsNone(card.duplicate_of)
        self.assertEqual(card.assignee, other)

    # 끝난 일과 같은 요청이 다시 오면 그것은 새 일이다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_request_after_the_work_is_done_is_a_new_card(self):
        self.generate()
        InstructionCard.objects.update(status=InstructionCard.Status.DONE)

        again = self._document('2.1', '<@U001> 결제 실패 로그 다시 좀 봐주세요')
        # 앞선 실행에서 self.past 는 판정이 끝나 후보에서 빠진다. 남은 후보는 이것 하나다.
        self.generate(judgements=[Judgement(index=0, asked_of='조상원', reason='끝나는 일입니다', is_instruction=True)])

        self.assertIsNone(InstructionCard.objects.get(document=again).duplicate_of)

    # 멘션된 사람 중 SAI 계정이 이어진 사람이 담당자다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_assignee_from_mention(self):
        self.generate()

        self.assertEqual(InstructionCard.objects.get().assignee, self.assignee)

    # 슬랙에는 있지만 SAI에 가입하지 않은 사람이면 담당자를 비워 둔다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_no_assignee_when_user_not_linked(self):
        self.identity.user = None
        self.identity.save()

        self.generate()

        self.assertIsNone(InstructionCard.objects.get().assignee)

    def test_resolve_assignee_without_mention(self):
        self.assertIsNone(resolve_assignee(self.company, '회의록 정리해주세요'))

    # 기한이 없으면 추정 표시도 서지 않아야 한다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_no_deadline(self):
        self.generate(card=draft(deadline_text='', deadline_at='', is_deadline_inferred=True))
        card = InstructionCard.objects.get()

        self.assertIsNone(card.deadline_at)
        self.assertIsNone(card.deadline_text)
        self.assertFalse(card.is_deadline_inferred)

    @override_settings(OPENAI_API_KEY='test-key')
    def test_inferred_deadline_is_marked(self):
        self.generate(card=draft(
            deadline_text='이번 주 안에', deadline_at='2026-08-21T18:00:00',
            is_deadline_inferred=True,
        ))

        self.assertTrue(InstructionCard.objects.get().is_deadline_inferred)

    # 모델이 이상한 날짜를 주면 저장하지 않는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_unparseable_deadline_is_dropped(self):
        self.generate(card=draft(deadline_at='내일쯤'))

        self.assertIsNone(InstructionCard.objects.get().deadline_at)

    # --- 스텝 / 미정 항목 ---

    @override_settings(OPENAI_API_KEY='test-key')
    def test_step_links_to_handbook_rule(self):
        self.generate()
        step = Step.objects.get()

        self.assertEqual(step.text, 'Sentry에서 결제 실패 로그 확인')
        self.assertEqual(step.entry, self.rule)

    # 프로젝트 채널의 지시에도 회사 규칙이 적용된다.
    # 프로젝트 범위만 뒤지면 '배포 전 공지' 같은 회사 규칙을 단계에 달지 못한다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_company_rule_reaches_a_project_card(self):
        company_scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.rule.delete()
        company_rule = HandbookEntry.objects.create(
            company=self.company, scope=company_scope, title='배포 전 공지',
            body_ko='배포 전에 #dev 에 공지합니다.', status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.SLACK, embedding_ko=VECTOR,
        )

        self.generate()

        self.assertEqual(Step.objects.first().entry, company_rule)

    @override_settings(OPENAI_API_KEY='test-key')
    def test_step_without_rule(self):
        self.generate(card=draft(steps=[CardStep(text='로그 확인', text_en='Check the logs', rule_index=-1)]))

        self.assertIsNone(Step.objects.get().entry)

    # 없는 번호를 가리키면 규칙을 붙이지 않는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_out_of_range_rule_index_is_dropped(self):
        self.generate(card=draft(steps=[CardStep(text='로그 확인', text_en='Check the logs', rule_index=99)]))

        self.assertIsNone(Step.objects.get().entry)

    @override_settings(OPENAI_API_KEY='test-key')
    def test_blanks_saved(self):
        self.generate()

        self.assertEqual(Blank.objects.get().question_en, 'Which environment should I check?')

    # --- 말투 근거 ---

    @override_settings(OPENAI_API_KEY='test-key')
    def test_tone_evidence_from_past_case(self):
        self.generate()
        evidence = ToneEvidence.objects.get()

        self.assertEqual(evidence.quote, '시간 되실 때')
        self.assertEqual(evidence.document, self.past)
        self.assertEqual(evidence.source_label, '#dev')

    # 모델이 지어낸 인용은 버린다. 근거 없는 말투 해석을 남기지 않는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_fabricated_tone_quote_is_dropped(self):
        self.generate(card=draft(tone_cases=[ToneCase(case_index=0, quote='원문에 없는 말')]))

        self.assertFalse(ToneEvidence.objects.exists())

    @override_settings(OPENAI_API_KEY='test-key')
    def test_out_of_range_tone_case_is_dropped(self):
        self.generate(card=draft(tone_cases=[ToneCase(case_index=99, quote='시간 되실 때')]))

        self.assertFalse(ToneEvidence.objects.exists())

    # --- 재실행 ---

    # 이미 카드가 있는 원문은 다시 판정하지 않는다.
    @override_settings(OPENAI_API_KEY='test-key')
    def test_documents_with_cards_are_skipped(self):
        self.generate()

        cards, _ = self.generate(
            judgements=[Judgement(index=0, asked_of='', reason='이미 처리됨', is_instruction=False)]
        )

        self.assertEqual(cards, [])
        self.assertEqual(InstructionCard.objects.count(), 1)


class CardApiTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='결제 시스템'
        )
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=connection, external_id='C001', label='#dev'
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Alex'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.other = User.objects.create_user(
            email='o@example.com', password='pw', display_name='Other'
        )
        Membership.objects.create(
            user=self.other, company=self.company, role=Membership.Role.MEMBER
        )
        self.document = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref='1.1',
            raw_text='결제 로그 봐주세요', content_hash='a' * 64, occurred_at=timezone.now(),
        )
        self.card = InstructionCard.objects.create(
            company=self.company, scope=self.scope, document=self.document,
            assignee=self.member, purpose='결제 실패 로그 원인 파악',
            purpose_en='Find the cause of the payment failures',
            tone_note='내일 오전이 실제 기한입니다.',
            tone_note_en='Tomorrow morning is the real deadline.',
        )
        Step.objects.create(
            company=self.company, card=self.card, ord=0,
            text='Sentry 확인', text_en='Check Sentry',
        )
        Blank.objects.create(
            company=self.company, card=self.card, question_en='Which environment?'
        )
        ToneEvidence.objects.create(
            company=self.company, card=self.card, document=self.document,
            quote='시간 되실 때', source_label='#dev',
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.base = f'/api/companies/{self.company.id}/cards'

    def test_list(self):
        response = self.client.get(self.base)

        self.assertEqual(response.status_code, 200)
        item = response.data['items'][0]
        self.assertEqual(item['purpose'], '결제 실패 로그 원인 파악')
        self.assertEqual(item['assigneeName'], 'Alex')
        self.assertEqual(item['sourceLabel'], '#dev')
        self.assertEqual(item['blankCount'], 1)

    # 화면이 영어를 보여주려면 목록과 상세 모두에 실려야 한다.
    def test_english_is_served(self):
        item = self.client.get(self.base).data['items'][0]

        self.assertEqual(item['purposeEn'], 'Find the cause of the payment failures')
        self.assertEqual(item['toneNoteEn'], 'Tomorrow morning is the real deadline.')

        detail = self.client.get(f'{self.base}/{self.card.id}').data

        self.assertEqual(detail['purposeEn'], 'Find the cause of the payment failures')
        self.assertEqual(detail['steps'][0]['textEn'], 'Check Sentry')

    # 같은 요청이 세 번 올라왔어도 목록에는 한 장만 나와야 한다.
    def test_duplicates_are_folded_into_one_row(self):
        for ref in ('1.2', '1.3'):
            InstructionCard.objects.create(
                company=self.company, document=RawDocument.objects.create(
                    company=self.company, item=self.item, external_ref=ref,
                    raw_text='결제 로그 다시 봐주세요', content_hash=ref.ljust(64, '0'),
                    occurred_at=timezone.now(), permalink=f'https://slack/{ref}',
                ),
                assignee=self.member, purpose='결제 실패 로그 원인 파악',
                duplicate_of=self.card,
            )

        items = self.client.get(self.base).data['items']

        self.assertEqual([i['id'] for i in items], [self.card.id])
        self.assertEqual(items[0]['duplicateCount'], 3)

        detail = self.client.get(f'{self.base}/{self.card.id}').data
        self.assertEqual(
            [s['permalink'] for s in detail['duplicateSources']],
            ['https://slack/1.2', 'https://slack/1.3'],
        )

    def test_mine_filter(self):
        other_card = InstructionCard.objects.create(
            company=self.company, document=RawDocument.objects.create(
                company=self.company, item=self.item, external_ref='1.2',
                raw_text='다른 지시', content_hash='b' * 64, occurred_at=timezone.now(),
            ),
            assignee=self.other, purpose='남의 일',
        )

        response = self.client.get(f'{self.base}?mine=true')

        ids = [c['id'] for c in response.data['items']]
        self.assertIn(self.card.id, ids)
        self.assertNotIn(other_card.id, ids)

    def test_status_filter(self):
        response = self.client.get(f'{self.base}?status=DONE')

        self.assertEqual(response.data['items'], [])

    def test_invalid_status(self):
        self.assertEqual(self.client.get(f'{self.base}?status=NOPE').status_code, 400)

    def test_detail_includes_steps_blanks_and_tone(self):
        response = self.client.get(f'{self.base}/{self.card.id}')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['steps'][0]['text'], 'Sentry 확인')
        self.assertEqual(response.data['blanks'][0]['questionEn'], 'Which environment?')
        self.assertEqual(response.data['toneEvidences'][0]['quote'], '시간 되실 때')
        self.assertEqual(response.data['toneNote'], '내일 오전이 실제 기한입니다.')
        self.assertEqual(response.data['originalText'], '결제 로그 봐주세요')

    def test_status_update(self):
        response = self.client.patch(
            f'{self.base}/{self.card.id}', {'status': 'DONE'}, format='json'
        )

        self.assertEqual(response.data['status'], 'DONE')

    def test_outsider_cannot_read(self):
        outsider = User.objects.create_user(email='x@example.com', password='pw', display_name='X')
        self.client.force_authenticate(user=outsider)

        self.assertEqual(self.client.get(self.base).status_code, 403)

    def test_other_company_card_is_404(self):
        other_company = Company.objects.create(name='다른회사', code='TESTCODE2')
        other_owner = User.objects.create_user(
            email='oo@example.com', password='pw', display_name='OO'
        )
        Membership.objects.create(
            user=other_owner, company=other_company, role=Membership.Role.OWNER
        )

        self.client.force_authenticate(user=other_owner)
        response = self.client.get(
            f'/api/companies/{other_company.id}/cards/{self.card.id}'
        )

        self.assertEqual(response.status_code, 404)
