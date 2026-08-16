import re
from datetime import datetime, timedelta
from typing import Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from openai import OpenAI, OpenAIError
from pgvector.django import CosineDistance
from pydantic import BaseModel, Field

from handbook.retrieval import search_rules
from handbook.services import scopes_in_view
from sources.classifier import build_lookup
from sources.models import Chunk, Identity, RawDocument
from sources.text import normalize_document_text

from .blanks import answer_blanks
from .models import Blank, InstructionCard, Step, ToneEvidence
from .prompts import CARD_PROMPT, JUDGE_PROMPT

# 프롬프트를 고치면 올린다.
GENERATOR_VERSION = 'card-v5'

# 지시 판정은 한 번에 여러 건을 본다. 문서마다 부르면 비용이 몇십 배가 된다.
JUDGE_BATCH_SIZE = 25

# 카드 하나에 붙일 후보 수.
MAX_RULES = 4
MAX_TONE_CASES = 5
RULE_MAX_DISTANCE = 0.8

# 같은 요청으로 볼 코사인 거리. 실측에서 같은 요청은 0.09, 다른 요청은 0.31 이상이었다.
DUPLICATE_DISTANCE = 0.15

# 이 기간이 지나 다시 올라온 같은 요청은 새 일로 본다.
DUPLICATE_WINDOW = timedelta(days=14)

MENTION = re.compile(r'<@([UWB][A-Z0-9]+)>')


# 근거를 먼저 쓰게 두면 판정 품질이 올라간다. 생성 순서가 곧 사고 순서다.
# asked_of 가 맨 앞인 이유: 대상을 먼저 찾게 하면 '로그 확인' 같은 조각에서 빈칸이 나오고,
# 빈칸을 쓴 뒤에는 지시라고 답하기 어려워진다.
class Judgement(BaseModel):
    index: int
    asked_of: str = Field(description="요청 대상. 아무에게도 아니면 빈 문자열")
    reason: str = Field(description='끝나는 일인지 상시 규칙인지 한 문장으로')
    is_instruction: bool


class JudgementResult(BaseModel):
    judgements: list[Judgement]


class CardStep(BaseModel):
    text: str
    text_en: str
    rule_index: int = Field(description='근거가 되는 회사 규칙 번호. 없으면 -1')


class CardBlank(BaseModel):
    question_en: str


class ToneCase(BaseModel):
    case_index: int
    quote: str


# blanks 가 맨 앞인 이유: 빠진 것을 먼저 찾게 하면 그다음 purpose 를 얼버무리지 않는다.
# 뒤에 두었더니 '요청한 작업을 검토합니다' 같은 문장을 쓰고 미정 항목은 비워 두었다.
class CardDraft(BaseModel):
    blanks: list[CardBlank]
    purpose: str
    purpose_en: str
    deliverable: str
    deliverable_en: str
    deadline_text: str
    deadline_text_en: str
    deadline_at: str
    is_deadline_inferred: bool
    urgency: Literal['URGENT', 'SOON', 'WHENEVER', 'UNCLEAR']
    tone_note: str
    tone_note_en: str
    steps: list[CardStep]
    tone_cases: list[ToneCase]


def _get_client():
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 카드를 만들 수 없습니다')

    return OpenAI(api_key=settings.OPENAI_API_KEY)


# 멘션된 사람 중 SAI 계정이 연결된 첫 번째를 담당자로 본다.
# 멘션이 없거나 가입하지 않은 사람이면 담당자 없이 둔다. 엉뚱한 사람에게 배정하지 않는다.
def resolve_assignee(company, raw_text):
    slack_ids = MENTION.findall(raw_text)
    if not slack_ids:
        return None

    identity = (
        Identity.objects.filter(
            company=company, external_user_id__in=slack_ids, user__isnull=False
        )
        .select_related('user')
        .first()
    )

    return identity.user if identity else None


def _parse_deadline(value, company):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None

    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())

    return parsed


def _judge_batch(client, batch, channels, users):
    prompt = '\n'.join(
        f'[{index}] source={document.item.connection.kind} '
        f'type={document.external_ref.split(":", 1)[0]} '
        f'{normalize_document_text(document, channels, users)[:300]}'
        for index, document in enumerate(batch)
    )
    completion = client.chat.completions.parse(
        model=settings.OPENAI_CLASSIFIER_MODEL,
        messages=[
            {'role': 'system', 'content': JUDGE_PROMPT},
            {'role': 'user', 'content': prompt},
        ],
        response_format=JudgementResult,
        temperature=0,
    )

    # 대상이 비면 지시가 아니다. 프롬프트에도 적었지만 여기서 한 번 더 막는다.
    # 조각글과 붙여넣은 명령어가 남의 할 일 목록에 올라가는 것을 프롬프트만으로 막지 못했다.
    return {
        judgement.index: judgement.is_instruction and bool(judgement.asked_of.strip())
        for judgement in completion.choices[0].message.parsed.judgements
        if 0 <= judgement.index < len(batch)
    }


# 지시가 아닌 것에는 판정 버전을 남긴다. 남기지 않으면 다음 실행에서 같은 문서를 또 판정한다.
# 지시인 것에는 남기지 않는다. 카드 생성이 실패하면 다음 실행에서 다시 시도해야 한다.
def _judge(client, documents, channels, users):
    instructions = []

    for start in range(0, len(documents), JUDGE_BATCH_SIZE):
        batch = documents[start:start + JUDGE_BATCH_SIZE]
        decided = _judge_batch(client, batch, channels, users)

        # 응답에서 통째로 빠지는 항목이 실제로 있었다. 빠진 것만 한 번 더 묻는다.
        # 여기서 놓치면 진짜 지시가 카드가 되지 못한 채 조용히 사라진다.
        missing = [index for index in range(len(batch)) if index not in decided]
        if missing:
            retried = _judge_batch(client, [batch[index] for index in missing], channels, users)
            decided.update({
                missing[local]: value for local, value in retried.items() if local < len(missing)
            })

        # 인덱스 순으로 돈다. 중복 판정에서 먼저 온 것이 원본이 되므로 순서가 뒤집히면 안 된다.
        judged = []
        for index in range(len(batch)):
            if index not in decided:
                continue
            document = batch[index]
            if decided[index]:
                instructions.append(document)
                continue
            document.card_version = GENERATOR_VERSION
            judged.append(document)

        # 배치마다 저장한다. 뒤 배치가 실패해도 앞 배치의 판정은 남는다.
        RawDocument.objects.bulk_update(judged, ['card_version'])

    return instructions


# 프로젝트 채널의 지시에도 회사 규칙이 적용된다. 프로젝트만 뒤지면 '배포 전 공지' 같은
# 회사 규칙을 단계에 달지 못한다.
def _find_rules(client, company, text, scope):
    vector = client.embeddings.create(
        model=settings.OPENAI_EMBEDDING_MODEL, input=[text]
    ).data[0].embedding
    rules = search_rules(
        vector, company, scopes_in_view(company, scope), MAX_RULES, RULE_MAX_DISTANCE
    )

    return rules, vector


# 말투 해석의 근거가 될 과거 대화. 같은 문서는 제외한다.
# 본문이 같은 것은 한 번만 쓴다. 같은 문장이 두 줄 뜨면 근거가 빈약해 보인다.
def _find_tone_cases(company, vector, document):
    rows = (
        Chunk.objects.filter(company=company, embedding__isnull=False)
        .exclude(document=document)
        .annotate(distance=CosineDistance('embedding', vector))
        .filter(distance__lte=0.75)
        .select_related('document', 'document__item', 'document__author_identity')
        .order_by('distance')[: MAX_TONE_CASES * 3]
    )

    seen = set()
    cases = []
    for chunk in rows:
        if chunk.text in seen:
            continue
        seen.add(chunk.text)
        cases.append(chunk)
        if len(cases) >= MAX_TONE_CASES:
            break

    return cases


# 같은 요청을 슬랙에 두 번 올리면 카드도 두 장이 된다. 화면에는 같은 카드가 두 번 뜬다.
# 이미 있는 카드 중 충분히 가까운 것을 찾는다. 담당자가 다르면 다른 사람의 일이므로 별개로 둔다.
# 끝난 일과 같은 요청이 다시 오면 그것은 새 일이다.
def find_duplicate(company, vector, assignee, occurred_at):
    since = (occurred_at or timezone.now()) - DUPLICATE_WINDOW

    return (
        InstructionCard.objects.filter(
            company=company,
            assignee=assignee,
            embedding__isnull=False,
            duplicate_of__isnull=True,
            document__occurred_at__gte=since,
        )
        .exclude(status=InstructionCard.Status.DONE)
        .annotate(distance=CosineDistance('embedding', vector))
        .filter(distance__lte=DUPLICATE_DISTANCE)
        .order_by('distance')
        .first()
    )


# 중복은 내용을 새로 만들지 않는다. 원본을 그대로 복사하고 원본을 가리킨다.
# 카드를 남기지 않으면 다음 실행에서 같은 원문을 또 후보로 집어 판정 비용이 계속 든다.
def _save_duplicate(company, document, original):
    return InstructionCard.objects.create(
        company=company,
        scope=document.item.scope,
        document=document,
        assignee=original.assignee,
        duplicate_of=original,
        purpose=original.purpose,
        purpose_en=original.purpose_en,
        deliverable=original.deliverable,
        deliverable_en=original.deliverable_en,
        deadline_text=original.deadline_text,
        deadline_text_en=original.deadline_text_en,
        deadline_at=original.deadline_at,
        is_deadline_inferred=original.is_deadline_inferred,
        urgency=original.urgency,
        tone_note=original.tone_note,
        tone_note_en=original.tone_note_en,
    )


def _render_rules(rules):
    if not rules:
        return '(no rules)'

    return '\n'.join(f'[{i}] {e.title}: {e.body_ko}' for i, e in enumerate(rules))


def _render_cases(cases):
    if not cases:
        return '(no past cases)'

    lines = []
    for index, chunk in enumerate(cases):
        author = (
            chunk.document.author_identity.external_handle
            if chunk.document.author_identity else '?'
        )
        when = chunk.document.occurred_at
        occurred_at = when.strftime('%Y-%m-%d') if when else '?'
        lines.append(
            f'[{index}] {author} in {chunk.document.item.label}'
            f'{occurred_at} : {chunk.text[:200]}'
        )

    return '\n'.join(lines)


@transaction.atomic
def _save_card(company, document, draft, rules, cases, assignee, vector):
    card, _ = InstructionCard.objects.update_or_create(
        document=document,
        defaults={
            'company': company,
            'scope': document.item.scope,
            'assignee': assignee,
            'embedding': vector,
            'urgency': draft.urgency,
            'purpose': draft.purpose,
            'purpose_en': draft.purpose_en or None,
            'deliverable': draft.deliverable or None,
            'deliverable_en': draft.deliverable_en or None,
            'deadline_text': (draft.deadline_text or None) and draft.deadline_text[:60],
            'deadline_text_en': (draft.deadline_text_en or None) and draft.deadline_text_en[:60],
            'deadline_at': _parse_deadline(draft.deadline_at, company),
            'is_deadline_inferred': bool(draft.deadline_at) and draft.is_deadline_inferred,
            'tone_note': draft.tone_note or None,
            'tone_note_en': draft.tone_note_en or None,
        },
    )

    # 스텝·미정항목·말투근거는 매번 새로 쓴다. 다시 만들면 내용이 바뀌기 때문.
    card.steps.all().delete()
    card.blanks.all().delete()
    card.tone_evidences.all().delete()

    Step.objects.bulk_create([
        Step(
            company=company, card=card, ord=index, text=step.text,
            text_en=step.text_en or None,
            entry=rules[step.rule_index] if 0 <= step.rule_index < len(rules) else None,
        )
        for index, step in enumerate(draft.steps)
    ])
    Blank.objects.bulk_create([
        Blank(company=company, card=card, question_en=blank.question_en)
        for blank in draft.blanks
    ])

    # 모델이 지어낸 인용은 버린다. 근거 없는 말투 해석을 남기지 않는다.
    evidences = []
    for case in draft.tone_cases:
        if not 0 <= case.case_index < len(cases):
            continue
        chunk = cases[case.case_index]
        quote = case.quote.strip().strip('"“”\'')
        if not quote or quote not in chunk.text:
            continue
        evidences.append(ToneEvidence(
            company=company,
            card=card,
            document=chunk.document,
            quote=quote,
            source_label=chunk.document.item.label,
            permalink=chunk.document.permalink,
            occurred_at=chunk.document.occurred_at,
        ))
    ToneEvidence.objects.bulk_create(evidences)

    return card


def _build_card(client, company, document, channels, users):
    text = normalize_document_text(document, channels, users)
    rules, vector = _find_rules(client, company, text, document.item.scope)

    # 카드 생성 호출 전에 거른다. 중복 한 건마다 gpt-4o 호출이 통째로 절약된다.
    assignee = resolve_assignee(company, document.raw_text)
    original = find_duplicate(company, vector, assignee, document.occurred_at)
    if original is not None:
        return _save_duplicate(company, document, original)

    cases = _find_tone_cases(company, vector, document)

    author = document.author_identity.external_handle if document.author_identity else '?'
    now = timezone.localtime()
    parent = None
    if document.thread_ref:
        parent = RawDocument.objects.filter(
            company=company,
            item=document.item,
            external_ref=document.thread_ref,
        ).select_related('item__connection').first()

    user_content = (
        f'Company timezone: {company.timezone}\n'
        f'Current time: {now:%Y-%m-%d %H:%M} ({now:%A})\n'
        f'Working hours end: {company.working_hours_end:%H:%M}\n\n'
        f'Source: {document.item.connection.kind} / {document.item.label}\n'
        f'Content from {author}:\n{text}\n'
    )
    if parent:
        parent_text = normalize_document_text(parent, channels, users)
        user_content += f'\nThread parent:\n{parent_text[:300]}\n'
    user_content += (
        f'\nCompany rules that may apply:\n{_render_rules(rules)}\n'
        f'\nPast messages with similar phrasing (for tone_note):\n{_render_cases(cases)}'
    )

    completion = client.chat.completions.parse(
        model=settings.OPENAI_DRAFTER_MODEL,
        messages=[
            {'role': 'system', 'content': CARD_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
        response_format=CardDraft,
        temperature=0,
    )
    draft = completion.choices[0].message.parsed
    if not draft.purpose.strip():
        return None

    return _save_card(company, document, draft, rules, cases, assignee, vector)


def generate_cards(company, documents=None):
    if documents is None:
        documents = RawDocument.objects.filter(
            company=company,
            sync_state__in=[
                RawDocument.SyncState.CURRENT,
                RawDocument.SyncState.CHANGED,
            ],
        )

    documents = list(
        documents.filter(instruction_cards__isnull=True)
        # 분류기가 상시 규칙으로 본 것은 지시가 아니다. 규칙은 핸드북이 맡는다.
        # 후보를 줄여 판정 비용도 함께 아낀다.
        .exclude(classified_as=RawDocument.ClassifiedAs.INSTRUCTION)
        # 이미 지시가 아니라고 본 것은 다시 보지 않는다.
        # 프롬프트를 고쳐 GENERATOR_VERSION 을 올리면 전부 다시 판정한다.
        .exclude(card_version=GENERATOR_VERSION)
        .select_related('item__connection', 'item__scope', 'author_identity')
        # 판정 결과가 인덱스로 돌아온다. 동시각 문서가 있으면 순서를 id 로 고정해야 한다.
        .order_by('occurred_at', 'id')
    )
    if not documents:
        return [], []

    channels, users = build_lookup(company.id)
    client = _get_client()
    errors = []

    try:
        instructions = _judge(client, documents, channels, users)
    except (OpenAIError, ValueError) as exc:
        return [], [{'scope': 'card_judge', 'code': type(exc).__name__}]

    cards = []
    for document in instructions:
        try:
            card = _build_card(client, company, document, channels, users)
        except (OpenAIError, ValueError) as exc:
            errors.append({
                'scope': 'card_build',
                'documentId': document.id,
                'code': type(exc).__name__,
            })
            continue
        if card:
            # 카드를 저장한 뒤에 돈다. 트랜잭션 밖이어야 빈칸 답변이 카드 저장을 붙잡지 않는다.
            answer_blanks(card)
            cards.append(card)

    return cards, errors
