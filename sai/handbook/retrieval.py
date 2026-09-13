import re

from django.conf import settings
from django.db.models import Q
from pgvector.django import CosineDistance

from .models import HandbookEntry
from .queries import SOURCE_PREFETCH, live_entries

DEFAULT_LIMIT = 5
DEFAULT_MAX_DISTANCE = 0.85
TOKEN = re.compile(r'[0-9A-Za-z가-힣_#.-]+')
STOP_WORDS = {
    'a', 'an', 'and', 'are', 'can', 'do', 'does', 'for', 'how', 'i', 'in',
    'is', 'it', 'of', 'on', 'or', 'the', 'this', 'to', 'we', 'what', 'when',
    'where', 'which', 'with', 'you',
}


def lexical_terms(query, limit=8):
    found = []
    seen = set()
    for token in TOKEN.findall((query or '').lower()):
        if len(token) < 2 or token in STOP_WORDS or token in seen:
            continue
        seen.add(token)
        found.append(token)
        if len(found) >= limit:
            break

    return found


def lexical_score(query, text):
    terms = lexical_terms(query)
    if not terms:
        return 0.0
    haystack = ' '.join(TOKEN.findall((text or '').lower()))
    hits = sum(term in haystack for term in terms)
    score = hits / len(terms)
    normalized_query = ' '.join(TOKEN.findall((query or '').lower()))
    if normalized_query and normalized_query in haystack:
        score = min(1.0, score + 0.15)

    return score


def _lexical_candidates(entries, query, limit):
    terms = lexical_terms(query)
    if not terms:
        return []

    filters = Q()
    for term in terms:
        filters |= (
            Q(title__icontains=term)
            | Q(title_en__icontains=term)
            | Q(body_ko__icontains=term)
            | Q(body_en__icontains=term)
        )

    # 확정만 되고 임베딩 확정이 실패한 규칙은 기존에도 검색 대상이 아니었다. Hybrid
    # 도 그 안전 경계를 우회하지 않고, 벡터가 있는 규칙의 순위 보정에만 사용한다.
    searchable = Q(embedding_ko__isnull=False) | Q(embedding_en__isnull=False)
    return list(
        entries.filter(searchable).filter(filters).distinct().order_by('id')[:limit]
    )


# 한국어/영어 임베딩을 모두 뒤져 항목별로 더 가까운 쪽을 쓴다.
# 영어 질문이 한국어로만 쓰인 규칙을 찾을 수 있어야 하기 때문.
def search_rules(vector, company, scope_ids=None, limit=DEFAULT_LIMIT,
                 max_distance=DEFAULT_MAX_DISTANCE, query=None):
    entries = live_entries(company).filter(
        status=HandbookEntry.Status.CONFIRMED
    ).select_related('scope').prefetch_related(SOURCE_PREFETCH)
    if scope_ids is not None:
        entries = entries.filter(scope_id__in=scope_ids)

    use_hybrid = bool(query and settings.HANDBOOK_HYBRID_RETRIEVAL_ENABLED)
    multiplier = max(1, settings.HANDBOOK_RETRIEVAL_CANDIDATE_MULTIPLIER)
    candidate_limit = limit * multiplier if use_hybrid else limit
    best = {}
    for field in ('embedding_ko', 'embedding_en'):
        rows = (
            entries.filter(**{f'{field}__isnull': False})
            .annotate(distance=CosineDistance(field, vector))
            .filter(distance__lte=max_distance)
            .order_by('distance')[:candidate_limit]
        )
        for entry in rows:
            if entry.id not in best or entry.distance < best[entry.id].distance:
                best[entry.id] = entry

    if not use_hybrid:
        return sorted(best.values(), key=lambda entry: entry.distance)[:limit]

    for entry in _lexical_candidates(entries, query, candidate_limit):
        if entry.id not in best:
            # lexical-only 후보도 기존 Source 직렬화 계약을 지키도록 distance를 둔다.
            entry.distance = max_distance
            best[entry.id] = entry

    vector_weight = max(0.0, min(1.0, settings.HANDBOOK_RETRIEVAL_VECTOR_WEIGHT))
    for entry in best.values():
        semantic = max(0.0, 1.0 - float(entry.distance))
        text = ' '.join(filter(None, [
            entry.title, entry.title_en, entry.body_ko, entry.body_en,
        ]))
        lexical = lexical_score(query, text)
        entry.retrieval_score = (
            vector_weight * semantic + (1.0 - vector_weight) * lexical
        )

    return sorted(
        best.values(),
        key=lambda entry: (-entry.retrieval_score, float(entry.distance), entry.id),
    )[:limit]
