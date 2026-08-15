import re
from datetime import datetime, time as dt_time, timedelta
from typing import Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from openai import OpenAI, OpenAIError
from pgvector.django import CosineDistance
from pydantic import BaseModel, Field

from handbook.models import HandbookEntry
from sources.classifier import build_lookup
from sources.models import Chunk, Identity, RawDocument
from sources.text import normalize_slack_text

from .models import Blank, InstructionCard, Step, ToneEvidence

# 프롬프트를 고치면 올린다.
GENERATOR_VERSION = 'card-v1'

# 지시 판정은 한 번에 여러 건을 본다. 문서마다 부르면 비용이 몇십 배가 된다.
JUDGE_BATCH_SIZE = 25

# 카드 하나에 붙일 후보 수.
MAX_RULES = 4
MAX_TONE_CASES = 5

MENTION = re.compile(r'<@([UWB][A-Z0-9]+)>')

JUDGE_PROMPT = """You decide whether a Slack message hands a specific piece of work to a person.

Write `reason` first, then decide.

The deciding test: will this piece of work be FINISHED at some point?
  A task gets done and is over.        -> is_instruction = true
  A rule keeps applying forever.       -> is_instruction = false

is_instruction = true
  "회의록 정리해서 노션에 올려주세요"                        (one document, then done)
  "@조상원 결제 실패 로그 좀 봐주실 수 있을까요? 내일 오전까지"   (one investigation, then done)
  "급한 건 아닌데 시간 되실 때 배포 스크립트 한번 봐주세요"      (one review, then done)
  "Could you review this PR today?"

is_instruction = false
  "시크릿 키는 절대 커밋하지 마세요"           (a standing rule, never 'done')
  "일반 PR은 승인 1명으로 하죠. 확정하겠습니다"  (a policy decision)
  "staging 재기동은 앞으로 저한테 말씀해주세요"  (a standing procedure)
  "핫픽스는 #dev에 먼저 공지하고 올립니다"      (a standing procedure)
  "로컬 세팅 안 되시면 이거 실행하시면 됩니다"    (information, nobody was asked)
  "PR 리뷰 기준도 정해야 할 것 같은데 어떻게 할까요?"  (a question to the group)
  "배포 프로세스 좀 정리하고 싶은데요"           (an intention, nobody was asked)
  "staging 서버 방금 재기동했습니다"            (a status report)
  "넵 알겠습니다"                             (acknowledgement)

Words like '앞으로', '항상', '~하지 마세요', '~로 하겠습니다', '~하시면 됩니다' signal a rule.
Korean requests are softened - '~해주실 수 있을까요', '~부탁드려요', '시간 되실 때', '가능하시면'
are still real requests. But softening alone does not make a rule into a task.

When you cannot point to a specific piece of work that someone will finish, answer false.
A wrong card puts something on a person's to-do list that was never asked of them."""

CARD_PROMPT = """You turn a Slack message into a card that a foreign employee can act on.

The reader does not read Korean well and does not know this company's habits. Your job is to make
the request unambiguous: what is actually being asked, by when, and what the tone really means.

Fill the fields in the order given.

purpose - what the requester actually wants achieved, in Korean. One sentence. Not a restatement
  of the message.

deliverable - the concrete thing to hand over. Empty string if the request does not name one.

deadline_text - the deadline exactly as written in the message ('내일 오전까지'). Empty if none.

deadline_at - that deadline as an ISO 8601 datetime in the company timezone given below, or empty
  if the message states no deadline. Use the current time given below to resolve relative words.
  When only a date is implied, use the end of the working day.

is_deadline_inferred - true when you had to guess. '내일 오전까지' is explicit. '이번 주 안에' and
  '시간 되실 때' are inferred. If deadline_at is empty, false.

tone_note - in Korean, what this phrasing actually means in practice at this company. Use the past
  cases below as your basis. This is the most valuable field: Korean requests are softened, and a
  foreign reader will misjudge urgency. Say plainly whether this is urgent, and what the softening
  words really signal. If the past cases do not support a reading, say the tone is unclear rather
  than guessing. Empty string when the message is already direct and needs no interpretation.

steps - concrete actions in order, in Korean. Two to five. For each, rule_index points at a company
  rule below that governs that step, or -1 when none applies. Do not invent rules.

blanks - anything the assignee cannot proceed without knowing, written as short English questions.
  Empty list when the request is complete. Do not manufacture questions.

tone_cases - which past cases you used for tone_note. case_index plus the quote copied EXACTLY
  from that case. Empty list when tone_note is empty or unsupported.

Never invent facts. Everything must come from the message, the rules, or the past cases."""


# 근거를 먼저 쓰게 두면 판정 품질이 올라간다. 생성 순서가 곧 사고 순서다.
class Judgement(BaseModel):
    index: int
    reason: str = Field(description='끝나는 일인지 상시 규칙인지 한 문장으로')
    is_instruction: bool


class JudgementResult(BaseModel):
    judgements: list[Judgement]


class CardStep(BaseModel):
    text: str
    rule_index: int = Field(description='근거가 되는 회사 규칙 번호. 없으면 -1')


class CardBlank(BaseModel):
    question_en: str


class ToneCase(BaseModel):
    case_index: int
    quote: str


class CardDraft(BaseModel):
    purpose: str
    deliverable: str
    deadline_text: str
    deadline_at: str
    is_deadline_inferred: bool
    tone_note: str
    steps: list[CardStep]
    blanks: list[CardBlank]
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


# 판정 단계. 지시인 문서만 골라 낸다.
def _judge(client, documents, channels, users):
    instructions = []

    for start in range(0, len(documents), JUDGE_BATCH_SIZE):
        batch = documents[start:start + JUDGE_BATCH_SIZE]
        prompt = '\n'.join(
            f'[{index}] {normalize_slack_text(document.raw_text, channels, users)[:300]}'
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
        for judgement in completion.choices[0].message.parsed.judgements:
            if judgement.is_instruction and 0 <= judgement.index < len(batch):
                instructions.append(batch[judgement.index])

    return instructions


# 이 지시와 관련된 확정 규칙. Step 이 근거로 삼는다.
def _find_rules(client, company, text, scope):
    vector = client.embeddings.create(
        model=settings.OPENAI_EMBEDDING_MODEL, input=[text]
    ).data[0].embedding

    entries = HandbookEntry.objects.filter(
        company=company, status=HandbookEntry.Status.CONFIRMED, embedding_ko__isnull=False
    )
    if scope is not None:
        entries = entries.filter(scope=scope)

    return list(
        entries.annotate(distance=CosineDistance('embedding_ko', vector))
        .filter(distance__lte=0.8)
        .order_by('distance')[:MAX_RULES]
    ), vector


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
        lines.append(
            f'[{index}] {author} in {chunk.document.item.label}'
            f'{when:%Y-%m-%d} : {chunk.text[:200]}'
        )

    return '\n'.join(lines)


@transaction.atomic
def _save_card(company, document, draft, rules, cases, assignee):
    card, _ = InstructionCard.objects.update_or_create(
        document=document,
        defaults={
            'company': company,
            'scope': document.item.scope,
            'assignee': assignee,
            'purpose': draft.purpose,
            'deliverable': draft.deliverable or None,
            'deadline_text': (draft.deadline_text or None) and draft.deadline_text[:60],
            'deadline_at': _parse_deadline(draft.deadline_at, company),
            'is_deadline_inferred': bool(draft.deadline_at) and draft.is_deadline_inferred,
            'tone_note': draft.tone_note or None,
        },
    )

    # 스텝·미정항목·말투근거는 매번 새로 쓴다. 다시 만들면 내용이 바뀌기 때문.
    card.steps.all().delete()
    card.blanks.all().delete()
    card.tone_evidences.all().delete()

    Step.objects.bulk_create([
        Step(
            company=company, card=card, ord=index, text=step.text,
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
    text = normalize_slack_text(document.raw_text, channels, users)
    rules, vector = _find_rules(client, company, text, document.item.scope)
    cases = _find_tone_cases(company, vector, document)

    author = document.author_identity.external_handle if document.author_identity else '?'
    now = timezone.localtime()
    parent = None
    if document.thread_ref:
        parent = RawDocument.objects.filter(
            company=company, external_ref=document.thread_ref
        ).values_list('raw_text', flat=True).first()

    user_content = (
        f'Company timezone: {company.timezone}\n'
        f'Current time: {now:%Y-%m-%d %H:%M} ({now:%A})\n'
        f'Working hours end: {company.working_hours_end:%H:%M}\n\n'
        f'Message from {author} in {document.item.label}:\n{text}\n'
    )
    if parent:
        user_content += f'\nThread parent:\n{normalize_slack_text(parent, channels, users)[:300]}\n'
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

    assignee = resolve_assignee(company, document.raw_text)

    return _save_card(company, document, draft, rules, cases, assignee)


# 아직 카드가 없는 원문에서 지시를 찾아 카드를 만든다.
def generate_cards(company, documents=None):
    if documents is None:
        documents = RawDocument.objects.filter(
            company=company, sync_state=RawDocument.SyncState.CURRENT
        )

    documents = list(
        documents.filter(instruction_cards__isnull=True)
        # 분류기가 상시 규칙으로 본 것은 지시가 아니다. 규칙은 핸드북이 맡는다.
        # 후보를 줄여 판정 비용도 함께 아낀다.
        .exclude(classified_as=RawDocument.ClassifiedAs.INSTRUCTION)
        .select_related('item', 'item__scope', 'author_identity')
        .order_by('occurred_at')
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
            cards.append(card)

    return cards, errors
