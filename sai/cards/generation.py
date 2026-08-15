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
GENERATOR_VERSION = 'card-v4'

# 지시 판정은 한 번에 여러 건을 본다. 문서마다 부르면 비용이 몇십 배가 된다.
JUDGE_BATCH_SIZE = 25

# 카드 하나에 붙일 후보 수.
MAX_RULES = 4
MAX_TONE_CASES = 5

# 같은 요청으로 볼 코사인 거리. 실측에서 같은 요청은 0.09, 다른 요청은 0.31 이상이었다.
DUPLICATE_DISTANCE = 0.15

# 이 기간이 지나 다시 올라온 같은 요청은 새 일로 본다.
DUPLICATE_WINDOW = timedelta(days=14)

MENTION = re.compile(r'<@([UWB][A-Z0-9]+)>')

JUDGE_PROMPT = """You decide whether a Slack message hands a specific piece of work to a person.

Answer `asked_of` first, then `reason`, then decide.

Both of these must be true for is_instruction = true.
  1. Someone was actually asked. The message makes a request of a person.
  2. The work will be FINISHED at some point. A task gets done and is over;
     a rule keeps applying forever.

asked_of - who is being asked.
  The name or handle when the message names one.
  'the channel' when the message asks but names nobody. Korean request forms - '~해주세요',
  '~부탁드려요', '~봐주실 수 있을까요', '~한번 봐주세요' - are requests even with no name on them.
  Most requests here carry no mention at all. Do not require one.
  Empty only when nobody is being asked at all: a fragment, a pasted command or log, a status
  report, a note to self, shared reference material.
  When asked_of is empty, is_instruction is false. Do not invent a target to fill it.

is_instruction = true
  "회의록 정리해서 노션에 올려주세요"                        (asked_of: the channel)
  "@조상원 결제 실패 로그 좀 봐주실 수 있을까요? 내일 오전까지"   (asked_of: 조상원)
  "급한 건 아닌데 시간 되실 때 배포 스크립트 한번 봐주세요"      (asked_of: the channel)
  "가능하시면 오늘 중으로 확인 부탁드려요"                     (asked_of: the channel)
  "Could you review this PR today?"                        (asked_of: the channel)

is_instruction = false
  "시크릿 키는 절대 커밋하지 마세요"           (a standing rule, never 'done')
  "일반 PR은 승인 1명으로 하죠. 확정하겠습니다"  (a policy decision)
  "staging 재기동은 앞으로 저한테 말씀해주세요"  (a standing procedure)
  "핫픽스는 #dev에 먼저 공지하고 올립니다"      (a standing procedure)
  "로컬 세팅 안 되시면 이거 실행하시면 됩니다"    (information, nobody was asked)
  "PR 리뷰 기준도 정해야 할 것 같은데 어떻게 할까요?"  (asked_of: empty - a question to the group)
  "다들 시간 되실 때 배포 프로세스 좀 정리하고 싶은데요"  (asked_of: empty - the speaker's own intention)
  "staging 서버 방금 재기동했습니다"            (a status report)
  "넵 알겠습니다"                             (acknowledgement)
  "로그 확인"                                 (asked_of: empty - a fragment)
  "```docker compose up -d --build```"        (asked_of: empty - pasted commands)
  "결제 실패 로그 3건 첨부합니다"                (asked_of: empty - sharing material)

Words like '앞으로', '항상', '~하지 마세요', '~로 하겠습니다', '~하시면 됩니다' signal a rule.
'~하고 싶은데요', '~해야 할 것 같은데요' state what the speaker themselves wants. That is not a
request, even when '다들' or '시간 되실 때' is attached to it.
Korean requests are softened - '~해주실 수 있을까요', '~부탁드려요', '시간 되실 때', '가능하시면'
are still real requests. But softening alone does not make a rule into a task.

When you cannot point to a specific piece of work that someone was asked to finish, answer false.
A wrong card puts something on a person's to-do list that was never asked of them.

Return a judgement for every index given in the input, including the ones you answer false for."""

CARD_PROMPT = """You turn a Slack message into a card that a foreign employee can act on.

The reader does not read Korean well and does not know this company's habits. Your job is to make
the request unambiguous: what is actually being asked, by when, and what the tone really means.

Every field comes in a Korean and an English version. The reader works from the English; the
Korean is there so a Korean colleague can check the card. Write the Korean first, then the English
right after it.

The English is not a word-by-word translation. Write what a competent English-speaking manager
would say to a new hire. Plain workplace English, no honorific padding. Keep channel names (#dev),
tool and product names, file names, code, URLs, numbers and times exactly as they are.

Fill the fields in the order given.

blanks - what the assignee cannot start without knowing, as short English questions.
  These come first on purpose. Finding the gap is the whole point of the card: a request that
  reads fine to a Korean colleague often leaves out something a new reader cannot guess.

  Ask when the message does not say
    - what the work actually is - '시간 될 때 봐주세요' says review, but review what?
    - which thing to act on - which environment, which channel, which document, which branch
    - what counts as finished, but only when the request is genuinely open-ended

  Do not ask about anything the message, the thread parent, the rules or the past cases already
  answer. Do not ask for a deadline that was already given. Two questions at most.
  Empty list when the request stands on its own.

  These questions are in English. Every field after this one keeps its Korean and English pair -
  write the Korean version in Korean.

purpose (Korean) / purpose_en (English) - what the requester actually wants achieved. One sentence.
  Not a restatement of the message: '결제 실패 로그 검토를 요청합니다' just repeats it, while
  '결제 실패의 원인을 찾는다' says what it is for.
  Say the work is unstated only when the message names no object at all. '봐주세요' alone names
  nothing, so write '검토 대상이 무엇인지 원문에 없습니다'. But '배포 스크립트 한번 봐주세요'
  does name the object - write the purpose normally and ask which one in blanks.

deliverable / deliverable_en - the concrete thing to hand over. Empty strings if the request does
  not name one.

deadline_text - the deadline exactly as written in the message ('내일 오전까지'). Empty if none.
deadline_text_en - the same deadline in English ('by tomorrow morning'). Empty if none.

deadline_at - that deadline as an ISO 8601 datetime in the company timezone given below, or empty
  if the message states no deadline. Use the current time given below to resolve relative words.
  When only a date is implied, use the end of the working day.

is_deadline_inferred - true when you had to guess. '내일 오전까지' is explicit. '이번 주 안에' and
  '시간 되실 때' are inferred. If deadline_at is empty, false.

urgency - pick one before you write tone_note, then keep tone_note consistent with it.
  URGENT   - the requester needs it now and other work should yield.
  SOON     - a deadline was stated. '내일 오전까지', '오늘 중으로', '이번 주 안에' are deadlines
             even when the sentence around them is soft. Normal working order is fine.
  WHENEVER - the requester said it can wait and named no deadline. '급한 건 아닌데', '천천히',
             '시간 되실 때', '여유 되실 때'. Take them at their word.
  UNCLEAR  - the message states neither a deadline nor that it can wait, and the past cases do
             not tell you.

  A stated deadline outranks soft wording. '급한 건 아닌데 이번 주 안에' is SOON, not WHENEVER.

  A message that denies urgency is not urgent. '급한 건 아닌데 시간 되실 때 봐주세요' is WHENEVER,
  never URGENT and never SOON.
  Politeness is not urgency. '~해주실 수 있을까요', '~부탁드려요' are how every request is phrased
  here. Judge urgency from the stated deadline and from the past cases, not from politeness.
  When you are between two levels, pick the lower one. Telling a new hire to drop everything for
  work that could have waited costs more than the reverse.

tone_note / tone_note_en - what this phrasing actually means in practice at this company. Use the
  past cases below as your basis. This is the most valuable field: Korean requests are softened,
  and a foreign reader will misjudge urgency. Explain what the softening words really signal here.
  In tone_note_en you may quote the Korean phrase and then explain it - "'가능하시면' reads as
  optional but here it is not" - because the reader is looking at that phrase in Slack.
  Never contradict urgency. If urgency is WHENEVER, do not write that it should be handled soon
  or quickly. If urgency is UNCLEAR, say the tone cannot be read from what is available rather
  than guessing at it.
  Empty strings when the message is already direct and needs no interpretation.

steps - concrete actions in order. Two to five. Each has text (Korean) and text_en (English).
  rule_index points at a company rule below that governs that step, or -1 when none applies.
  Do not invent rules. When you wrote a blank because the work itself is unstated, the first step
  is to ask - do not fill the gap with plausible-sounding actions.

tone_cases - which past cases you used for tone_note. case_index plus the quote copied EXACTLY
  from that case, in the original Korean. Empty list when tone_note is empty or unsupported.

Never invent facts. Everything must come from the message, the rules, or the past cases."""


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


# 배치 하나를 판정해 {인덱스: 지시 여부}를 돌려준다.
def _judge_batch(client, batch, channels, users):
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

    # 대상이 비면 지시가 아니다. 프롬프트에도 적었지만 여기서 한 번 더 막는다.
    # 조각글과 붙여넣은 명령어가 남의 할 일 목록에 올라가는 것을 프롬프트만으로 막지 못했다.
    return {
        judgement.index: judgement.is_instruction and bool(judgement.asked_of.strip())
        for judgement in completion.choices[0].message.parsed.judgements
        if 0 <= judgement.index < len(batch)
    }


# 판정 단계. 지시인 문서만 골라 낸다.
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
        lines.append(
            f'[{index}] {author} in {chunk.document.item.label}'
            f'{when:%Y-%m-%d} : {chunk.text[:200]}'
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
    text = normalize_slack_text(document.raw_text, channels, users)
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

    return _save_card(company, document, draft, rules, cases, assignee, vector)


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
        # 이미 지시가 아니라고 본 것은 다시 보지 않는다.
        # 프롬프트를 고쳐 GENERATOR_VERSION 을 올리면 전부 다시 판정한다.
        .exclude(card_version=GENERATOR_VERSION)
        .select_related('item', 'item__scope', 'author_identity')
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
            cards.append(card)

    return cards, errors
