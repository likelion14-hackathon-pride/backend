from statistics import median

from django.db.models import Q
from django.utils import timezone

from accounts.models import Membership
from companies.timing import (
    WorkingHours,
    add_working_minutes,
    next_start,
    state,
    working_minutes_between,
)
from qna.models import Escalation

from .models import Blank, InstructionCard, Step

# 모달에서 훑어보는 목록이다. 다 내려보내면 카드 수만큼 길어진다.
BUCKET_LIMIT = 5

# 답변 소요시간의 표본. 한두 건을 중앙값이라고 부를 수는 없다.
MIN_SAMPLES = 3
HISTORY_SIZE = 20

HISTORY = 'HISTORY'
WORKING_HOURS = 'WORKING_HOURS'
BASES = (HISTORY, WORKING_HOURS)

# 손을 댄 카드만 본다. 아직 안 잡은 일(READY)과 끝낸 일(DONE)은 여기 없다.
# ANSWERED 는 답이 도착했을 뿐 아직 하던 일이므로 포함한다.
OPEN_COLUMNS = [
    InstructionCard.Column.IN_PROGRESS,
    InstructionCard.Column.WAITING,
    InstructionCard.Column.ANSWERED,
]

# 아직 답이 오지 않은 미정 항목. 답이 온 것은 더 이상 사람을 기다리지 않는다.
_UNANSWERED = Q(escalation__isnull=True) | Q(
    escalation__status__in=[Escalation.Status.DRAFT, Escalation.Status.SENT]
)


def _owner(company):
    membership = (
        Membership.objects.select_related('user')
        .filter(company=company, role=Membership.Role.OWNER, left_at__isnull=True)
        .first()
    )

    return membership.user if membership else None


def _hours(company, zone):
    return WorkingHours(
        zone,
        company.working_hours_start,
        company.working_hours_end,
        company.working_hours_enabled,
    )


def _person(user, hours, now):
    return {
        'name': user.display_name if user else None,
        'timezone': hours.timezone,
        'state': state(hours, now),
    }


def _answer_lags(company, hours):
    recent = (
        Escalation.objects.filter(
            company=company, sent_at__isnull=False, answered_at__isnull=False
        )
        .order_by('-answered_at')
        .values_list('sent_at', 'answered_at')[:HISTORY_SIZE]
    )

    return [working_minutes_between(hours, sent, answered) for sent, answered in recent]


# 대표가 실제로 답한 이력에서 근무시간 기준 소요분의 중앙값을 뽑는다.
# 표본이 모자라면 다음 근무 시작 시각을 그대로 쓴다. 어느 쪽인지는 basis 로 밝힌다.
def _reply_expected(company, hours, now):
    opening = next_start(hours, now)
    lags = _answer_lags(company, hours)
    if len(lags) < MIN_SAMPLES:
        return {'at': opening, 'basis': WORKING_HOURS, 'sampleSize': len(lags)}

    return {
        'at': add_working_minutes(hours, opening, median(lags)),
        'basis': HISTORY,
        'sampleSize': len(lags),
    }


# 대표를 기다리지 않는 카드의 단계. 핸드북에 근거가 있는 것부터 보여 준다.
# 근거를 요구하지는 않는다. 규칙이 붙은 단계는 실측에서 17개 중 0개였다.
# 검색된 규칙이 그 단계를 실제로 지배하는 경우가 드물기 때문이고, 그 판단은 맞다.
def _can_do(card_ids):
    steps = (
        Step.objects.filter(card_id__in=card_ids)
        .select_related('entry', 'entry__scope')
        .annotate(has_rule=Q(entry__isnull=False))
        .order_by('-has_rule', 'card__deadline_at', 'card_id', 'ord')
    )
    items = [
        {
            'cardId': step.card_id,
            'stepId': step.id,
            'title': step.text_en or step.text,
            'entryId': step.entry_id,
            'entryTitle': step.entry.title if step.entry else None,
            'scopeName': step.entry.scope.name if step.entry else None,
        }
        for step in steps[:BUCKET_LIMIT]
    ]

    return items, steps.count()


# 아직 답이 없는 미정 항목. 대표가 답해야 풀린다.
# 어느 카드가 막혀 있는지도 함께 돌려준다. 막힌 카드의 단계는 지금 할 일이 아니다.
def _needs_person(card_ids):
    blanks = (
        Blank.objects.filter(card_id__in=card_ids)
        .filter(_UNANSWERED)
        .order_by('card__deadline_at', 'card_id', 'id')
    )
    blocked = list(blanks.values_list('card_id', flat=True))
    shown = blanks.select_related('card__scope', 'escalation')[:BUCKET_LIMIT]
    items = [
        {
            'cardId': blank.card_id,
            'blankId': blank.id,
            'title': blank.question_en,
            'scopeName': blank.card.scope.name if blank.card.scope else None,
            'escalationStatus': blank.escalation.status if blank.escalation else None,
        }
        for blank in shown
    ]

    return items, len(blocked), set(blocked)


def timing_for(company, user, cards):
    now = timezone.now()
    hours = _hours(company, company.timezone)
    # 구성원별 근무시간을 받는 곳은 없다. 대표와 같은 근무시간을 쓰되
    # 시계만 각자의 지역으로 돌린다.
    your_hours = _hours(company, user.timezone)

    card_ids = list(
        cards.filter(duplicate_of__isnull=True, column__in=OPEN_COLUMNS)
        .values_list('id', flat=True)
    )
    needs_person, needs_person_total, blocked = _needs_person(card_ids)
    can_do, can_do_total = _can_do([i for i in card_ids if i not in blocked])

    return {
        'now': now,
        'you': _person(user, your_hours, now),
        'owner': _person(_owner(company), hours, now),
        'workingHours': {
            'enabled': company.working_hours_enabled,
            'start': company.working_hours_start,
            'end': company.working_hours_end,
            'timezone': company.timezone,
        },
        'replyExpected': _reply_expected(company, hours, now),
        'canDo': can_do,
        'canDoTotal': can_do_total,
        'needsPerson': needs_person,
        'needsPersonTotal': needs_person_total,
    }
