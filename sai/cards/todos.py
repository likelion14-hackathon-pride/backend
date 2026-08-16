from datetime import timedelta

from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from .models import InstructionCard, Task
from .queries import cards_for

# 홈 화면이 유지하는 미완료 칸 수. 비면 Ready 카드에서 채운다.
# 직접 추가한 것 때문에 이보다 많아질 수는 있다. 그때는 채우지 않는다.
TODO_SLOTS = 5

# 완료한 항목을 바로 지우지 않는다. 체크한 것이 목록 아래에 하루는 남아 있어야
# 방금 무엇을 끝냈는지 보인다. 지우는 일은 워커가 주기적으로 한다.
KEEP_DONE = timedelta(days=1)

# 완료한 것은 아래로 내린다.
_ORDER = Case(
    When(status=Task.Status.DONE, then=Value(1)),
    default=Value(0),
    output_field=IntegerField(),
)


def _open(company, user):
    return Task.objects.filter(company=company, user=user).exclude(
        status=Task.Status.DONE
    )


# 미완료 칸이 비면 나에게 배정된 Ready 카드에서 최신순으로 채운다.
# 담긴 할 일을 체크해도 카드는 그대로다. 개인 체크리스트와 팀이 공유하는 작업은 다르다.
def fill(company, user):
    missing = TODO_SLOTS - _open(company, user).count()
    if missing <= 0:
        return []

    taken = Task.objects.filter(
        company=company, user=user, card__isnull=False
    ).values_list('card_id', flat=True)
    cards = (
        cards_for(company)
        .filter(
            assignee=user,
            duplicate_of__isnull=True,
            column=InstructionCard.Column.READY,
        )
        .exclude(id__in=taken)
        .order_by('-created_at', '-id')[:missing]
    )
    # 최신 카드부터 고르되 넣는 순서는 뒤집는다. 목록이 id 역순이라
    # 가장 최근 카드가 마지막에 들어가야 맨 위에 선다.
    cards = list(cards)[::-1]

    return Task.objects.bulk_create([
        Task(
            company=company,
            user=user,
            scope=card.scope,
            card=card,
            title=(card.purpose_en or card.purpose)[:200],
            due_at=card.deadline_at,
        )
        for card in cards
    ])


def todos_for(company, user):
    fill(company, user)

    return (
        Task.objects.filter(company=company, user=user)
        .select_related('scope', 'card__document__author_identity', 'card__document__item')
        .annotate(done_last=_ORDER)
        .order_by('done_last', '-id')
    )


# 완료하고 하루가 지난 항목을 지운다. 워커가 부른다.
# 마지막 실행 시각을 어디에도 남기지 않는다. 조건이 데이터에만 걸려 있어
# 워커가 다시 떠도 방금 체크한 것을 앞당겨 지우지 않는다.
def purge_done(now=None):
    cutoff = (now or timezone.now()) - KEEP_DONE
    deleted, _ = Task.objects.filter(
        status=Task.Status.DONE, done_at__lt=cutoff
    ).delete()

    return deleted
