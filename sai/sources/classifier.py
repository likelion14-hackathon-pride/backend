from typing import Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from openai import OpenAI, OpenAIError
from pydantic import BaseModel

from .models import Identity, Item, RawDocument
from .text import normalize_slack_text

# 프롬프트를 고치면 이 값을 올린다. RawDocument.classifier_version에 기록되므로
# 나중에 "옛 프롬프트로 분류된 것만 다시 돌리기"가 가능하다.
CLASSIFIER_VERSION = 'clf-v1'

# 한 번의 호출에 넣는 메시지 수. 메시지마다 호출하면 비용과 시간이 수십 배가 된다.
BATCH_SIZE = 25

SYSTEM_PROMPT = """당신은 한국 스타트업의 슬랙 대화에서 사내 규칙을 찾아내는 분류기입니다.

이 회사에는 한국어가 서툰 외국인 직원이 있습니다. 그들이 알아야 할 '이 회사가 일하는 방식'을
뽑아내는 것이 목적입니다.

각 메시지를 아래 셋 중 하나로 분류하세요.

INSTRUCTION — 앞으로도 반복 적용되는 규칙·기준·절차를 정하는 발언
  "배포는 금요일 오후에는 하지 않는 걸로 합시다"
  "일반 PR은 승인 1명, 마이그레이션 포함된 PR만 2명으로 하죠"
  "시크릿 키는 절대 커밋하지 마세요"
  "주간 회의는 매주 화요일 오전 10시로 하겠습니다"

CONTEXT — 업무와 관련 있지만 규칙은 아닌 것. 일회성 공지, 상태 공유, 잡담, 단순 응답
  "오늘 오후에 병원 들렀다 와서 3시쯤 복귀합니다"
  "staging 서버 방금 재기동했습니다"
  "점심 뭐 드실래요"
  "넵 알겠습니다"

AMBIGUOUS — 규칙이 될 수 있으나 아직 확정되지 않은 것. 논의가 중단되었거나 이견이 남은 상태
  "테스트 커버리지 80%면 좀 빡센가요"
  "에러 알림이 너무 많이 와요. 좀 줄여야 할 것 같은데" → "나중에 정리하죠"

판단 기준
- 핵심 질문은 '앞으로도 계속 그렇게 하는가?'입니다. 한 번의 사건이면 CONTEXT입니다.
- 질문만 있고 결론이 없으면 AMBIGUOUS입니다.
- 확정 표현('~하겠습니다', '~로 합시다', '~하지 마세요', '~로 확정')이 있으면 INSTRUCTION 쪽입니다.
- 스레드 답글은 parent에 적힌 원 발언의 맥락 안에서 판단하세요.
- 애매하면 INSTRUCTION으로 과하게 분류하지 마세요. 잘못된 규칙이 만들어지는 쪽이 더 해롭습니다.

입력에 주어진 모든 메시지에 대해 index를 그대로 붙여 결과를 돌려주세요."""


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
        .order_by('occurred_at')
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
