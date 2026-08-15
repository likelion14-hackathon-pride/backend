import re
import time
from typing import Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from openai import OpenAI, OpenAIError, RateLimitError
from pgvector.django import CosineDistance
from pydantic import BaseModel

from handbook.models import HandbookEntry
from policy.models import RiskKeyword

# 프롬프트를 고치면 올린다. Message.prompt_version 에 기록된다.
PROMPT_VERSION = 'ask-v1'

# 검색해서 모델에 넘길 규칙 수. 너무 많으면 모델이 엉뚱한 걸 인용한다.
TOP_K = 5

# 코사인 거리 상한. 이보다 먼 것은 후보에서 뺀다.
# 실측상 맞는 답이 0.35~0.71 구간이라 여유를 두되 명백한 잡음은 자른다.
MAX_DISTANCE = 0.85

# 본문에 섞여 나오는 [0], [1][2] 같은 인용 표시.
CITATION_MARKER = re.compile(r'\s*\[\d+\](?:\s*\[\d+\])*')

SYSTEM_PROMPT = """You answer questions about a company's internal rules.

The person asking is an employee who may not read Korean. You are given the company's confirmed
handbook rules that were retrieved for this question. Answer ONLY from those rules.

Choose exactly one verdict.

GROUNDED - the retrieved rules answer the question. Cite them.
GROUNDED_BY_CASES - only past examples were retrieved, no confirmed rule. Say so in the answer.
NO_SOURCE - nothing retrieved actually answers the question. Do NOT guess.
NEEDS_DECISION - the retrieved rules contradict each other, or the question needs a judgement
                 the rules do not cover.
OUT_OF_SCOPE - not a question about company rules or how to work here.

Hard requirements
- Never state a rule that is not in the retrieved list. If the rules do not cover it, use NO_SOURCE.
- Do not soften NO_SOURCE into a vague answer. "I don't know, let's ask the owner" is the correct
  and useful response.
- Cite by the index numbers you were given, only for rules you actually used. Put them in
  `cited_indexes` only. Never write index markers like [0] or footnote numbers inside `answer`.
- Write `answer` in the language named in the request. Keep it short: two or three sentences.
- For NO_SOURCE and NEEDS_DECISION, write `draft_ko`: a short, polite Korean message the employee
  could send to the company owner to get this decided. Otherwise leave draft_ko empty.
- For OUT_OF_SCOPE, leave answer empty."""


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


class AnswerResult(BaseModel):
    verdict: Literal[
        'GROUNDED', 'GROUNDED_BY_CASES', 'NO_SOURCE', 'NEEDS_DECISION', 'OUT_OF_SCOPE'
    ]
    answer: str
    cited_indexes: list[int]
    draft_ko: str


# 사용자가 화면에서 기다리는 요청이다.
# SDK 기본값은 429를 만나면 20초씩 두 번 자고 재시도해서 21초짜리 응답이 된다.
# 여기서는 재시도하지 않고 즉시 알려 주고, 다시 누르게 하는 편이 낫다.
# 배경 작업(분류·초안·번역)은 기본 재시도를 그대로 쓴다.
def _get_client():
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 질문에 답할 수 없습니다')

    return OpenAI(api_key=settings.OPENAI_API_KEY, max_retries=0, timeout=30)


# 확정된 규칙 중 질문과 가까운 것을 찾는다.
# 한국어/영어 임베딩을 모두 뒤져 항목별로 더 가까운 쪽을 쓴다.
# 영어 질문이 한국어로만 쓰인 규칙을 찾을 수 있어야 하기 때문.
def retrieve(client, company, question, scope=None):
    vector = client.embeddings.create(
        model=settings.OPENAI_EMBEDDING_MODEL, input=[question]
    ).data[0].embedding

    entries = HandbookEntry.objects.filter(
        company=company, status=HandbookEntry.Status.CONFIRMED
    ).select_related('scope')
    if scope is not None:
        entries = entries.filter(scope=scope)

    best = {}
    for field in ('embedding_ko', 'embedding_en'):
        rows = (
            entries.filter(**{f'{field}__isnull': False})
            .annotate(distance=CosineDistance(field, vector))
            .filter(distance__lte=MAX_DISTANCE)
            .order_by('distance')[:TOP_K]
        )
        for entry in rows:
            if entry.id not in best or entry.distance < best[entry.id].distance:
                best[entry.id] = entry

    return sorted(best.values(), key=lambda entry: entry.distance)[:TOP_K]


def _render(entry, index, lang):
    body = (entry.body_en if lang == 'en' else entry.body_ko) or entry.body_ko or entry.body_en

    return f'[{index}] scope={entry.scope.name} title={entry.title}\n    {body}'


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


# 질문 하나에 답한다. (AnswerResult, 인용된 항목, 검색 스냅샷, 사용량) 반환.
def answer_question(company, question, lang='en', scope=None):
    client = _get_client()
    started = time.time()

    entries = retrieve(client, company, question, scope)
    retrieval = [
        {'entryId': entry.id, 'score': round(1 - entry.distance, 4)} for entry in entries
    ]

    rules = '\n'.join(_render(entry, index, lang) for index, entry in enumerate(entries))
    if not rules:
        rules = '(no rules retrieved)'

    language = 'English' if lang == 'en' else 'Korean'
    try:
        completion = client.chat.completions.parse(
            model=settings.OPENAI_ANSWER_MODEL,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {
                    'role': 'user',
                    'content': (
                        f'Answer in: {language}\n\n'
                        f'Question:\n{question}\n\n'
                        f'Retrieved company rules:\n{rules}'
                    ),
                },
            ],
            response_format=AnswerResult,
            temperature=0,
        )
    except RateLimitError as exc:
        raise AnswerRateLimited(_retry_after(exc)) from exc
    except (OpenAIError, ValueError) as exc:
        raise RuntimeError(f'answer_failed: {type(exc).__name__}') from exc

    result = completion.choices[0].message.parsed
    # 프롬프트로 막아도 가끔 [0] 같은 인용 표시가 본문에 섞여 나온다.
    result.answer = CITATION_MARKER.sub('', result.answer).strip()
    # 모델이 없는 번호를 인용하는 경우가 있어 실제 후보로만 걸러낸다.
    cited = [entries[i] for i in result.cited_indexes if 0 <= i < len(entries)]
    usage = {
        'model': settings.OPENAI_ANSWER_MODEL,
        'promptTokens': completion.usage.prompt_tokens if completion.usage else None,
        'completionTokens': completion.usage.completion_tokens if completion.usage else None,
        'latencyMs': int((time.time() - started) * 1000),
    }

    return result, cited, retrieval, usage
