import logging

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import F
from django.utils import timezone
from openai import OpenAI, OpenAIError
from pgvector.django import CosineDistance

from config.ai import client_options, record_usage, timed_call

from .models import CompanyScope, HandbookEntry

logger = logging.getLogger(__name__)

# 같은 빈칸으로 볼 코사인 거리. 답변 검색(0.85)보다 훨씬 촘촘하다.
# 서로 다른 빈칸을 하나로 묶으면 대표가 하나만 답하고 끝났다고 생각하게 된다.
# 같은 빈칸이 두 줄로 남는 쪽이 덜 나쁘다.
GAP_MAX_DISTANCE = 0.25

BODY_KO = '이 주제에 정해진 내용이 없습니다.'
BODY_EN = 'Nothing is established for this yet.'


def _embed(question):
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 빈 항목을 남길 수 없습니다')

    client = OpenAI(api_key=settings.OPENAI_API_KEY, **client_options())

    with timed_call(settings.OPENAI_EMBEDDING_MODEL):
        response = client.embeddings.create(
            model=settings.OPENAI_EMBEDDING_MODEL, input=[question]
        )
    record_usage('handbook_gap_embedding', settings.OPENAI_EMBEDDING_MODEL, response)
    return response.data[0].embedding


def _fallback_scope(company):
    return CompanyScope.objects.filter(
        company=company,
        kind=CompanyScope.Kind.COMPANY,
        area_key=CompanyScope.AreaKey.COMPANY,
    ).first()


def _nearest(company, scope, vector):
    return (
        HandbookEntry.objects.filter(
            company=company,
            scope=scope,
            status=HandbookEntry.Status.BLANK,
            embedding_en__isnull=False,
        )
        .annotate(distance=CosineDistance('embedding_en', vector))
        .filter(distance__lte=GAP_MAX_DISTANCE)
        .order_by('distance')
        .first()
    )


# 근거가 없어 답하지 못한 질문을 빈 항목으로 남긴다.
# 추측으로 채우지 않고 비워 둔 자리가 어디인지 대표가 볼 수 있어야 한다.
# 같은 질문이 다시 오면 항목을 늘리지 않고 발생 횟수만 올린다.
# BLANK 는 확정 항목이 아니라 검색 후보에 들지 않으므로 답변 근거로 쓰이지 않는다.
#
# asked 는 사람이 실제로 물어본 것인지를 가른다. 카드의 미정 항목은 SAI 가 지시를 읽다
# 발견한 구멍이라 자리만 잡고 횟수는 세지 않는다. 세면 카드를 다시 만들 때마다 올라가
# '질문 3회 발생'이 재생성 횟수가 되어 버린다.
def record_gap(company, scope, question, asked=True):
    question = (question or '').strip()
    scope = scope or _fallback_scope(company)
    if not question or scope is None:
        return None

    try:
        vector = _embed(question)
    except (ImproperlyConfigured, OpenAIError, ValueError):
        # 빈 항목은 기록이지 답변이 아니다. 실패해도 질문 처리 자체를 막지 않는다.
        logger.exception('빈 항목 기록 실패 company=%s', company.id)
        return None

    existing = _nearest(company, scope, vector)
    if existing is not None:
        if asked:
            HandbookEntry.objects.filter(id=existing.id).update(
                ask_count=F('ask_count') + 1
            )
            existing.refresh_from_db(fields=['ask_count'])

        return existing

    return HandbookEntry.objects.create(
        company=company,
        scope=scope,
        title=question[:200],
        body_ko=BODY_KO,
        body_en=BODY_EN,
        original_lang='en',
        status=HandbookEntry.Status.BLANK,
        origin=HandbookEntry.Origin.ESCALATION,
        embedding_en=vector,
        embedding_model=settings.OPENAI_EMBEDDING_MODEL,
        embedded_at=timezone.now(),
        ask_count=1 if asked else 0,
    )
