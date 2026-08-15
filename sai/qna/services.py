from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import APIException

from .answering import (
    PROMPT_VERSION,
    AnswerRateLimited,
    answer_question,
    find_risk_warnings,
)
from .models import Citation, Message, Thread

# 근거가 없거나 판단이 필요한 경우는 대표 확인이 필요하다는 뜻이다.
NEEDS_OWNER = {'NO_SOURCE', 'NEEDS_DECISION'}


class AnswerUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = 'answer generation is unavailable'


class RateLimited(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = 'AI usage limit reached, try again shortly'

    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__()


def language_of(user):
    return user.ui_language if user.ui_language in ('ko', 'en') else 'en'


def open_thread(company, user, scope=None):
    return Thread.objects.create(company=company, user=user, scope=scope)


def ask(company, user, thread, question, scope=None, context=None):
    try:
        return _ask(company, user, thread, question, scope, context)
    except AnswerRateLimited as exc:
        raise RateLimited(exc.retry_after)
    except (ImproperlyConfigured, RuntimeError) as exc:
        raise AnswerUnavailable(str(exc))


def _ask(company, user, thread, question, scope, context):
    lang = language_of(user)
    body_field = 'body_en' if lang == 'en' else 'body_ko'

    Message.objects.create(
        company=company, thread=thread, role=Message.Role.USER, **{body_field: question}
    )

    asked = f'{context}\n\n{question}' if context else question
    result, cited, retrieval, usage = answer_question(company, asked, lang, scope)

    bodies = {body_field: result.answer or None}
    # 대표 확인이 필요한 답변은 한국어 초안이 본체다.
    # 여기서 저장해 두지 않으면 나중에 에스컬레이션을 만들 때 초안을 잃어버린다.
    if result.verdict in NEEDS_OWNER and result.draft_ko:
        bodies['body_ko'] = result.draft_ko

    with transaction.atomic():
        message = Message.objects.create(
            company=company,
            thread=thread,
            role=Message.Role.AI,
            verdict=result.verdict,
            model=usage['model'],
            prompt_version=PROMPT_VERSION,
            prompt_tokens=usage['promptTokens'],
            completion_tokens=usage['completionTokens'],
            # 청크 본문은 넣지 않는다. id와 점수만 남긴다.
            retrieval=retrieval,
            latency_ms=usage['latencyMs'],
            **bodies,
        )
        Citation.objects.bulk_create([
            Citation(
                company=company, message=message, entry=source.entry, chunk=source.chunk,
            )
            for source in cited
        ])

    return {
        'threadId': thread.id,
        'messageId': message.id,
        'resultType': 'NEEDS_OWNER' if result.verdict in NEEDS_OWNER else 'ANSWERED',
        'verdict': result.verdict,
        'answer': result.answer or None,
        'draftKo': result.draft_ko or None,
        'citations': [source.payload() for source in cited],
        'warnings': find_risk_warnings(company, question, result.answer),
        'latencyMs': usage['latencyMs'],
    }
