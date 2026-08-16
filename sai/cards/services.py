from django.utils import timezone
from rest_framework.exceptions import ValidationError

from config.errors import INVALID_STATUS_MOVE
from handbook.retrieval import search_rules
from handbook.services import scopes_in_view

from .generation import MAX_RULES, RULE_MAX_DISTANCE
from .models import InstructionCard

# 어느 열에서 어느 상태로 옮길 수 있는가.
# READY 로는 아무도 돌아가지 못한다. 한 번 잡은 일을 안 잡은 것으로 되돌릴 수 없다.
# DONE 은 실제로 손을 댄 뒤에만 찍는다. 답을 기다리는 중(WAITING)에는 끝났다고 할 수 없다.
ALLOWED_MOVES = {
    InstructionCard.Column.READY: {InstructionCard.Status.IN_PROGRESS},
    InstructionCard.Column.IN_PROGRESS: {
        InstructionCard.Status.IN_PROGRESS, InstructionCard.Status.DONE,
    },
    InstructionCard.Column.WAITING: {InstructionCard.Status.IN_PROGRESS},
    InstructionCard.Column.ANSWERED: {
        InstructionCard.Status.IN_PROGRESS, InstructionCard.Status.DONE,
    },
    InstructionCard.Column.DONE: {InstructionCard.Status.IN_PROGRESS},
}


def check_move(column, target):
    if target not in ALLOWED_MOVES.get(column, set()):
        raise ValidationError(
            f'cannot move to {target} from {column}', code=INVALID_STATUS_MOVE
        )


def original_text(card):
    return card.document.raw_text if card.document else ''


# 카드 생성 때 저장해 둔 벡터를 다시 쓴다. 상세를 열 때마다 임베딩을 부르지 않는다.
# 검색 시점에 도는 덕분에 핸드북이 자라면 걸리는 규칙도 같이 늘어난다.
def related_rules(card):
    if card.embedding is None:
        return []

    return search_rules(
        card.embedding,
        card.company,
        scopes_in_view(card.company, card.scope),
        MAX_RULES,
        RULE_MAX_DISTANCE,
    )


# 카드 화면에서 묻는 질문은 이 지시에 대한 것이다.
# 원문과 목적을 함께 넘기지 않으면 '얼마나 깊게 봐야 하나요' 같은 질문이 허공에 뜬다.
def card_context(card):
    lines = [f'The reader is working on this instruction: {card.purpose_en or card.purpose}']
    original = original_text(card)
    if original:
        lines.append(f'Original Slack message:\n{original[:500]}')

    return '\n\n'.join(lines)


def mark_read(card):
    if card.read_at is None:
        card.read_at = timezone.now()
        card.save(update_fields=['read_at'])

    return card
