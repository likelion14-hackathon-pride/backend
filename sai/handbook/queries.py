from django.db.models import Count, Q

from .models import CompanyScope, HandbookEntry


# review_status 는 status 와 reviewed_at 에서 파생되므로 쿼리로도 같은 규칙을 따른다.
def filter_by_review_status(entries, review_status):
    Status = HandbookEntry.Status
    ReviewStatus = HandbookEntry.ReviewStatus
    reviewable = entries.exclude(status__in=[Status.CONFIRMED, Status.ARCHIVED])

    if review_status == ReviewStatus.APPROVED:
        return entries.filter(status=Status.CONFIRMED)
    if review_status == ReviewStatus.REJECTED:
        return entries.filter(status=Status.ARCHIVED)
    if review_status == ReviewStatus.HELD:
        return reviewable.filter(reviewed_at__isnull=False)

    return reviewable.filter(reviewed_at__isnull=True)


def entries_for(company):
    return HandbookEntry.objects.filter(company=company).select_related('scope')


# 지식공간마다 확정 규칙이 몇 개인지. 화면이 공간별로 세면 공간 수만큼 쿼리가 늘어난다.
# 초안과 빈 항목은 세지 않는다. 팀원에게 답으로 나가는 것은 확정된 것뿐이다.
def scopes_with_counts(company):
    return CompanyScope.objects.filter(company=company).annotate(
        entry_count=Count(
            'handbook_entries',
            filter=Q(handbook_entries__status=HandbookEntry.Status.CONFIRMED),
        )
    )
