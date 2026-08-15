from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone

from handbook.models import CompanyScope, HandbookEntry

from . import questions
from .models import Question


# 답변에서 만든 규칙임을 표시한다. 다시 답하면 같은 항목을 덮어쓴다.
def _dedupe_key(scope, template_key):
    return f'onboarding:{scope.id}:{template_key}'


def resolve_scope(company, spec, scope=None):
    if spec.area_key is None:
        return scope

    return CompanyScope.objects.filter(
        company=company, kind=CompanyScope.Kind.COMPANY, area_key=spec.area_key
    ).first()


# 고른 선택지를 찾는다. 선택지에 없는 답은 직접 입력으로 본다.
def find_choice(spec, answer):
    for choice in spec.choices:
        if choice.label == answer:
            return choice

    return None


# 선택지를 고른 경우 한국어와 영어 문장이 이미 코드에 있다. 번역도 임베딩 전 처리도 필요 없다.
# 직접 입력한 답만 body_en 이 비어 있고, 확정 시 기존 번역 파이프라인이 채운다.
def build_entry(company, spec, answer, scope):
    choice = find_choice(spec, answer)
    if choice is not None and not choice.creates_rule:
        return None

    target = resolve_scope(company, spec, scope)
    if target is None:
        raise ImproperlyConfigured(f'{spec.key} 의 지식공간을 찾을 수 없습니다')

    body_ko = choice.body_ko if choice else answer
    body_en = choice.body_en if choice else ''
    if not body_ko.strip():
        return None

    entry, _ = HandbookEntry.objects.update_or_create(
        company=company,
        dedupe_key=_dedupe_key(target, spec.key),
        defaults={
            'scope': target,
            'title': spec.title[:200],
            'body_ko': body_ko,
            'body_en': body_en or None,
            'original_lang': 'ko',
            # 대표가 직접 답한 것이라 검토 단계를 두지 않는다.
            'status': HandbookEntry.Status.CONFIRMED,
            'confidence': HandbookEntry.Confidence.HIGH,
            'origin': HandbookEntry.Origin.ONBOARDING,
            'reviewed_at': timezone.now(),
            'confirmed_at': timezone.now(),
            # 문구가 바뀌었을 수 있으니 번역과 임베딩은 다시 만든다.
            'translated_at': timezone.now() if body_en else None,
            'embedding_ko': None,
            'embedding_en': None,
            'embedded_at': None,
        },
    )

    return entry


@transaction.atomic
def answer_question(company, template_key, answer, scope=None):
    spec = questions.find(template_key)
    if spec is None:
        return None

    entry = build_entry(company, spec, answer, scope)
    row, _ = Question.objects.update_or_create(
        company=company,
        scope=scope if spec.area_key is None else None,
        template_key=template_key,
        defaults={
            'status': Question.Status.ANSWERED,
            'answer_ko': answer,
            'created_entry': entry,
            'answered_at': timezone.now(),
        },
    )

    return row


# 넘어간 질문. 앞서 만든 규칙이 있으면 함께 거둔다.
@transaction.atomic
def skip_question(company, template_key, scope=None):
    spec = questions.find(template_key)
    if spec is None:
        return None

    row, _ = Question.objects.update_or_create(
        company=company,
        scope=scope if spec.area_key is None else None,
        template_key=template_key,
        defaults={
            'status': Question.Status.SKIPPED,
            'answer_ko': None,
            'created_entry': None,
            'answered_at': None,
        },
    )
    target = resolve_scope(company, spec, scope)
    if target is not None:
        HandbookEntry.objects.filter(
            company=company, dedupe_key=_dedupe_key(target, spec.key)
        ).delete()

    return row


def list_questions(company, scope=None):
    specs = questions.PROJECT_QUESTIONS if scope else questions.COMPANY_QUESTIONS
    rows = {
        row.template_key: row
        for row in Question.objects.filter(
            company=company, scope=scope if scope else None
        )
    }

    items = []
    for spec in specs:
        row = rows.get(spec.key)
        items.append({
            'templateKey': spec.key,
            'category': spec.category,
            'title': spec.title,
            'question': spec.question,
            'placeholder': spec.placeholder or None,
            'options': [choice.label for choice in spec.choices],
            'status': row.status if row else 'PENDING',
            'answerKo': row.answer_ko if row else None,
            'entryId': row.created_entry_id if row else None,
        })

    return items
