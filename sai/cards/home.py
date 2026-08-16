from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from handbook.models import HandbookEntry
from handbook.queries import live_entries, scopes_with_counts
from qna.models import Escalation, Message
from sources.models import RawDocument

from .models import InstructionCard
from .queries import cards_for
from .todos import todos_for

# SAI 해결률을 재는 구간. 하루로 보면 질문 한 건에 비율이 튀고,
# 한 달로 보면 지난주에 고친 것이 안 보인다.
RESOLUTION_DAYS = 7

# 핸드북이 자라는 모양을 보여 줄 주 수.
GROWTH_WEEKS = 4

# SAI가 실제로 답을 준 판정. 나머지는 답이 비어 있다.
ANSWERED = [Message.Verdict.GROUNDED, Message.Verdict.GROUNDED_BY_CASES]


# '오늘'은 회사가 있는 곳 기준이다. UTC 자정으로 자르면 서울에서 아침 9시에
# 어제치가 섞여 보인다.
def _start_of_day(company, now):
    local = now.astimezone(ZoneInfo(company.timezone))

    return local.replace(hour=0, minute=0, second=0, microsecond=0)


# 오늘 SAI 가 읽고 처리한 양. 회사 전체의 상태다.
# 기다리는 것만 개인 것이다. 남이 보낸 질문을 내가 기다릴 이유가 없다.
#
# 두 숫자는 '오늘 들어온 원문 N건 중 M건이 카드가 됐다'는 한 묶음이라
# 같은 시계를 봐야 한다. 카드를 만든 시각으로 세면 어제 밀린 것을 오늘 처리했을 때
# 원문 0건인데 카드 4건이 나온다.
def _read_today(company, user, since):
    return {
        'messages': RawDocument.objects.filter(
            company=company, occurred_at__gte=since
        ).count(),
        'cards': InstructionCard.objects.filter(
            company=company,
            document__occurred_at__gte=since,
            duplicate_of__isnull=True,
        ).count(),
        'waiting': Escalation.objects.filter(
            company=company, asked_by=user, status=Escalation.Status.SENT
        ).count(),
    }


# 아직 아무도 열어 보지 않은 지시. read_at 은 카드에 하나뿐이라 회사 기준이다.
def _unread(company):
    cards = (
        cards_for(company)
        .filter(read_at__isnull=True, duplicate_of__isnull=True)
        .order_by('-created_at', '-id')
    )
    latest = cards.first()

    return {
        'count': cards.count(),
        'latest': latest and {
            'cardId': latest.id,
            'purpose': latest.purpose_en or latest.purpose,
            'text': latest.document.raw_text if latest.document else None,
            'requestedBy': (
                latest.document.author_identity.external_handle
                if latest.document and latest.document.author_identity else None
            ),
            'occurredAt': latest.document.occurred_at if latest.document else None,
        },
    }


# 팀원이 물은 것 중 SAI가 답해 끝난 비율.
#
# 분모는 SAI가 답한 것 + 답하지 못해 팀원이 슬랙으로 보낸 것이다.
# 근거가 없다고 답했어도 팀원이 안 보내고 넘어갔으면 대표를 부른 적이 없으니 실패가 아니다.
# 회사 규칙과 무관한 질문(OUT_OF_SCOPE)은 답도 아니고 대표를 부르지도 않아 양쪽에서 빠진다.
#
# 카드의 미정 항목을 SAI가 먼저 답한 것은 여기 잡히지 않는다.
# 그 경로는 Message 를 남기지 않고, 팀원이 물어서 생긴 것도 아니다.
def _resolution(company, since):
    answered = Message.objects.filter(
        company=company, role=Message.Role.AI,
        created_at__gte=since, verdict__in=ANSWERED,
    ).count()
    escalated = Escalation.objects.filter(
        company=company, sent_at__gte=since, origin_message__isnull=False
    ).count()

    return {
        'answered': answered,
        'total': answered + escalated,
        'since': since,
    }


# 한 주 구간 안에서 확정된 규칙 수.
#
# 가장 최근 주(index 0)는 위쪽을 닫지 않는다. now 로 자르면 확정 시각이 조회 시각과
# 같은 항목이 이 주에도, 한 주 앞에도 들어가지 못하고 어느 칸에서도 사라진다.
# 시계는 밀리초 단위로 똑딱거리므로 방금 확정한 규칙에서 실제로 일어난다.
def _week_count(confirmed, now, index):
    week = confirmed.filter(confirmed_at__gte=now - timedelta(weeks=index + 1))
    if index:
        week = week.filter(confirmed_at__lt=now - timedelta(weeks=index))

    return week.count()


def _handbook(company, now):
    confirmed = live_entries(company).filter(status=HandbookEntry.Status.CONFIRMED)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # 총계와 이번 달 증가는 따로 나가므로 여기서는 '언제 늘었나'만 본다.
    # 누적으로 두면 마지막 칸이 confirmed 와 같아 같은 말을 두 번 하게 된다.
    #
    # 확정 시각이 없는 옛 항목은 어느 주에도 넣지 않는다. 언제였는지 모르기 때문이다.
    # 그래서 주별 합이 총계보다 작을 수 있다.
    weekly = [
        _week_count(confirmed, now, index)
        for index in range(GROWTH_WEEKS - 1, -1, -1)
    ]

    return {
        'confirmed': confirmed.count(),
        'addedThisMonth': confirmed.filter(confirmed_at__gte=month).count(),
        'weekly': weekly,
        'scopes': scopes_with_counts(company).order_by('kind', 'name'),
    }


def home_for(company, user):
    now = timezone.now()

    return {
        'readToday': _read_today(company, user, _start_of_day(company, now)),
        'unread': _unread(company),
        'todos': todos_for(company, user),
        'resolution': _resolution(company, now - timedelta(days=RESOLUTION_DAYS)),
        'handbook': _handbook(company, now),
    }
