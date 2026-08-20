import logging
import re
from datetime import timedelta

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from openai import OpenAIError
from rest_framework.exceptions import ValidationError

from cards.models import Blank, InstructionCard
from config.errors import (
    AI_UNAVAILABLE,
    DRAFT_REQUIRED,
    NO_ANSWER_TO_PROMOTE,
    NO_SCOPE_AVAILABLE,
    QUESTION_TEXT_MISSING,
    RateLimited,
    UpstreamError,
)
from handbook.finalizing import finalize_entries
from handbook.gaps import record_gap
from handbook.models import CompanyScope, HandbookEntry, HandbookEvidence
from sources.models import Item
from sources.slack import SlackError

from .answering import (
    PROMPT_VERSION,
    AnswerRateLimited,
    answer_question,
    find_risk_warnings,
)
from .escalation import (
    draft_from_blank,
    fetch_reply,
    judge_reply,
    parse_thread_ref,
    translate_additions,
)
from .models import Citation, Escalation, Message, Thread

logger = logging.getLogger(__name__)

# 근거가 없거나 판단이 필요한 경우는 대표 확인이 필요하다는 뜻이다.
NEEDS_OWNER = {'NO_SOURCE', 'NEEDS_DECISION'}

# 한 바퀴에 회수할 답장 수. 건당 슬랙 조회와 AI 판정이 붙으므로,
# 워커의 스케줄링 한 바퀴(60초)를 넘기지 않을 만큼만 잡는다. 밀린 것은 다음 바퀴가 가져간다.
PENDING_ANSWER_LIMIT = 5

# 이보다 오래 회수되지 않은 표시는 버린다. 채널에서 봇이 빠졌거나 스레드가 지워진 경우
# 계속 재시도하면 슬랙을 매분 부르게 된다. 이때는 화면의 '답장 확인' 버튼이 남는다.
PENDING_ANSWER_MAX_AGE = timedelta(hours=6)

QUESTION_OR_REQUEST = (
    '?',
    '인가요',
    '나요',
    '까요',
    '습니까',
    '부탁드립니다',
    '부탁합니다',
    '확인 부탁',
    '확인 요청',
    '알려주세요',
)


# AI가 답을 만들지 못한 경우. 설정 누락이든 OpenAI 오류든 사용자가 할 수 있는 일은 같다.
# 원인은 reason 으로 받아 로그에만 남긴다. 그대로 내보내면 서버 설정이 화면에 뜬다.
class AnswerUnavailable(UpstreamError):
    def __init__(self, reason=None):
        self.reason = reason
        super().__init__(AI_UNAVAILABLE, 'answer generation is unavailable')


def language_of(user):
    return user.ui_language if user.ui_language in ('ko', 'en') else 'en'


def open_thread(company, user, scope=None, card=None):
    return Thread.objects.create(company=company, user=user, scope=scope, card=card)


def draft_for_blank(blank):
    try:
        return draft_from_blank(blank)
    except ImproperlyConfigured as exc:
        raise AnswerUnavailable(str(exc))
    except (OpenAIError, ValueError) as exc:
        raise AnswerUnavailable(f'draft_failed: {type(exc).__name__}')


# 팀원이 덧붙인 줄을 한국어로 바꾼다. 실패하면 보내지 않는다.
# 영어가 그대로 나가면 한국어를 직접 쓰지 않아도 된다는 약속이 깨진다.
def korean_additions(lines):
    try:
        return translate_additions(lines)
    except ImproperlyConfigured as exc:
        raise AnswerUnavailable(str(exc))
    except (OpenAIError, ValueError, RuntimeError) as exc:
        raise AnswerUnavailable(f'addition_failed: {type(exc).__name__}')


# AI 답변 메시지에 저장해 둔 한국어 초안. NEEDS_OWNER 판정일 때만 채워져 있다.
def draft_from_message(message):
    if message.verdict not in NEEDS_OWNER:
        return None

    return (message.body_ko or '').strip() or None


def create_escalation(company, user, question_en, draft_ko, scope=None, origin=None, blank=None):
    if not question_en:
        raise ValidationError('question text not found', code=QUESTION_TEXT_MISSING)
    # 초안이 없으면 영어 원문이 그대로 대표에게 나간다. 그럴 바엔 막고 받는다.
    if not draft_ko:
        raise ValidationError('korean draft required', code=DRAFT_REQUIRED)

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


def ensure_waiting_card(escalation):
    if escalation.card_blanks.exists():
        return None
    if escalation.origin_message is None or escalation.origin_message.thread.card_id:
        return None

    card = InstructionCard.objects.create(
        company=escalation.company,
        scope=escalation.scope,
        assignee=escalation.asked_by,
        purpose=escalation.draft_ko,
        purpose_en=escalation.question_en,
        status=InstructionCard.Status.IN_PROGRESS,
    )
    Blank.objects.create(
        company=escalation.company,
        card=card,
        question_en=escalation.question_en,
        escalation=escalation,
    )

    return card


def _looks_like_question_or_request(text):
    text = (text or '').strip()
    if not text:
        return False

    return any(marker in text for marker in QUESTION_OR_REQUEST)


def _title_from_answer(answer):
    answer = (answer or '').strip()
    if not answer:
        return None

    first = re.split(r'[\n.!?]', answer, maxsplit=1)[0].strip()
    if not first or _looks_like_question_or_request(first):
        return None

    return first[:80]


def _clean_proposed_title(title, answer):
    title = (title or '').strip()
    if title and not _looks_like_question_or_request(title):
        return title[:200]

    return _title_from_answer(answer)


def _reject_as_bad_answer(escalation):
    escalation.answer_is_answer = False
    escalation.answer_reason = (
        '답변이 질문이나 확인 요청 형태로 정리되어 규칙으로 저장하지 않았습니다.'
    )
    escalation.answer_needs_review = True
    escalation.save(update_fields=['answer_is_answer', 'answer_reason', 'answer_needs_review'])

    return escalation


# 대표 답장을 회수해 판정한다. 아직 답이 없으면 아무것도 바꾸지 않는다.
def collect_answer(escalation):
    # SlackError 는 DomainError 라서 그대로 두면 봉투까지 올라간다.
    reply, text = fetch_reply(escalation)

    if reply is None:
        return escalation

    try:
        judgement = judge_reply(escalation.question_en, escalation.draft_ko, text)
    except (ImproperlyConfigured, RuntimeError) as exc:
        raise AnswerUnavailable(str(exc))

    escalation.answer_reason = judgement.reason[:200]
    escalation.answer_needs_review = judgement.needs_review
    if judgement.is_answer:
        if _looks_like_question_or_request(judgement.answer_ko):
            return _reject_as_bad_answer(escalation)

        title = _clean_proposed_title(judgement.title_ko, judgement.answer_ko)
        if not title:
            escalation.answer_needs_review = True

        escalation.answer_is_answer = True
        escalation.answer_ko = judgement.answer_ko
        escalation.answer_en = judgement.answer_en
        escalation.proposed_title = title
        escalation.answered_at = timezone.now()
        escalation.status = Escalation.Status.ANSWERED
    else:
        escalation.answer_is_answer = False
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


# 웹훅이 표시해 둔 답장을 회수한다. 워커가 주기적으로 부른다.
# 대표가 슬랙에 답해도 아무도 check-answer 를 누르지 않으면 질문이 답변대기에 남기 때문이다.
def collect_pending_answers(now=None):
    now = now or timezone.now()
    escalations = list(
        Escalation.objects.filter(
            status=Escalation.Status.SENT,
            reply_pending_at__isnull=False,
            reply_pending_at__gte=now - PENDING_ANSWER_MAX_AGE,
        ).select_related('company').order_by('reply_pending_at')[:PENDING_ANSWER_LIMIT]
    )

    collected = 0
    for escalation in escalations:
        try:
            collect_answer(escalation)
        except (SlackError, AnswerUnavailable) as exc:
            # 표시를 남겨 두면 다음 바퀴에 다시 시도한다. 일시적인 장애가 대부분이다.
            logger.warning(
                '답장 회수 실패 escalation=%s code=%s', escalation.id, type(exc).__name__
            )
            continue

        Escalation.objects.filter(id=escalation.id).update(reply_pending_at=None)
        collected += 1

    return collected


def default_scope(escalation):
    return escalation.scope or CompanyScope.objects.filter(
        company=escalation.company, kind=CompanyScope.Kind.COMPANY,
        area_key=CompanyScope.AreaKey.COMPANY,
    ).first()


def _proposed_title(escalation):
    return (
        _clean_proposed_title(escalation.proposed_title, escalation.answer_ko)
        or '대표 답변을 확인해 규칙으로 저장합니다.'
    )


# 승인 버튼을 누르기 전에 어떤 규칙이 어디에 저장될지 보여 준다.
# 답이 오기 전에는 제안할 것이 없다.
def proposal_for(escalation):
    if escalation.status != Escalation.Status.ANSWERED or not escalation.answer_ko:
        return None

    scope = default_scope(escalation)
    channel, _ = parse_thread_ref(escalation.slack_thread_ref)

    return {
        'title': _proposed_title(escalation),
        'bodyKo': escalation.answer_ko,
        'bodyEn': escalation.answer_en or None,
        'scopeId': scope.id if scope else None,
        'scopeName': scope.name if scope else None,
        'scopeKind': scope.kind if scope else None,
        'sourceLabel': _channel_label(escalation.company, channel),
        'answeredAt': escalation.answered_at,
    }


def _channel_label(company, external_id):
    if not external_id:
        return None
    item = Item.objects.filter(company=company, external_id=external_id).first()

    return item.label if item else None


# 대표 답변을 핸드북 규칙으로 만든다.
# 미리보기에서 고친 제목·영문·계층이 오면 그것으로 저장한다.
#
# 여기서 바로 확정한다. 승인 버튼 자체가 '핸드북에 넣겠다'는 결정이라,
# 초안으로 두면 대표가 확인보관함에서 같은 결정을 한 번 더 하게 된다.
def promote_to_entry(escalation, title=None, body_en=None, scope=None):
    company = escalation.company
    if escalation.status != Escalation.Status.ANSWERED or not escalation.answer_ko:
        raise ValidationError('no answer to promote', code=NO_ANSWER_TO_PROMOTE)
    if _looks_like_question_or_request(escalation.answer_ko):
        raise ValidationError('no answer to promote', code=NO_ANSWER_TO_PROMOTE)

    scope = scope or default_scope(escalation)
    if scope is None:
        raise ValidationError('no scope available', code=NO_SCOPE_AVAILABLE)

    now = timezone.now()
    with transaction.atomic():
        entry = HandbookEntry.objects.create(
            company=company,
            scope=scope,
            title=(title or _proposed_title(escalation))[:200],
            body_ko=escalation.answer_ko,
            body_en=body_en or escalation.answer_en or None,
            original_lang='ko',
            status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.ESCALATION,
            confidence=HandbookEntry.Confidence.MEDIUM,
            reviewed_at=now,
            confirmed_at=now,
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

    # 번역과 임베딩이 없으면 확정 상태여도 검색에 걸리지 않아 답변에 쓰이지 않는다.
    finalize_entries([entry])

    return escalation


def ask(company, user, thread, question, scope=None, context=None):
    try:
        return _ask(company, user, thread, question, scope, context)
    except AnswerRateLimited as exc:
        raise RateLimited(exc.retry_after)
    except (ImproperlyConfigured, RuntimeError) as exc:
        raise AnswerUnavailable(str(exc))


def _ask(company, user, thread, question, scope, context):
    user_body_field = 'body_en' if language_of(user) == 'en' else 'body_ko'

    Message.objects.create(
        company=company, thread=thread, role=Message.Role.USER, **{user_body_field: question}
    )

    asked = f'{context}\n\n{question}' if context else question
    result, cited, retrieval, usage = answer_question(company, asked, 'en', scope)

    bodies = {'body_en': result.answer or None}
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
        'warnings': find_risk_warnings(company, question, result.answer, english=True),
        'latencyMs': usage['latencyMs'],
    }
