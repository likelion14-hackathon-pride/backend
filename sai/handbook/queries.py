from django.db.models import Count, Prefetch, Q

from .models import CompanyScope, HandbookEntry, HandbookEvidence

# 목록의 접힌 행에도 출처가 보인다. 항목마다 근거를 따로 부르면 항목 수만큼 호출이 는다.
# 근거 상세 조회와 같은 순서라 두 화면이 서로 다른 출처를 가리키지 않는다.
SOURCE_PREFETCH = Prefetch(
    'evidences', queryset=HandbookEvidence.objects.order_by('occurred_at', 'id')
)


# review_status 는 status 와 reviewed_at 에서 파생되므로 쿼리로도 같은 규칙을 따른다.
#
# BLANK 는 검토 대상이 아니다. 내용이 없어 승인하면 400 이 나므로,
# 검토 큐에 섞이면 대표는 누를 수 없는 항목을 계속 보게 된다. status=BLANK 로 따로 본다.
def filter_by_review_status(entries, review_status):
    Status = HandbookEntry.Status
    ReviewStatus = HandbookEntry.ReviewStatus
    reviewable = entries.exclude(
        status__in=[Status.CONFIRMED, Status.ARCHIVED, Status.BLANK]
    )

    if review_status == ReviewStatus.APPROVED:
        return entries.filter(status=Status.CONFIRMED)
    if review_status == ReviewStatus.REJECTED:
        return entries.filter(status=Status.ARCHIVED)
    if review_status == ReviewStatus.HELD:
        return reviewable.filter(reviewed_at__isnull=False)

    return reviewable.filter(reviewed_at__isnull=True)


# 대표가 지운 항목을 뺀 것. 목록·상세·답변 검색·집계가 모두 이 기준을 쓴다.
# 한 곳이라도 빠뜨리면 지운 규칙이 그 화면에서만 살아 있는 것처럼 보인다.
def live_entries(company):
    return HandbookEntry.objects.filter(company=company, deleted_at__isnull=True)


def entries_for(company):
    return (
        live_entries(company)
        .select_related('scope')
        .prefetch_related(SOURCE_PREFETCH)
    )


# 지식공간마다 확정 규칙이 몇 개인지. 화면이 공간별로 세면 공간 수만큼 쿼리가 늘어난다.
# 초안과 빈 항목은 세지 않는다. 팀원에게 답으로 나가는 것은 확정된 것뿐이다.
def scopes_with_counts(company):
    return CompanyScope.objects.filter(company=company).annotate(
        entry_count=Count(
            'handbook_entries',
            filter=Q(
                handbook_entries__status=HandbookEntry.Status.CONFIRMED,
                handbook_entries__deleted_at__isnull=True,
            ),
        )
    )
