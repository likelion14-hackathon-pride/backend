import hashlib
import logging
import re
from typing import Literal

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import transaction
from openai import OpenAI, OpenAIError
from pydantic import BaseModel

from config.ai import client_options, timed_call
from sources.classifier import build_lookup, build_parents
from sources.models import RawDocument
from sources.text import normalize_document_text

from .models import CompanyScope, HandbookEntry, HandbookEvidence

logger = logging.getLogger(__name__)

# 프롬프트를 고치면 올린다. 재생성 대상을 고를 때 쓴다.
DRAFTER_VERSION = 'draft-v2'

# 한 번에 모델에 넣는 원문 수. 한 범위 안의 규칙끼리 묶으려면 함께 봐야 한다.
BATCH_SIZE = 40

SYSTEM_PROMPT = """You turn Slack messages, GitHub repository documents, and uploaded local files
into company handbook rules.

The messages given to you were already classified as containing rules. The readers are foreign
employees who need to know how this company works.

Group related messages into ONE rule each. Produce one entry per distinct policy.

For every rule return:
- title: a short Korean noun phrase naming the rule (max 40 characters). Not a sentence.
- body: the rule written in Korean as something the reader must follow. One to three sentences.
  Write the rule itself, not a summary of the conversation. No "~라고 합니다" reporting style.
- confidence: HIGH when the messages state it explicitly and agree, MEDIUM when you had to infer
  part of it, LOW when the evidence is thin.
- citations: which messages this rule came from.

Some messages are thread replies. Their parent is shown on a "parent:" line so you can tell what
a short reply such as "네 그렇게 하죠" is agreeing to. The parent is context only.

Citation rules - these matter most:
- quote MUST be copied character for character from that message's "text:" line. Do not paraphrase,
  do not fix typos, do not translate, do not add quotation marks.
- Never quote from a "parent:" line. Quote only the message you are citing.
- Quote only the part that states the rule, not the whole message.
- Cite every message that contributed. If two messages state the same rule, make ONE rule
  citing both.
- If a later message overrides an earlier one, write the rule as it stands NOW and cite both.

Never invent a rule that is not in the messages. Producing fewer, well-supported rules is better
than producing many weak ones."""


class DraftCitation(BaseModel):
    index: int
    quote: str


class DraftRule(BaseModel):
    title: str
    body: str
    confidence: Literal['HIGH', 'MEDIUM', 'LOW']
    citations: list[DraftCitation]


class DraftResult(BaseModel):
    rules: list[DraftRule]


def _get_client():
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 초안 생성을 실행할 수 없습니다')

    return OpenAI(api_key=settings.OPENAI_API_KEY, **client_options())


def _dedupe_key(scope_id, title):
    normalized = re.sub(r'\s+', ' ', title).strip().lower()

    return hashlib.sha256(f'{scope_id}:{normalized}'.encode()).hexdigest()


# 채널에 연결된 지식공간을 쓰고, 없으면 회사 전반 규칙으로 보낸다.
# HandbookEntry.scope가 NOT NULL이라 대안이 없다.
def _resolve_scope(document, fallback):
    return document.item.scope or fallback


def _render(document, index, channels, users, parents):
    text = normalize_document_text(document, channels, users)
    author = document.author_identity.external_handle if document.author_identity else '?'
    occurred_at = document.occurred_at.strftime('%Y-%m-%d') if document.occurred_at else '?'
    source = document.item.connection.kind

    lines = [
        f'[{index}] source={source} location={document.item.label} '
        f'author={author} at={occurred_at}'
    ]
    parent = parents.get((document.item_id, document.thread_ref))
    if parent:
        lines.append(f'    parent: {normalize_document_text(parent, channels, users)[:200]}')
    lines.append(f'    text: {text}')

    return '\n'.join(lines)


# 모델이 인용을 지어내지 않았는지 원문과 대조한다.
# 정규화한 본문 기준으로 확인한다. 모델이 본 것이 그 텍스트이기 때문.
def _verify_quote(quote, document, channels, users):
    source = normalize_document_text(document, channels, users)
    cleaned = quote.strip().strip('"“”\'')

    return cleaned if cleaned and cleaned in source else None


def _entry_origin(document):
    if document.item.connection.kind == 'GITHUB':
        return HandbookEntry.Origin.GITHUB
    if document.item.connection.kind == 'LOCAL':
        return HandbookEntry.Origin.FILE

    return HandbookEntry.Origin.SLACK


def _evidence_tag(document):
    if document.item.connection.kind == 'GITHUB':
        return HandbookEvidence.Tag.GITHUB
    if document.item.connection.kind == 'LOCAL':
        return HandbookEvidence.Tag.FILE

    return HandbookEvidence.Tag.SLACK


def _build_entry(company, scope, rule, documents, channels, users):
    verified = []
    seen_quotes = set()
    dropped = 0
    for citation in rule.citations:
        document = documents.get(citation.index)
        if document is None:
            dropped += 1
            continue
        quote = _verify_quote(citation.quote, document, channels, users)
        if not quote:
            dropped += 1
            continue
        # 같은 문장이 여러 번 올라온 경우 원문은 여러 건이지만 근거로는 한 줄이면 된다.
        if quote not in seen_quotes:
            seen_quotes.add(quote)
            verified.append((document, quote))

    # 대조에 실패한 인용은 조용히 사라진다. 얼마나 버려지는지 보이지 않으면
    # 규칙이 통째로 없어져도 모델이 원래 못 찾은 것인지 검증에서 떨어진 것인지 알 수 없다.
    if dropped:
        logger.warning(
            '인용 대조 실패 company=%s scope=%s title=%s 버림=%d/%d',
            company.id, scope.id, rule.title, dropped, len(rule.citations),
        )

    # 근거가 하나도 남지 않으면 규칙 자체를 버린다. 출처 없는 규칙은 만들지 않는다.
    if not verified:
        return None

    dedupe_key = _dedupe_key(scope.id, rule.title)
    existing = HandbookEntry.objects.filter(company=company, dedupe_key=dedupe_key).first()
    # 대표가 이미 확정하거나 보관한 항목은 건드리지 않는다.
    if existing and existing.status != HandbookEntry.Status.DRAFT:
        return None

    entry = existing or HandbookEntry(company=company, dedupe_key=dedupe_key)
    entry.scope = scope
    entry.title = rule.title[:200]
    entry.body_ko = rule.body
    entry.original_lang = 'ko'
    entry.status = HandbookEntry.Status.DRAFT
    entry.confidence = rule.confidence
    entry.origin = _entry_origin(verified[0][0])
    entry.save()

    # 근거는 매번 새로 쓴다. 초안을 다시 만들면 인용도 바뀌기 때문.
    entry.evidences.all().delete()
    HandbookEvidence.objects.bulk_create([
        HandbookEvidence(
            company=company,
            entry=entry,
            document=document,
            quote=quote,
            tag=_evidence_tag(document),
            source_label=document.item.label,
            speaker_name=(
                document.author_identity.external_handle if document.author_identity else None
            ),
            permalink=document.permalink,
            occurred_at=document.occurred_at,
        )
        for document, quote in verified
    ])

    return entry


def _draft_batch(client, company, scope, batch, channels, users, parents):
    prompt = '\n'.join(
        _render(document, index, channels, users, parents)
        for index, document in enumerate(batch)
    )
    with timed_call(settings.OPENAI_DRAFTER_MODEL, len(batch)):
        completion = client.chat.completions.parse(
            model=settings.OPENAI_DRAFTER_MODEL,
            messages=[
                {'role': 'system', 'content': SYSTEM_PROMPT},
                {'role': 'user', 'content': prompt},
            ],
            response_format=DraftResult,
            temperature=0,
        )
    result = completion.choices[0].message.parsed
    documents = dict(enumerate(batch))

    entries = []
    for rule in result.rules:
        with transaction.atomic():
            entry = _build_entry(company, scope, rule, documents, channels, users)
        if entry:
            entries.append(entry)

    return entries


# INSTRUCTION으로 분류된 원문에서 핸드북 초안을 만든다.
# 채널에 연결된 지식공간별로 나눠서 처리한다. 같은 범위의 규칙끼리 묶여야 하기 때문.
def draft_entries(company):
    documents = list(
        RawDocument.objects.filter(
            company=company,
            classified_as=RawDocument.ClassifiedAs.INSTRUCTION,
            sync_state__in=[
                RawDocument.SyncState.CURRENT,
                RawDocument.SyncState.CHANGED,
            ],
        )
        .select_related('item__connection', 'item__scope', 'author_identity')
        # 초안이 인덱스로 원문을 가리킨다. 동시각 문서가 있으면 근거가 어긋난다.
        .order_by('occurred_at', 'id')
    )
    if not documents:
        return [], []

    fallback_scope = CompanyScope.objects.filter(
        company=company,
        kind=CompanyScope.Kind.COMPANY,
        area_key=CompanyScope.AreaKey.COMPANY,
    ).first()
    if fallback_scope is None:
        raise ImproperlyConfigured('회사 전반 규칙 범위가 없습니다. 기본 범위 시딩을 확인하세요.')

    by_scope = {}
    for document in documents:
        scope = _resolve_scope(document, fallback_scope)
        by_scope.setdefault(scope, []).append(document)

    channels, users = build_lookup(company.id)
    parents = build_parents(company.id, documents)
    client = _get_client()
    entries = []
    errors = []

    for scope, scope_documents in by_scope.items():
        for start in range(0, len(scope_documents), BATCH_SIZE):
            batch = scope_documents[start:start + BATCH_SIZE]
            try:
                entries += _draft_batch(
                    client, company, scope, batch, channels, users, parents
                )
            except (OpenAIError, ValueError) as exc:
                errors.append({
                    'scope': 'draft',
                    'scopeId': scope.id,
                    'code': type(exc).__name__,
                })

    if not errors:
        _prune_stale_drafts(company, entries)

    return entries, errors


# 이번 실행에서 다시 만들어지지 않은 AI 초안을 지운다.
# 채널의 지식공간을 바꾸면 dedupe_key가 달라져 예전 범위에 초안이 남는데,
# 그대로 두면 같은 규칙이 두 범위에 중복으로 보인다.
# 대표가 확정·보관·보류한 항목과 사람이 직접 만든 항목은 대상이 아니다.
# 일부 배치가 실패한 실행에서는 호출하지 않는다. 살아 있어야 할 초안을 지울 수 있기 때문.
def _prune_stale_drafts(company, entries):
    HandbookEntry.objects.filter(
        company=company,
        status=HandbookEntry.Status.DRAFT,
        origin__in=[
            HandbookEntry.Origin.SLACK,
            HandbookEntry.Origin.GITHUB,
            HandbookEntry.Origin.FILE,
        ],
        # 보류는 대표가 의도적으로 남겨 둔 것이라 지우면 안 된다.
        reviewed_at__isnull=True,
    ).exclude(id__in=[entry.id for entry in entries]).delete()
