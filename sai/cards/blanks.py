import logging

from django.core.exceptions import ImproperlyConfigured

from handbook.gaps import record_gap
from qna.answering import AnswerRateLimited, answer_question

from .models import Blank

logger = logging.getLogger(__name__)

# 핸드북이나 과거 사례로 답이 된 판정. 나머지는 사람이 정해야 한다.
GROUNDED = {'GROUNDED', 'GROUNDED_BY_CASES'}


# 카드의 미정 항목을 핸드북으로 먼저 답해 본다.
# 답이 나오면 대표를 기다릴 이유가 없고, 안 나오면 빈 항목으로 남겨 둔다.
# 카드 생성의 부수 작업이라 실패해도 카드는 그대로 둔다.
# 답이 안 채워진 빈칸은 원래대로 사람에게 가므로 실패가 답을 지어내지는 않는다.
def answer_blanks(card):
    for blank in card.blanks.filter(answered_by__isnull=True):
        try:
            result, cited, _, _ = answer_question(
                card.company, blank.question_en, 'en', card.scope
            )
        except (AnswerRateLimited, ImproperlyConfigured, RuntimeError):
            logger.exception('미정 항목 답변 실패 blank=%s', blank.id)
            continue

        if result.verdict not in GROUNDED or not result.answer:
            # 사람이 물은 것이 아니라 지시에서 발견한 구멍이다. 자리만 잡고 횟수는 세지 않는다.
            record_gap(card.company, card.scope, blank.question_en, asked=False)
            continue

        blank.sai_answer_en = result.answer
        blank.answer_citations = [source.payload() for source in cited]
        blank.answered_by = Blank.AnsweredBy.SAI
        blank.save(update_fields=['sai_answer_en', 'answer_citations', 'answered_by'])
