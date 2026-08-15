from .models import HandbookEntry


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
