from django.db.models import Count, Max, Q

from handbook.models import HandbookEntry
from handbook.queries import live_entries

from .models import Connection, Item, RawDocument

# 소스 종류마다 거기서 만들어진 핸드북 항목의 origin 이 다르다.
SOURCE_ORIGINS = {
    Connection.Kind.GITHUB: HandbookEntry.Origin.GITHUB,
    Connection.Kind.SLACK: HandbookEntry.Origin.SLACK,
    Connection.Kind.LOCAL: HandbookEntry.Origin.FILE,
}

# 아직 아무도 열어 보지 않은 지시. read_at 은 카드마다 하나라 회사 기준이다.
# 메시지 단위 읽음은 저장하는 곳이 없어서 '안 읽은 지시 수'로 센다.
_UNREAD = Q(
    documents__instruction_cards__read_at__isnull=True,
    documents__instruction_cards__duplicate_of__isnull=True,
)


# 팀원이 읽는 채널 목록. 대표가 수집 대상을 관리하는 목록과 달리
# 연결에 묶이지 않고 회사의 살아 있는 채널만 본다.
def channels_for(company):
    return (
        Item.objects.filter(
            company=company,
            connection__kind=Connection.Kind.SLACK,
            connection__disconnected_at__isnull=True,
            removed_at__isnull=True,
        )
        .select_related('scope')
        .annotate(
            last_message_at=Max('documents__occurred_at'),
            unread_count=Count('documents__instruction_cards', filter=_UNREAD, distinct=True),
        )
    )


def messages_in(item):
    return (
        RawDocument.objects.filter(item=item)
        .select_related('author_identity')
        # 카드가 있으면 지시다. 문서당 한 장이라 목록 전체에 쿼리 한 번이면 된다.
        .prefetch_related('instruction_cards')
    )


def connections_for(company):
    return (
        Connection.objects.filter(company=company, disconnected_at__isnull=True)
        .annotate(
            resource_count=Count(
                'items', filter=Q(items__removed_at__isnull=True), distinct=True
            ),
            last_synced=Max('items__last_synced_at'),
        )
    )


# 소스에서 만들어진 핸드북 항목 수. 대표가 거절한 것은 빼고 센다.
# 연결마다 세면 소스 수만큼 쿼리가 늘어나므로 한 번에 세어 종류별로 나눈다.
def extracted_counts(company):
    rows = (
        live_entries(company)
        .exclude(status=HandbookEntry.Status.ARCHIVED)
        .values('origin')
        .annotate(total=Count('id'))
    )
    by_origin = {row['origin']: row['total'] for row in rows}

    return {kind: by_origin.get(origin, 0) for kind, origin in SOURCE_ORIGINS.items()}
