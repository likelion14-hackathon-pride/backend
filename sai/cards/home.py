from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from handbook.models import HandbookEntry
from handbook.queries import scopes_with_counts
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

# 대표를 부르지 않고 끝난 답변.
RESOLVED = [Message.Verdict.GROUNDED, Message.Verdict.GROUNDED_BY_CASES]


# '오늘'은 회사가 있는 곳 기준이다. UTC 자정으로 자르면 서울에서 아침 9시에
# 어제치가 섞여 보인다.
def _start_of_day(company, now):
    local = now.astimezone(ZoneInfo(company.timezone))

    return local.replace(hour=0, minute=0, second=0, microsecond=0)


# 오늘 SAI 가 읽고 처리한 양. 회사 전체의 상태다.
# 기다리는 것만 개인 것이다. 남이 보낸 질문을 내가 기다릴 이유가 없다.
def _read_today(company, user, since):
    return {
        'messages': RawDocument.objects.filter(
            company=company, occurred_at__gte=since
        ).count(),
        'cards': InstructionCard.objects.filter(
            company=company, created_at__gte=since, duplicate_of__isnull=True
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


# 물어본 것 중 대표를 부르지 않고 끝난 비율.
# 카드의 미정 항목을 핸드북으로 먼저 답한 것도 여기 들어간다.
def _resolution(company, since):
    answers = Message.objects.filter(
        company=company, role=Message.Role.AI, created_at__gte=since
    )

    return {
        'answered': answers.filter(verdict__in=RESOLVED).count(),
        'total': answers.count(),
        'since': since,
    }


def _handbook(company, now):
    confirmed = HandbookEntry.objects.filter(
        company=company, status=HandbookEntry.Status.CONFIRMED
    )
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # 확정 시각이 없는 항목은 언제부터 있었는지 알 수 없다. 빼면 마지막 칸이
    # 총계보다 작아져 그래프가 총계와 어긋난다. 처음부터 있었던 것으로 센다.
    since_always = Q(confirmed_at__isnull=True)
    weekly = [
        confirmed.filter(Q(confirmed_at__lte=now - timedelta(weeks=index)) | since_always).count()
        for index in range(GROWTH_WEEKS - 1, -1, -1)
    ]

    return {
        'confirmed': confirmed.count(),
        'addedThisMonth': confirmed.filter(confirmed_at__gte=month).count(),
        # 누적이라 우상향한다. 주마다 몇 개 늘었는지가 아니라 얼마나 쌓였는지를 보여 준다.
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
