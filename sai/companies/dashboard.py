from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db.models import Count
from django.utils import timezone

from handbook.models import HandbookEntry
from handbook.queries import live_entries
from qna.models import Citation, Escalation, Message


SAI_VERDICTS = {
    Message.Verdict.GROUNDED,
    Message.Verdict.GROUNDED_BY_CASES,
}
OWNER_VERDICTS = {
    Message.Verdict.NO_SOURCE,
    Message.Verdict.NEEDS_DECISION,
}
OWNER_RESOLVED_STATUSES = {
    Escalation.Status.ANSWERED,
    Escalation.Status.APPROVED,
}
OWNER_WAITING_STATUSES = {
    Escalation.Status.DRAFT,
    Escalation.Status.SENT,
}

ANSWER_SAVED_MINUTES = 5
DASHBOARD_LIST_LIMIT = 4


def _company_now(company):
    return timezone.now().astimezone(ZoneInfo(company.timezone))


def _week_start(value):
    return (value - timedelta(days=value.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _month_start(value, offset=0):
    index = value.year * 12 + value.month - 1 + offset
    year, month_index = divmod(index, 12)

    return value.replace(
        year=year,
        month=month_index + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


def _question_for(message):
    if message is None:
        return ''

    question = (
        Message.objects.filter(
            thread_id=message.thread_id,
            role=Message.Role.USER,
            created_at__lt=message.created_at,
        )
        .order_by('-created_at')
        .first()
    )
    if question is None:
        return ''

    return question.body_ko or question.body_en or ''


def _source_labels(message):
    labels = []
    for citation in message.citations.all():
        if citation.entry:
            label = citation.entry.title
        elif citation.chunk:
            label = citation.chunk.document.item.label
        else:
            continue

        if label not in labels:
            labels.append(label)

    return labels


def _weekly_questions(company, week_start, week_end):
    messages = Message.objects.filter(
        company=company,
        role=Message.Role.AI,
        created_at__gte=week_start,
        created_at__lt=week_end,
        verdict__isnull=False,
    ).exclude(verdict=Message.Verdict.OUT_OF_SCOPE)

    return {
        'totalCount': messages.count(),
        'saiAnsweredCount': messages.filter(verdict__in=SAI_VERDICTS).count(),
        'ownerRequiredCount': messages.filter(verdict__in=OWNER_VERDICTS).count(),
    }


def _resolution(company, week_start, week_end, sai_count):
    owner_count = Escalation.objects.filter(
        company=company,
        status__in=OWNER_RESOLVED_STATUSES,
        answered_at__gte=week_start,
        answered_at__lt=week_end,
    ).count()
    total = sai_count + owner_count

    return {
        'saiCount': sai_count,
        'ownerCount': owner_count,
        'saiRate': round(sai_count / total * 100, 1) if total else 0,
        'ownerRate': round(owner_count / total * 100, 1) if total else 0,
    }


def _answer_reuse(company):
    # 지운 규칙은 세지 않는다. 인용 행은 남겨 두므로 entry 를 타고 그대로 딸려 오는데,
    # 방금 지운 규칙이 '많이 쓰인 규칙' 1위로 뜨면 삭제가 안 먹힌 것처럼 보인다.
    citations = Citation.objects.filter(
        company=company,
        entry__isnull=False,
        entry__deleted_at__isnull=True,
        message__role=Message.Role.AI,
    )
    total = citations.count()
    entry_count = citations.values('entry_id').distinct().count()
    top_entries = (
        citations.values('entry_id', 'entry__title')
        .annotate(reuse_count=Count('id'))
        .order_by('-reuse_count', 'entry__title')[:3]
    )

    return {
        'averageCount': round(total / entry_count, 1) if entry_count else 0,
        'totalCount': total,
        'topEntries': [
            {
                'entryId': row['entry_id'],
                'title': row['entry__title'],
                'reuseCount': row['reuse_count'],
            }
            for row in top_entries
        ],
    }


def _owner_time_saved(company, current_week):
    weekly_trend = []
    for offset in range(5, -1, -1):
        start = current_week - timedelta(weeks=offset)
        end = start + timedelta(weeks=1)
        answer_count = Message.objects.filter(
            company=company,
            role=Message.Role.AI,
            verdict__in=SAI_VERDICTS,
            created_at__gte=start,
            created_at__lt=end,
        ).count()
        weekly_trend.append({
            'weekStart': start.date(),
            'minutes': answer_count * ANSWER_SAVED_MINUTES,
        })

    current_minutes = weekly_trend[-1]['minutes']
    previous_minutes = weekly_trend[-2]['minutes']

    return {
        'minutes': current_minutes,
        'changeMinutes': current_minutes - previous_minutes,
        'minutesPerAnswer': ANSWER_SAVED_MINUTES,
        'weeklyTrend': weekly_trend,
    }


def _handbook_summary(company, now, week_start, week_end):
    entries = live_entries(company).filter(status=HandbookEntry.Status.CONFIRMED)
    monthly_trend = []
    first_month = _month_start(now, -5)
    for offset in range(6):
        start = _month_start(first_month, offset)
        end = _month_start(first_month, offset + 1)
        monthly_trend.append({
            'month': start.strftime('%Y-%m'),
            'count': entries.filter(confirmed_at__lt=end).count(),
        })

    return {
        'totalCount': entries.count(),
        'thisWeekCount': entries.filter(
            confirmed_at__gte=week_start,
            confirmed_at__lt=week_end,
        ).count(),
        'lastConfirmedAt': entries.filter(confirmed_at__isnull=False)
        .order_by('-confirmed_at')
        .values_list('confirmed_at', flat=True)
        .first(),
        'monthlyTrend': monthly_trend,
    }


def _recent_answers(company, today_start, today_end):
    sai_messages = (
        Message.objects.filter(
            company=company,
            role=Message.Role.AI,
            verdict__in=SAI_VERDICTS,
            created_at__gte=today_start,
            created_at__lt=today_end,
        )
        .prefetch_related(
            'citations__entry',
            'citations__chunk__document__item',
        )
        .order_by('-created_at')
    )
    owner_answers = (
        Escalation.objects.filter(
            company=company,
            status__in=OWNER_RESOLVED_STATUSES,
            answered_at__gte=today_start,
            answered_at__lt=today_end,
        )
        .select_related('origin_message')
        .order_by('-answered_at')
    )

    items = [
        {
            'question': _question_for(message),
            'answer': message.body_ko or message.body_en or '',
            'resolutionType': 'SAI',
            'sourceLabels': _source_labels(message),
            'resolvedAt': message.created_at,
        }
        for message in sai_messages[:DASHBOARD_LIST_LIMIT]
    ]
    items.extend([
        {
            'question': _question_for(escalation.origin_message)
            or escalation.question_en
            or '',
            'answer': escalation.answer_ko or escalation.answer_en or '',
            'resolutionType': 'OWNER',
            'sourceLabels': ['대표 확인 답변'],
            'resolvedAt': escalation.answered_at,
        }
        for escalation in owner_answers[:DASHBOARD_LIST_LIMIT]
    ])
    items.sort(key=lambda item: item['resolvedAt'], reverse=True)

    return {
        'todayCount': sai_messages.count() + owner_answers.count(),
        'items': items[:DASHBOARD_LIST_LIMIT],
    }


def _waiting_questions(company):
    questions = (
        Escalation.objects.filter(
            company=company,
            status__in=OWNER_WAITING_STATUSES,
        )
        .select_related('asked_by', 'scope')
        .order_by('-created_at')
    )

    return {
        'totalCount': questions.count(),
        'items': [
            {
                'id': question.id,
                'question': question.draft_ko
                or question.question_en
                or '',
                'askedByName': question.asked_by.display_name,
                'scopeName': question.scope.name if question.scope else None,
                'status': question.status,
                'createdAt': question.created_at,
            }
            for question in questions[:DASHBOARD_LIST_LIMIT]
        ],
    }


def dashboard_data(company):
    now = _company_now(company)
    current_week = _week_start(now)
    next_week = current_week + timedelta(weeks=1)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    weekly_questions = _weekly_questions(company, current_week, next_week)

    return {
        'weeklyQuestions': weekly_questions,
        'resolution': _resolution(
            company,
            current_week,
            next_week,
            weekly_questions['saiAnsweredCount'],
        ),
        'answerReuse': _answer_reuse(company),
        'ownerTimeSaved': _owner_time_saved(company, current_week),
        'handbook': _handbook_summary(
            company,
            now,
            current_week,
            next_week,
        ),
        'recentAnswers': _recent_answers(company, today_start, today_end),
        'waitingQuestions': _waiting_questions(company),
    }
