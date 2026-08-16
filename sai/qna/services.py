from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from openai import OpenAIError
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError

from cards.models import Blank
from handbook.gaps import record_gap
from handbook.models import CompanyScope, HandbookEntry, HandbookEvidence
from sources.slack import SlackError

from .answering import (
    PROMPT_VERSION,
    AnswerRateLimited,
    answer_question,
    find_risk_warnings,
)
from .escalation import draft_from_blank, fetch_reply, judge_reply
from .models import Citation, Escalation, Message, Thread

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


def draft_for_blank(blank):
    try:
        return draft_from_blank(blank)
    except ImproperlyConfigured as exc:
        raise AnswerUnavailable(str(exc))
    except (OpenAIError, ValueError) as exc:
        raise AnswerUnavailable(f'draft_failed: {type(exc).__name__}')


# AI 답변 메시지에 저장해 둔 한국어 초안. NEEDS_OWNER 판정일 때만 채워져 있다.
# AI 답변 메시지에 저장해 둔 한국어 초안. NEEDS_OWNER 판정일 때만 채워져 있다.
def draft_from_message(message):
    if message.verdict not in NEEDS_OWNER:
        return None

    return (message.body_ko or '').strip() or None


def create_escalation(company, user, question_en, draft_ko, scope=None, origin=None, blank=None):
    if not question_en:
        raise ValidationError({'questionEn': ['question text not found']})
    # 초안이 없으면 영어 원문이 그대로 대표에게 나간다. 그럴 바엔 막고 받는다.
    if not draft_ko:
        raise ValidationError({'draftKo': ['korean draft required']})

    with transaction.atomic():
        escalation = Escalation.objects.create(
            company=company,
            asked_by=user,
            scope=origin.thread.scope if origin else scope,
            origin_message=origin,
            question_en=question_en,
            draft_ko=draft_ko,
        )
        if blank is not None:
            blank.escalation = escalation
            blank.save(update_fields=['escalation'])

    return escalation


# 대표 답장을 회수해 판정한다. 아직 답이 없으면 아무것도 바꾸지 않는다.
def collect_answer(escalation):
    try:
        reply, text = fetch_reply(escalation)
    except SlackError as exc:
        raise ValidationError({'slack': [exc.code]})

    if reply is None:
        return escalation

    try:
        judgement = judge_reply(escalation.question_en, escalation.draft_ko, text)
    except (ImproperlyConfigured, RuntimeError) as exc:
        raise AnswerUnavailable(str(exc))

    escalation.answer_is_answer = judgement.is_answer
    escalation.answer_reason = judgement.reason[:200]
    escalation.answer_needs_review = judgement.needs_review
    if judgement.is_answer:
        escalation.answer_ko = judgement.answer_ko
        escalation.answer_en = judgement.answer_en
        escalation.answered_at = timezone.now()
        escalation.status = Escalation.Status.ANSWERED
    escalation.save()

    # 카드에서 올라온 질문이면 카드에도 답을 채운다.
    # 여기서 안 채우면 답은 왔는데 카드는 그대로 비어 있다.
    if judgement.is_answer:
        escalation.card_blanks.update(
            sai_answer_ko=judgement.answer_ko,
            sai_answer_en=judgement.answer_en,
            answered_by=Blank.AnsweredBy.OWNER,
        )

    return escalation


# 대표 답변을 핸드북 초안으로 만든다. 확정은 별도 검토에서 한다.
def promote_to_entry(escalation):
    company = escalation.company
    if escalation.status != Escalation.Status.ANSWERED or not escalation.answer_ko:
        raise ValidationError({'status': ['no answer to promote']})

    scope = escalation.scope or CompanyScope.objects.filter(
        company=company, kind=CompanyScope.Kind.COMPANY,
        area_key=CompanyScope.AreaKey.COMPANY,
    ).first()
    if scope is None:
        raise ValidationError({'scope': ['no scope available']})

    with transaction.atomic():
        entry = HandbookEntry.objects.create(
            company=company,
            scope=scope,
            title=escalation.question_en[:200],
            body_ko=escalation.answer_ko,
            body_en=escalation.answer_en or None,
            original_lang='ko',
            status=HandbookEntry.Status.DRAFT,
            origin=HandbookEntry.Origin.ESCALATION,
            confidence=HandbookEntry.Confidence.MEDIUM,
        )
        HandbookEvidence.objects.create(
            company=company,
            entry=entry,
            quote=escalation.answer_ko,
            tag=HandbookEvidence.Tag.OWNER,
            source_label='대표 확인 답변',
            speaker_name=None,
            occurred_at=escalation.answered_at,
        )
        escalation.proposed_entry = entry
        escalation.status = Escalation.Status.APPROVED
        escalation.save(update_fields=['proposed_entry', 'status'])

    return escalation


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

    # 답하지 못한 질문은 핸드북의 빈 자리다. 남겨 두지 않으면 대표는 무엇이 비었는지 모른다.
    if result.verdict in NEEDS_OWNER:
        record_gap(company, scope, question)

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
