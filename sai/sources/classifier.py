from typing import Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from openai import OpenAI, OpenAIError
from pydantic import BaseModel

from .models import Identity, Item, RawDocument
from .text import normalize_slack_text

# 프롬프트를 고치면 이 값을 올린다. RawDocument.classifier_version에 기록되므로
# 나중에 "옛 프롬프트로 분류된 것만 다시 돌리기"가 가능하다.
CLASSIFIER_VERSION = 'clf-v2'

# 한 번의 호출에 넣는 메시지 수. 메시지마다 호출하면 비용과 시간이 수십 배가 된다.
BATCH_SIZE = 25

# 한국어판과 A/B 비교했을 때 한국어 데이터 정확도는 동일하고(97.1%),
# 영어 확정 표현까지 커버하므로 이 버전을 쓴다. 예시는 한/영 둘 다 둔다.
SYSTEM_PROMPT = """You classify Slack messages from a Korean startup to extract internal company rules.

The company has foreign employees who do not read Korean well. The goal is to surface
"how this company works" so they can follow it.

Classify each message into exactly one label.

INSTRUCTION - states a rule, standard, or procedure that applies repeatedly from now on
  "배포는 금요일 오후에는 하지 않는 걸로 합시다"
  "일반 PR은 승인 1명, 마이그레이션 포함된 PR만 2명으로 하죠"
  "Let's not deploy on Friday afternoons"
  "Going forward, please post in #dev before deploying"

CONTEXT - work related but not a rule: one-off notices, status updates, small talk, acknowledgements
  "오늘 오후에 병원 들렀다 와서 3시쯤 복귀합니다"
  "staging 서버 방금 재기동했습니다"
  "I'm heading out early today"
  "넵 알겠습니다" / "Got it"

AMBIGUOUS - could become a rule but is not settled: the discussion stalled or disagreement remains
  "테스트 커버리지 80%면 좀 빡센가요"
  "에러 알림 좀 줄여야 할 것 같은데" -> "나중에 정리하죠"
  "Should we require two reviewers?" with no conclusion

How to decide
- The core question is "will this keep applying from now on?" A one-time event is CONTEXT.
- A question with no conclusion is AMBIGUOUS.
- Commitment markers push toward INSTRUCTION. Korean: '~하겠습니다', '~로 합시다', '~하지 마세요',
  '~로 확정'. English: "let's", "from now on", "going forward", "please make sure", "never".
- For thread replies, judge within the context given in the parent line.
- Do not over-assign INSTRUCTION. A wrong rule is more harmful than a missed one.
- Messages may be in Korean or English. Apply the same criteria to both.

Return a label for every index given in the input."""


class MessageLabel(BaseModel):
    index: int
    label: Literal['INSTRUCTION', 'CONTEXT', 'AMBIGUOUS']


class ClassificationResult(BaseModel):
    labels: list[MessageLabel]


def _get_client():
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 분류를 실행할 수 없습니다')

    return OpenAI(api_key=settings.OPENAI_API_KEY)


# 채널 ID -> '#dev', 슬랙 유저 ID -> '조상원' 매핑.
# 멘션을 사람이 읽는 형태로 바꾸는 데 쓴다.
def build_lookup(company_id):
    channels = dict(
        Item.objects.filter(company_id=company_id).values_list('external_id', 'label')
    )
    users = dict(
        Identity.objects.filter(company_id=company_id)
        .exclude(external_handle=None)
        .values_list('external_user_id', 'external_handle')
    )

    return channels, users


def _render(document, index, channels, users, parents):
    text = normalize_slack_text(document.raw_text, channels, users)
    author = document.author_identity.external_handle if document.author_identity else '알수없음'
    lines = [f'[{index}] 채널={document.item.label} 작성자={author}']

    # 스레드 답글은 부모 발언을 봐야 의미가 잡힌다.
    parent = parents.get(document.thread_ref)
    if parent:
        lines.append(f'    parent: {normalize_slack_text(parent, channels, users)[:200]}')

    lines.append(f'    text: {text}')

    return '\n'.join(lines)


def _classify_batch(client, documents, channels, users, parents):
    prompt = '\n'.join(
        _render(document, index, channels, users, parents)
        for index, document in enumerate(documents)
    )
    completion = client.chat.completions.parse(
        model=settings.OPENAI_CLASSIFIER_MODEL,
        messages=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': prompt},
        ],
        response_format=ClassificationResult,
        temperature=0,
    )
    result = completion.choices[0].message.parsed

    return {label.index: label.label for label in result.labels}


# 아직 분류되지 않았거나 옛 프롬프트로 분류된 원문을 분류한다.
# (분류 건수, 배치 오류 목록) 반환.
def classify_documents(company_id, documents=None):
    if documents is None:
        documents = RawDocument.objects.filter(company_id=company_id)

    # 현재 프롬프트 버전으로 이미 분류된 것만 건너뛴다.
    # 미분류(version=None)와 옛 버전으로 분류된 것은 모두 다시 돌린다.
    documents = list(
        documents.exclude(classifier_version=CLASSIFIER_VERSION)
        .select_related('item', 'author_identity')
        # 라벨을 인덱스로 되받으므로 순서가 흔들리면 남의 라벨이 붙는다.
        # occurred_at 이 같은 문서가 있어 id 로 한 번 더 묶는다.
        .order_by('occurred_at', 'id')
    )
    if not documents:
        return 0, []

    channels, users = build_lookup(company_id)
    # 스레드 답글의 부모 본문. 답글만 있는 배치에서도 맥락을 잃지 않게 미리 모아 둔다.
    thread_refs = {d.thread_ref for d in documents if d.thread_ref}
    parents = dict(
        RawDocument.objects.filter(company_id=company_id, external_ref__in=thread_refs)
        .values_list('external_ref', 'raw_text')
    )

    client = _get_client()
    classified = 0
    errors = []

    for start in range(0, len(documents), BATCH_SIZE):
        batch = documents[start:start + BATCH_SIZE]
        try:
            labels = _classify_batch(client, batch, channels, users, parents)
        except (OpenAIError, ValueError) as exc:
            errors.append({'scope': 'classify', 'batch': start // BATCH_SIZE, 'code': type(exc).__name__})
            continue

        updated = []
        for index, document in enumerate(batch):
            label = labels.get(index)
            if label is None:
                continue
            document.classified_as = label
            document.classifier_version = CLASSIFIER_VERSION
            updated.append(document)

        RawDocument.objects.bulk_update(updated, ['classified_as', 'classifier_version'])
        classified += len(updated)

    return classified, errors
