from accounts.models import Membership

from .models import Escalation, Message


# 대표는 회사 전체를, 팀원은 본인이 올린 것만 본다.
def escalations_for(company, user):
    queryset = Escalation.objects.filter(company=company).select_related(
        'asked_by', 'scope', 'origin_message', 'origin_message__thread__user'
    ).prefetch_related(
        'card_blanks__card__assignee',
        'card_blanks__card__document__author_identity__user',
    )
    is_owner = Membership.objects.filter(
        user=user, company=company, role=Membership.Role.OWNER, left_at__isnull=True
    ).exists()

    return queryset if is_owner else queryset.filter(asked_by=user)


def messages_in(thread):
    return (
        Message.objects.filter(thread=thread)
        .prefetch_related('citations__entry__scope')
        .order_by('id')
    )
