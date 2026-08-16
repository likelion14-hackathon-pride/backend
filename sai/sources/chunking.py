import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from openai import OpenAI, OpenAIError

from config.ai import client_options, timed_call

from .classifier import build_lookup
from .models import Chunk, RawDocument
from .text import normalize_document_text, redact_secrets

# 임베딩 한 요청에 넣는 텍스트 수.
BATCH_SIZE = 100

HANGUL = re.compile(r'[가-힣]')


def _get_client():
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 임베딩을 실행할 수 없습니다')

    return OpenAI(api_key=settings.OPENAI_API_KEY, **client_options())


def _language(text):
    return 'ko' if HANGUL.search(text) else 'en'


# 현재는 원문 하나를 청크 하나로 저장한다.
# 나중에 긴 파일을 지원하면 여기서 쪼개면 된다.
def build_chunks(company):
    documents = list(
        RawDocument.objects.filter(
            company=company,
            sync_state__in=[
                RawDocument.SyncState.CURRENT,
                RawDocument.SyncState.CHANGED,
            ],
        ).select_related('item__connection', 'item__scope')
    )
    if not documents:
        return []

    channels, users = build_lookup(company.id)
    existing = {
        chunk.document_id: chunk
        for chunk in Chunk.objects.filter(company=company, ord=0)
    }

    touched = []
    for document in documents:
        text, was_redacted = redact_secrets(
            normalize_document_text(document, channels, users)
        )
        if not text:
            continue

        chunk = existing.get(document.id)
        if chunk and chunk.text == text:
            # 내용이 그대로면 임베딩도 유효하다.
            touched.append(chunk)
            continue

        chunk, _ = Chunk.objects.update_or_create(
            document=document,
            ord=0,
            defaults={
                'company_id': company.id,
                'scope': document.item.scope,
                'text': text,
                'lang': _language(text),
                'is_secret_filtered': was_redacted,
                # 본문이 바뀌었으니 기존 벡터는 더 이상 이 텍스트가 아니다.
                'embedding': None,
                'embedded_at': None,
            },
        )
        touched.append(chunk)

    return touched


# 아직 임베딩되지 않았거나 모델이 바뀐 청크를 임베딩한다.
def embed_chunks(company):
    pending = list(
        Chunk.objects.filter(company=company)
        .filter(embedded_at__isnull=True)
        .order_by('id')
    ) + list(
        Chunk.objects.filter(company=company, embedded_at__isnull=False)
        .exclude(embedding_model=settings.OPENAI_EMBEDDING_MODEL)
        .order_by('id')
    )
    if not pending:
        return 0, []

    client = _get_client()
    errors = []
    embedded = 0

    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        try:
            with timed_call(settings.OPENAI_EMBEDDING_MODEL, len(batch)):
                response = client.embeddings.create(
                    model=settings.OPENAI_EMBEDDING_MODEL,
                    input=[chunk.text for chunk in batch],
                )
        except (OpenAIError, ValueError) as exc:
            errors.append({
                'scope': 'embed_chunks',
                'batch': start // BATCH_SIZE,
                'code': type(exc).__name__,
            })
            continue

        now = timezone.now()
        for chunk, item in zip(batch, response.data):
            chunk.embedding = item.embedding
            chunk.embedding_model = settings.OPENAI_EMBEDDING_MODEL
            chunk.embedded_at = now

        Chunk.objects.bulk_update(
            batch, ['embedding', 'embedding_model', 'embedded_at']
        )
        embedded += len(batch)

    return embedded, errors


# 수집 뒤에 호출한다. 청크를 만들고 임베딩까지 맞춘다.
def sync_chunks(company):
    build_chunks(company)

    return embed_chunks(company)
