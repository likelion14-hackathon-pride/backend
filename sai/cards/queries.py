from django.db.models import Case, Count, F, Q, Value, When
from django.db.models.fields import CharField

from qna.models import Escalation

from .models import InstructionCard

_OPEN = Q(blanks__escalation__status=Escalation.Status.SENT)
_ANSWERED = Q(
    blanks__escalation__status=Escalation.Status.ANSWERED,
    blanks__escalation__acknowledged_at__isnull=True,
)


# 보드 열. DONE 이 가장 세다. 질문이 열린 채로 끝낸 카드도 DONE 에 있어야 하기 때문.
# 그다음은 질문 상태가 정한다. 사람이 옮기는 것은 IN_PROGRESS / DONE 뿐이다.
_COLUMN = Case(
    When(status=InstructionCard.Status.DONE, then=Value(InstructionCard.Column.DONE)),
    When(open_question_count__gt=0, then=Value(InstructionCard.Column.WAITING)),
    When(answered_question_count__gt=0, then=Value(InstructionCard.Column.ANSWERED)),
    default=F('status'),
    output_field=CharField(),
)


def cards_for(company):
    return (
        InstructionCard.objects.filter(company=company)
        .select_related(
            'assignee', 'scope', 'document', 'document__item', 'document__author_identity'
        )
        .annotate(
            duplicate_count=Count('duplicates', distinct=True),
            open_question_count=Count('blanks__escalation', filter=_OPEN, distinct=True),
            answered_question_count=Count(
                'blanks__escalation', filter=_ANSWERED, distinct=True
            ),
        )
        .annotate(column=_COLUMN)
    )
