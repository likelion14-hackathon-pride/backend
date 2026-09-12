import re
from dataclasses import dataclass, field
from datetime import timedelta
from difflib import SequenceMatcher

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from django.utils import timezone
from openai import OpenAI, OpenAIError

from config.ai import client_options, timed_call
from policy.models import RiskKeyword
from sources.models import RawDocument

from . import finalizing
from .finalizing import mark_confirmed
from .models import CompanyScope, HandbookEntry
from .retrieval import search_rules
from .services import scopes_in_view


@dataclass
class PromotionEvaluation:
    promotion_type: str
    method: str | None = None
    codes: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    evidence_count: int = 0
    risk_keywords: list[dict] = field(default_factory=list)
    conflict_detected: bool = False
    similar_entry_id: int | None = None
    similarity_score: float | None = None


def _normalise(text):
    return re.sub(r'[^0-9a-zA-Z가-힣]+', ' ', text or '').strip().lower()


def _contains_term(haystack, term):
    # 영어 약어(HR 등)는 단어 경계를 지키고, 한국어는 조사와 붙어 쓰이므로 부분 일치를 쓴다.
    if re.fullmatch(r'[0-9a-z ]+', term):
        return re.search(rf'(?<![0-9a-z]){re.escape(term)}(?![0-9a-z])', haystack) is not None
    return term in haystack


def _risk_keywords(entry):
    haystack = _normalise(' '.join(filter(None, [entry.title, entry.body_ko, entry.body_en])))
    found = []
    seen = set()

    for keyword in RiskKeyword.objects.filter(company=entry.company):
        for word in [keyword.word, *(keyword.aliases or [])]:
            normalised = _normalise(word)
            if normalised and _contains_term(haystack, normalised):
                key = ('COMPANY_CONFIGURED', keyword.id, normalised)
                if key not in seen:
                    seen.add(key)
                    found.append({
                        'keyword': keyword.word,
                        'matched': word,
                        'level': keyword.level,
                        'category': 'COMPANY_CONFIGURED',
                    })
                break

    for category, words in settings.HANDBOOK_HIGH_RISK_KEYWORDS.items():
        for word in words:
            normalised = _normalise(word)
            if normalised and _contains_term(haystack, normalised):
                key = ('SYSTEM', category, normalised)
                if key not in seen:
                    seen.add(key)
                    found.append({
                        'keyword': word,
                        'matched': word,
                        'level': 'DANGER',
                        'category': category,
                    })

    return found


def _evidence_snapshot(entry):
    evidences = list(
        entry.evidences.select_related('document__item__connection').order_by('id')
    )
    document_ids = {evidence.document_id for evidence in evidences if evidence.document_id}
    evidence_count = len(document_ids) or len(evidences)
    recent_cutoff = timezone.now() - timedelta(
        days=settings.HANDBOOK_AUTO_PROMOTION_RECENT_DAYS
    )
    has_recent = any(
        evidence.occurred_at is not None and evidence.occurred_at >= recent_cutoff
        for evidence in evidences
    )

    invalid = []
    for evidence in evidences:
        if not (evidence.quote or '').strip():
            invalid.append({'evidenceId': evidence.id, 'reason': 'empty_quote'})
        if evidence.tag == evidence.Tag.OWNER:
            continue
        document = evidence.document
        if document is None:
            invalid.append({'evidenceId': evidence.id, 'reason': 'source_missing'})
            continue
        item = document.item
        if document.sync_state not in {
            RawDocument.SyncState.CURRENT,
            RawDocument.SyncState.CHANGED,
        }:
            invalid.append({'evidenceId': evidence.id, 'reason': 'source_removed'})
        if item.removed_at is not None or item.connection.disconnected_at is not None:
            invalid.append({'evidenceId': evidence.id, 'reason': 'source_inactive'})

    return evidences, evidence_count, has_recent, invalid


def _scope_is_confirmed(entry, evidences, *, explicit_owner_scope=False):
    if entry.scope_id is None or entry.scope.company_id != entry.company_id:
        return False
    if explicit_owner_scope:
        return True

    source_evidences = [evidence for evidence in evidences if evidence.document_id]
    return bool(source_evidences) and all(
        evidence.document.item.is_scope_confirmed
        and evidence.document.item.scope_id == entry.scope_id
        for evidence in source_evidences
    )


def _body_similarity(left, right):
    left = _normalise(left)
    right = _normalise(right)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _embed_for_similarity(text):
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 유사 규칙을 확인할 수 없습니다')
    client = OpenAI(api_key=settings.OPENAI_API_KEY, **client_options())
    with timed_call(settings.OPENAI_EMBEDDING_MODEL):
        return client.embeddings.create(
            model=settings.OPENAI_EMBEDDING_MODEL,
            input=[text],
        ).data[0].embedding


def _similarity_result(entry):
    scope_ids = scopes_in_view(entry.company, entry.scope)
    confirmed = HandbookEntry.objects.filter(
        company=entry.company,
        scope_id__in=scope_ids,
        status=HandbookEntry.Status.CONFIRMED,
        deleted_at__isnull=True,
    ).exclude(id=entry.id)
    if not confirmed.exists():
        return {'kind': 'NONE'}

    candidate_body = entry.body_ko or entry.body_en or ''
    for existing in confirmed.only('id', 'body_ko', 'body_en', 'title'):
        existing_body = existing.body_ko or existing.body_en or ''
        ratio = max(
            _body_similarity(candidate_body, existing_body),
            _body_similarity(entry.title, existing.title),
        )
        if ratio >= settings.HANDBOOK_PROMOTION_DUPLICATE_TEXT_RATIO:
            return {
                'kind': 'DUPLICATE',
                'entry_id': existing.id,
                'score': ratio,
            }

    # 검색 불가능한 확정 규칙이 하나라도 있으면 전체 충돌 검사를 했다고 볼 수 없다.
    if confirmed.filter(embedding_ko__isnull=True, embedding_en__isnull=True).exists():
        return {'kind': 'UNAVAILABLE'}

    vector = _embed_for_similarity(candidate_body)
    similar = search_rules(
        vector,
        entry.company,
        scope_ids=scope_ids,
        limit=1,
        max_distance=settings.HANDBOOK_PROMOTION_SIMILAR_MAX_DISTANCE,
    )
    if not similar:
        return {'kind': 'NONE'}

    existing = similar[0]
    score = max(0.0, min(1.0, 1.0 - float(existing.distance)))
    ratio = _body_similarity(candidate_body, existing.body_ko or existing.body_en)
    return {
        'kind': (
            'DUPLICATE'
            if ratio >= settings.HANDBOOK_PROMOTION_DUPLICATE_TEXT_RATIO
            else 'CONFLICT'
        ),
        'entry_id': existing.id,
        'score': score,
    }


def evaluate_entry(entry, *, method=None, explicit_owner_scope=False, owner_verified=False,
                   answer_needs_review=False):
    evidences, evidence_count, has_recent, invalid_sources = _evidence_snapshot(entry)
    risks = _risk_keywords(entry)
    scope_confirmed = _scope_is_confirmed(
        entry, evidences, explicit_owner_scope=explicit_owner_scope
    )
    signals = {
        'scopeConfirmed': scope_confirmed,
        'hasRecentEvidence': has_recent,
        'minimumEvidence': settings.HANDBOOK_AUTO_PROMOTION_MIN_EVIDENCE,
        'recentDays': settings.HANDBOOK_AUTO_PROMOTION_RECENT_DAYS,
        'invalidSources': invalid_sources,
        'ownerVerified': owner_verified,
        'answerNeedsReview': answer_needs_review,
        'similarityChecked': False,
    }

    def result(promotion_type, *codes, **extra):
        return PromotionEvaluation(
            promotion_type=promotion_type,
            method=method,
            codes=list(codes),
            signals=signals,
            evidence_count=evidence_count,
            risk_keywords=risks,
            **extra,
        )

    if entry.reviewed_at is not None and entry.status == HandbookEntry.Status.DRAFT:
        return result(HandbookEntry.PromotionType.PENDING_REVIEW, 'already_reviewed_by_owner')
    if not (entry.body_ko or entry.body_en or '').strip():
        return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'empty_rule')
    if not evidences:
        return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'evidence_missing')
    if invalid_sources:
        return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'source_invalid')
    if not scope_confirmed:
        return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'scope_unconfirmed')
    if risks:
        return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'risk_keyword_detected')
    # 반복 관찰만으로 회사 공통 규칙을 확정하지 않는다. 다만 명시적 Owner 결정은
    # 요구사항상 Company/Project Scope 모두 가능하며, 고위험 조건은 위에서 이미 차단한다.
    if (
        entry.scope.kind == CompanyScope.Kind.COMPANY
        and method != HandbookEntry.AutoPromotionMethod.OWNER_DECISION
    ):
        return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'company_wide_rule')
    if method == HandbookEntry.AutoPromotionMethod.OWNER_DECISION:
        if not owner_verified:
            return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'owner_not_verified')
        if answer_needs_review:
            return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'answer_needs_review')
    else:
        if entry.confidence != HandbookEntry.Confidence.HIGH:
            return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'ai_inference_possible')
        if evidence_count < settings.HANDBOOK_AUTO_PROMOTION_MIN_EVIDENCE:
            return result(HandbookEntry.PromotionType.PENDING_REVIEW, 'insufficient_evidence')
        if not has_recent:
            return result(HandbookEntry.PromotionType.PENDING_REVIEW, 'no_recent_evidence')

    try:
        similar = _similarity_result(entry)
    except (ImproperlyConfigured, OpenAIError, ValueError, RuntimeError):
        signals['similarityStatus'] = 'FAILED'
        return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'similarity_check_failed')

    signals['similarityChecked'] = similar['kind'] != 'UNAVAILABLE'
    signals['similarityStatus'] = similar['kind']
    if similar['kind'] == 'UNAVAILABLE':
        return result(HandbookEntry.PromotionType.MANUAL_REQUIRED, 'similarity_check_unavailable')
    if similar['kind'] == 'DUPLICATE':
        return result(
            HandbookEntry.PromotionType.MANUAL_REQUIRED,
            'duplicate_candidate',
            similar_entry_id=similar['entry_id'],
            similarity_score=similar['score'],
        )
    if similar['kind'] == 'CONFLICT':
        return result(
            HandbookEntry.PromotionType.MANUAL_REQUIRED,
            'possible_conflict',
            conflict_detected=True,
            similar_entry_id=similar['entry_id'],
            similarity_score=similar['score'],
        )

    return result(HandbookEntry.PromotionType.AUTO_PROMOTED, 'policy_conditions_satisfied')


def _save_evaluation(entry, evaluation):
    entry.promotion_type = evaluation.promotion_type
    entry.auto_promotion_method = (
        evaluation.method
        if evaluation.promotion_type == HandbookEntry.PromotionType.AUTO_PROMOTED
        else None
    )
    entry.promotion_reason = {
        'codes': evaluation.codes,
        'signals': evaluation.signals,
    }
    entry.evidence_count = evaluation.evidence_count
    entry.detected_risk_keywords = evaluation.risk_keywords
    entry.conflict_detected = evaluation.conflict_detected
    entry.similar_entry_id = evaluation.similar_entry_id
    entry.similarity_score = evaluation.similarity_score
    entry.promotion_policy_version = settings.HANDBOOK_PROMOTION_POLICY_VERSION


def evaluate_and_promote(entry, *, method=None, explicit_owner_scope=False,
                         owner_verified=False, answer_needs_review=False):
    evaluation = evaluate_entry(
        entry,
        method=method,
        explicit_owner_scope=explicit_owner_scope,
        owner_verified=owner_verified,
        answer_needs_review=answer_needs_review,
    )
    now = timezone.now()
    with transaction.atomic():
        locked = HandbookEntry.objects.select_for_update().get(id=entry.id)
        # Worker 재시도나 동시에 들어온 확인 요청이 이미 승격했다면 다시 확정하지 않는다.
        if locked.is_auto_promoted and locked.status == HandbookEntry.Status.CONFIRMED:
            return locked, {'translated': 0, 'embedded': 0, 'errors': []}

        _save_evaluation(locked, evaluation)
        fields = [
            'promotion_type', 'auto_promotion_method', 'promotion_reason',
            'evidence_count', 'detected_risk_keywords', 'conflict_detected',
            'similar_entry', 'similarity_score', 'promotion_policy_version',
        ]
        if evaluation.promotion_type == HandbookEntry.PromotionType.AUTO_PROMOTED:
            mark_confirmed(locked, at=now)
            locked.auto_promoted_at = now
            fields += ['auto_promoted_at']
        locked.save(update_fields=fields)

    finalization = {'translated': 0, 'embedded': 0, 'errors': []}
    if evaluation.promotion_type == HandbookEntry.PromotionType.AUTO_PROMOTED:
        # 수동 승인과 같은 번역/임베딩/RAG 반영 함수를 사용한다.
        finalization = finalizing.finalize_entries([locked])

    return locked, finalization
