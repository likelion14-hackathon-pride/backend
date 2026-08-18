import re
import time
import logging
from dataclasses import dataclass
from typing import Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from openai import OpenAI, OpenAIError, RateLimitError
from pgvector.django import CosineDistance
from pydantic import BaseModel

from config.ai import client_options, generation_options, timed_call
from handbook.models import CompanyScope
from handbook.retrieval import search_rules
from handbook.services import scopes_in_view
from policy.models import RiskKeyword
from sources.models import Chunk

logger = logging.getLogger(__name__)

# 프롬프트를 고치면 올린다. Message.prompt_version 에 기록된다.
PROMPT_VERSION = 'ask-v5'

# 지식공간마다 검색해서 모델에 넘길 규칙 수. 너무 많으면 모델이 엉뚱한 걸 인용한다.
TOP_K = 5

# 코사인 거리 상한. 이보다 먼 것은 후보에서 뺀다.
# 실측상 맞는 답이 0.35~0.71 구간이라 여유를 두되 명백한 잡음은 자른다.
MAX_DISTANCE = 0.85

# 확정 규칙이 없을 때 참고할 과거 대화 수.
# 규칙이 아직 몇 개 없는 초기 회사에서는 이것 말고 답할 근거가 없다.
MAX_CASES = 3

# 과거 대화는 규칙보다 촘촘하게 자른다. 그냥 대화라 조금만 멀어도 엉뚱한 게 걸린다.
CASE_MAX_DISTANCE = 0.75

# 본문에 섞여 나오는 [0], [1][2] 같은 인용 표시.
CITATION_MARKER = re.compile(r'\s*\[\d+\](?:\s*\[\d+\])*')

SYSTEM_PROMPT = """You answer questions about a company's internal rules.

The person asking is an employee who may not read Korean. You are given two kinds of retrieved
material and must answer ONLY from them.

CONFIRMED RULES - the company owner reviewed and approved these. They are authoritative.
PAST CASES - raw Slack messages from this company. Nobody approved them. They show what people
             actually did before, which is often the only thing available when the handbook is
             still thin. Treat them as evidence of practice, never as a settled rule.

The selected knowledge space is a hard boundary. If the selected space does not contain the
answer, use NO_SOURCE. Do not infer from other teams, other projects, general knowledge, or what
would be reasonable at another company.

Choose exactly one verdict.

GROUNDED - the confirmed rules answer the question. Cite them.
GROUNDED_BY_CASES - no confirmed rule covers it, but several past cases show how this was handled.
                    Answer from them and say plainly that this is what people did before, not a
                    confirmed rule, so it may be worth confirming with the owner.
NO_SOURCE - nothing retrieved actually answers the question. Do NOT guess.
NEEDS_DECISION - the retrieved material contradicts itself, or the question needs a judgement
                 it does not cover.
OUT_OF_SCOPE - not a question about company rules or how to work here.

Prefer a confirmed rule over a past case whenever one applies. Use past cases only when no
confirmed rule answers the question. A single offhand message is not enough unless it is from the
owner or clearly records a decision. If the cases do not clearly show a practice, use NO_SOURCE.

Each rule carries the area it belongs to. Company-wide rules apply everywhere; a project's rules
apply on top of them. When a project rule and a company rule cover the same thing, follow the
project rule and say that the project does it differently.

Answer style
- Lead with the practical answer.
- Include the basis in plain English: which rule or past practice supports it.
- Mention an important caveat or missing piece when it changes what the employee should do.
- If the answer comes only from past cases, make that limitation explicit.

Hard requirements
- Never state a rule that is not in the retrieved list. If nothing covers it, use NO_SOURCE.
- Do not soften NO_SOURCE into a vague answer. "I don't know, let's ask the owner" is the correct
  and useful response.
- Cite by the index numbers you were given, only for the items you actually used. Rules and cases
  share one numbering. Put them in `cited_indexes` only. Never write index markers like [0] or
  footnote numbers inside `answer`.
- Write `answer` in English only, even when the question or source material is Korean. Keep it
  concise, polished, and specific enough that the employee can act on it.
- For NO_SOURCE and NEEDS_DECISION, write `draft_ko`: a short, polite Korean message the employee
  could send to the company owner to get this decided. Otherwise leave draft_ko empty.
- For OUT_OF_SCOPE, leave answer empty."""

CITATION_JUDGE_PROMPT = """You validate citations for an answer about company rules.

Keep only candidate sources that directly support at least one concrete claim in the answer.
Drop sources that are merely adjacent, broadly related, about a similar schedule/calendar topic,
or useful background but not needed to justify the answer.

Return only the indexes of supported candidates. Do not add new citations."""


# OpenAI 사용량 한도에 걸린 경우. 잠시 뒤 다시 시도하면 되는 상황이라
# 서버 장애와 구분해서 알려 준다.
class AnswerRateLimited(Exception):
    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f'rate_limited: retry after {retry_after}s')


def _retry_after(exc, default=20):
    headers = getattr(getattr(exc, 'response', None), 'headers', None) or {}
    try:
        return int(float(headers.get('retry-after', default)))
    except (TypeError, ValueError):
        return default


# 답변이 근거로 쓴 것 하나. 확정 규칙이거나 과거 대화 한 건이다.
# 둘을 같은 번호 공간에 놓아야 모델이 돌려준 인용 번호를 되짚을 수 있다.
@dataclass
class Source:
    entry: object = None
    chunk: object = None

    @property
    def distance(self):
        return (self.entry or self.chunk).distance

    def snapshot(self):
        return {
            'entryId': self.entry.id if self.entry else None,
            'chunkId': self.chunk.id if self.chunk else None,
            'score': round(1 - self.distance, 4),
        }

    # 화면에 보여 줄 출처 이름. 사례는 어느 채널의 언제 대화인지가 곧 이름이다.
    def payload(self):
        if self.entry:
            return {
                'entryId': self.entry.id,
                'title': self.entry.title,
                'scopeName': self.entry.scope.name,
                'chunkId': None,
                'permalink': None,
            }

        document = self.chunk.document
        when = f'{document.occurred_at:%Y-%m-%d}' if document.occurred_at else None

        return {
            'entryId': None,
            'title': ' '.join(filter(None, [document.item.label, when])),
            'scopeName': self.chunk.scope.name if self.chunk.scope else None,
            'chunkId': self.chunk.id,
            'permalink': document.permalink,
        }


class AnswerResult(BaseModel):
    verdict: Literal[
        'GROUNDED', 'GROUNDED_BY_CASES', 'NO_SOURCE', 'NEEDS_DECISION', 'OUT_OF_SCOPE'
    ]
    answer: str
    cited_indexes: list[int]
    draft_ko: str


class CitationJudgeResult(BaseModel):
    supported_indexes: list[int]


# 사용자가 화면에서 기다리는 요청이다.
# SDK 기본값은 429를 만나면 20초씩 두 번 자고 재시도해서 21초짜리 응답이 된다.
# 여기서는 재시도하지 않고 즉시 알려 주고, 다시 누르게 하는 편이 낫다.
# 배경 작업(분류·초안·번역)은 기본 재시도를 그대로 쓴다.
def _get_client():
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 질문에 답할 수 없습니다')

    return OpenAI(api_key=settings.OPENAI_API_KEY, **client_options(max_retries=0))


# 같은 말이 여러 번 올라온 경우 한 번만 쓴다. 같은 문장이 두 줄 뜨면 근거가 빈약해 보인다.
#
# 한국어 원문과 영어판을 모두 뒤져 청크마다 더 가까운 쪽을 쓴다. 규칙(search_rules)과 같은
# 방식이다. 한쪽만 보면 영어 질문이 한국어 벡터와 비교되어 컷오프에 걸린다.
# 실측: 영어 질문의 평균 거리 0.752 로 상한 0.75 를 넘어 8건 중 5건이 버려졌다.
def retrieve_cases(vector, company, scope_ids=None, include_unscoped=True):
    chunks = Chunk.objects.filter(company=company)
    if scope_ids is not None:
        scope_filter = Q(scope_id__in=scope_ids)
        if include_unscoped:
            scope_filter |= Q(scope__isnull=True)
        chunks = chunks.filter(scope_filter)

    best = {}
    for field in ('embedding', 'embedding_en'):
        rows = (
            chunks.filter(**{f'{field}__isnull': False})
            .annotate(distance=CosineDistance(field, vector))
            .filter(distance__lte=CASE_MAX_DISTANCE)
            .select_related('document', 'document__item', 'document__author_identity')
            .order_by('distance')[: MAX_CASES * 3]
        )
        for chunk in rows:
            if chunk.id not in best or chunk.distance < best[chunk.id].distance:
                best[chunk.id] = chunk

    seen = set()
    cases = []
    for chunk in sorted(best.values(), key=lambda chunk: chunk.distance):
        if chunk.text in seen:
            continue
        seen.add(chunk.text)
        cases.append(chunk)
        if len(cases) >= MAX_CASES:
            break

    return cases


# 답변 생성과 같은 방식으로 실패를 알린다. 감싸지 않으면 임베딩이 죽었을 때
# 호출부가 OpenAI 예외를 그대로 받아 500 이 나간다.
def embed_question(client, question):
    try:
        with timed_call(settings.OPENAI_EMBEDDING_MODEL):
            return client.embeddings.create(
                model=settings.OPENAI_EMBEDDING_MODEL, input=[question]
            ).data[0].embedding
    except RateLimitError as exc:
        raise AnswerRateLimited(_retry_after(exc)) from exc
    except (OpenAIError, ValueError) as exc:
        raise RuntimeError(f'embed_failed: {type(exc).__name__}') from exc


def _merge_rows(*groups):
    seen = set()
    rows = []
    for group in groups:
        for row in group:
            if row.id in seen:
                continue
            seen.add(row.id)
            rows.append(row)

    return rows


def retrieve(vector, company, scope=None):
    company_scope_ids = scopes_in_view(company)
    if scope is not None and scope.kind == CompanyScope.Kind.PROJECT:
        return (
            _merge_rows(
                search_rules(vector, company, [scope.id], TOP_K, MAX_DISTANCE),
                search_rules(vector, company, company_scope_ids, TOP_K, MAX_DISTANCE),
            ),
            _merge_rows(
                retrieve_cases(vector, company, [scope.id], include_unscoped=False),
                retrieve_cases(vector, company, company_scope_ids),
            ),
        )

    scope_ids = scopes_in_view(company, scope)
    return (
        search_rules(vector, company, scope_ids, TOP_K, MAX_DISTANCE),
        retrieve_cases(vector, company, scope_ids),
    )


# 공간 이름만으로는 회사 전반인지 프로젝트인지 알 수 없다. 어느 쪽인지 모르면
# '프로젝트 규칙이 회사 규칙 위에 얹힌다'는 지시를 지킬 방법이 없다.
def _render_rule(entry, index, lang):
    body = (entry.body_en if lang == 'en' else entry.body_ko) or entry.body_ko or entry.body_en
    kind = 'company-wide' if entry.scope.kind == CompanyScope.Kind.COMPANY else 'project'

    return f'[{index}] scope={entry.scope.name} ({kind}) title={entry.title}\n    {body}'


def _render_case(chunk, index):
    document = chunk.document
    author = (
        document.author_identity.external_handle if document.author_identity else 'unknown'
    )
    when = f'{document.occurred_at:%Y-%m-%d}' if document.occurred_at else 'unknown date'

    return f'[{index}] {author} in {document.item.label}, {when}\n    {chunk.text}'


def _citation_text(source, index):
    if source.entry:
        entry = source.entry
        body = entry.body_en or entry.body_ko or ''
        kind = 'company-wide' if entry.scope.kind == CompanyScope.Kind.COMPANY else 'project'
        return f'[{index}] rule scope={entry.scope.name} ({kind}) title={entry.title}\n{body}'

    chunk = source.chunk
    document = chunk.document
    return f'[{index}] case channel={document.item.label}\n{chunk.text_en or chunk.text}'


def _judge_citations(client, question, answer, cited):
    if not cited or not answer:
        return []
    if len(cited) == 1:
        return cited

    candidates = '\n\n'.join(
        _citation_text(source, index) for index, source in enumerate(cited)
    )
    try:
        with timed_call(settings.OPENAI_CLASSIFIER_MODEL):
            completion = client.chat.completions.parse(
                model=settings.OPENAI_CLASSIFIER_MODEL,
                messages=[
                    {'role': 'system', 'content': CITATION_JUDGE_PROMPT},
                    {
                        'role': 'user',
                        'content': (
                            f'Question:\n{question}\n\n'
                            f'Answer:\n{answer}\n\n'
                            f'Candidate sources:\n{candidates}'
                        ),
                    },
                ],
                response_format=CitationJudgeResult,
                **generation_options(
                    settings.OPENAI_CLASSIFIER_MODEL,
                    reasoning_effort=settings.OPENAI_CLASSIFIER_REASONING_EFFORT,
                    verbosity=settings.OPENAI_CLASSIFIER_VERBOSITY,
                ),
            )
        supported = set(completion.choices[0].message.parsed.supported_indexes)
    except (OpenAIError, ValueError, RuntimeError, AttributeError) as exc:
        logger.warning('citation judge failed: %s', type(exc).__name__)
        return cited

    return [source for index, source in enumerate(cited) if index in supported]


# 질문과 답변에 회사가 등록한 위험 키워드가 들어 있으면 안내 문구를 함께 돌려준다.
def find_risk_warnings(company, *texts):
    haystack = ' '.join(t for t in texts if t).lower()
    warnings = []

    for keyword in RiskKeyword.objects.filter(company=company):
        words = [keyword.word] + list(keyword.aliases or [])
        if any(word and word.lower() in haystack for word in words):
            warnings.append({
                'keyword': keyword.word,
                'level': keyword.level,
                'note': keyword.note,
            })

    return warnings


def _scope_context(scope):
    if scope is not None and scope.kind == CompanyScope.Kind.PROJECT:
        return (
            f"Selected knowledge space: company-wide rules plus project '{scope.name}'.\n"
            "Hard boundary: do not use rules or cases from any other project.\n"
            "Priority: project rules override company-wide rules for this project."
        )

    return (
        'Selected knowledge space: company-wide rules only.\n'
        'Hard boundary: do not use project rules or project cases.'
    )


def _ask(client, question, lang, entries, cases, scope):
    rules = '\n'.join(
        _render_rule(entry, index, lang) for index, entry in enumerate(entries)
    ) or '(no confirmed rules retrieved)'
    past = '\n'.join(
        _render_case(chunk, len(entries) + index) for index, chunk in enumerate(cases)
    ) or '(no past cases retrieved)'
    language = 'English' if lang == 'en' else 'Korean'

    try:
        with timed_call(settings.OPENAI_ANSWER_MODEL):
            return client.chat.completions.parse(
                model=settings.OPENAI_ANSWER_MODEL,
                messages=[
                    {'role': 'system', 'content': SYSTEM_PROMPT},
                    {
                        'role': 'user',
                        'content': (
                            f'Answer in: {language}\n\n'
                            f'{_scope_context(scope)}\n\n'
                            f'Question:\n{question}\n\n'
                            f'CONFIRMED RULES:\n{rules}\n\n'
                            f'PAST CASES:\n{past}'
                        ),
                    },
                ],
                response_format=AnswerResult,
                **generation_options(
                    settings.OPENAI_ANSWER_MODEL,
                    reasoning_effort=settings.OPENAI_ANSWER_REASONING_EFFORT,
                    verbosity=settings.OPENAI_ANSWER_VERBOSITY,
                ),
            )
    except RateLimitError as exc:
        raise AnswerRateLimited(_retry_after(exc)) from exc
    except (OpenAIError, ValueError) as exc:
        raise RuntimeError(f'answer_failed: {type(exc).__name__}') from exc


def _tokens(completion, field):
    return getattr(completion.usage, field) if completion.usage else None


# 질문 하나에 답한다. (AnswerResult, 인용된 근거, 검색 스냅샷, 사용량) 반환.
# 인용된 근거는 Source 목록이다. 확정 규칙일 수도 과거 대화일 수도 있다.
def answer_question(company, question, lang='en', scope=None):
    lang = 'en'
    client = _get_client()
    started = time.time()

    vector = embed_question(client, question)
    entries, cases = retrieve(vector, company, scope)
    completion = _ask(client, question, lang, entries, cases, scope)
    prompt_tokens = _tokens(completion, 'prompt_tokens')
    completion_tokens = _tokens(completion, 'completion_tokens')

    # 규칙과 사례가 번호를 나눠 쓴다. 모델이 돌려준 번호를 그대로 되짚을 수 있어야 한다.
    sources = [Source(entry=entry) for entry in entries]
    sources += [Source(chunk=chunk) for chunk in cases]

    result = completion.choices[0].message.parsed
    # 프롬프트로 막아도 가끔 [0] 같은 인용 표시가 본문에 섞여 나온다.
    result.answer = CITATION_MARKER.sub('', result.answer).strip()
    # 모델이 없는 번호를 인용하는 경우가 있어 실제 후보로만 걸러낸다.
    cited = [sources[i] for i in result.cited_indexes if 0 <= i < len(sources)]
    if result.verdict in ('NO_SOURCE', 'OUT_OF_SCOPE'):
        cited = []
    elif result.verdict == 'GROUNDED':
        cited = [source for source in cited if source.entry]
    elif result.verdict == 'GROUNDED_BY_CASES':
        cited = [source for source in cited if source.chunk]
    cited = _judge_citations(client, question, result.answer, cited)
    usage = {
        'model': settings.OPENAI_ANSWER_MODEL,
        'promptTokens': prompt_tokens,
        'completionTokens': completion_tokens,
        'latencyMs': int((time.time() - started) * 1000),
    }

    return result, cited, [source.snapshot() for source in sources], usage
