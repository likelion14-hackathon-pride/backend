from pgvector.django import CosineDistance

from .models import HandbookEntry

DEFAULT_LIMIT = 5
DEFAULT_MAX_DISTANCE = 0.85


# 한국어/영어 임베딩을 모두 뒤져 항목별로 더 가까운 쪽을 쓴다.
# 영어 질문이 한국어로만 쓰인 규칙을 찾을 수 있어야 하기 때문.
def search_rules(vector, company, scope_ids=None, limit=DEFAULT_LIMIT,
                 max_distance=DEFAULT_MAX_DISTANCE):
    entries = HandbookEntry.objects.filter(
        company=company, status=HandbookEntry.Status.CONFIRMED
    ).select_related('scope')
    if scope_ids is not None:
        entries = entries.filter(scope_id__in=scope_ids)

    best = {}
    for field in ('embedding_ko', 'embedding_en'):
        rows = (
            entries.filter(**{f'{field}__isnull': False})
            .annotate(distance=CosineDistance(field, vector))
            .filter(distance__lte=max_distance)
            .order_by('distance')[:limit]
        )
        for entry in rows:
            if entry.id not in best or entry.distance < best[entry.id].distance:
                best[entry.id] = entry

    return sorted(best.values(), key=lambda entry: entry.distance)[:limit]
