import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from openai import OpenAI, OpenAIError
from pydantic import BaseModel

from config.ai import client_options, timed_call

from .classifier import build_lookup
from .models import Chunk, RawDocument
from .text import normalize_document_text, redact_secrets

# 임베딩 한 요청에 넣는 텍스트 수.
BATCH_SIZE = 100

# 번역은 출력 토큰이 붙으므로 임베딩보다 잘게 묶는다. handbook.finalizing 과 같은 크기.
TRANSLATE_BATCH_SIZE = 20

HANGUL = re.compile(r'[가-힣]')

TRANSLATE_PROMPT = """You translate internal Slack messages, repository documents, and uploaded
file excerpts from a Korean startup into English.

These translations are never shown to anyone. They exist so that an English question can find the
Korean message that answers it. Carry the same meaning; nothing else matters.

- Keep the same length and structure. Do not summarise, explain, or add context.
- Leave untouched: channel names (#dev), @handles, tool and product names, file names, code,
  URLs, numbers, and times.
- Plain workplace English.
- A fragment stays a fragment. Do not turn it into a sentence.
- Return one translation per index, in the same order you received them."""


class ChunkTranslation(BaseModel):
    index: int
    text: str


class ChunkTranslationResult(BaseModel):
    translations: list[ChunkTranslation]


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
                # 본문이 바뀌었으니 기존 번역과 벡터는 더 이상 이 텍스트가 아니다.
                'text_en': None,
                'translated_at': None,
                'embedding': None,
                'embedding_en': None,
                'embedded_at': None,
            },
        )
        touched.append(chunk)

    return touched


def untranslated_chunks(company):
    return Chunk.objects.filter(
        company=company, lang='ko', translated_at__isnull=True
    ).exclude(text='')


# 한국어 원문의 영어판을 채운다. 영어로 쓰인 청크는 원문이 곧 영어라 번역하지 않는다.
def translate_chunks(company):
    pending = list(untranslated_chunks(company).order_by('id'))
    if not pending:
        return 0, []

    client = _get_client()
    errors = []
    translated = []

    for start in range(0, len(pending), TRANSLATE_BATCH_SIZE):
        batch = pending[start:start + TRANSLATE_BATCH_SIZE]
        prompt = '\n\n'.join(
            f'[{index}]\n{chunk.text}' for index, chunk in enumerate(batch)
        )
        try:
            with timed_call(settings.OPENAI_TRANSLATOR_MODEL, len(batch)):
                completion = client.chat.completions.parse(
                    model=settings.OPENAI_TRANSLATOR_MODEL,
                    messages=[
                        {'role': 'system', 'content': TRANSLATE_PROMPT},
                        {'role': 'user', 'content': prompt},
                    ],
                    response_format=ChunkTranslationResult,
                    temperature=0,
                )
        except (OpenAIError, ValueError) as exc:
            errors.append({
                'scope': 'translate_chunks',
                'batch': start // TRANSLATE_BATCH_SIZE,
                'code': type(exc).__name__,
            })
            continue

        by_index = {
            item.index: item.text
            for item in completion.choices[0].message.parsed.translations
        }
        now = timezone.now()
        for index, chunk in enumerate(batch):
            text = by_index.get(index)
            if not text:
                continue
            chunk.text_en = text
            chunk.translated_at = now
            translated.append(chunk)

    Chunk.objects.bulk_update(translated, ['text_en', 'translated_at'])

    return len(translated), errors


def _stale_model(chunk):
    return chunk.embedding_model != settings.OPENAI_EMBEDDING_MODEL


# 벡터가 비었거나 임베딩 모델이 바뀐 청크. 번역이 뒤늦게 붙은 것도 여기 걸린다.
def _pending_chunks(company):
    chunks = Chunk.objects.filter(company=company)
    found = (
        list(chunks.filter(embedded_at__isnull=True))
        + list(chunks.filter(text_en__isnull=False, embedding_en__isnull=True))
        + list(
            chunks.filter(embedded_at__isnull=False).exclude(
                embedding_model=settings.OPENAI_EMBEDDING_MODEL
            )
        )
    )
    unique = {chunk.id: chunk for chunk in found}

    return sorted(unique.values(), key=lambda chunk: chunk.id)


# 한국어 본문과 영어 본문을 각각 임베딩한다. 두 벡터가 같은 차원이라
# 영어 질문이 한국어 원문을 찾을 수 있다.
def embed_chunks(company):
    targets = []
    for chunk in _pending_chunks(company):
        if chunk.embedding is None or _stale_model(chunk):
            targets.append((chunk, 'embedding', chunk.text))
        if chunk.text_en and (chunk.embedding_en is None or _stale_model(chunk)):
            targets.append((chunk, 'embedding_en', chunk.text_en))
    if not targets:
        return 0, []

    client = _get_client()
    errors = []
    embedded = set()

    for start in range(0, len(targets), BATCH_SIZE):
        batch = targets[start:start + BATCH_SIZE]
        try:
            with timed_call(settings.OPENAI_EMBEDDING_MODEL, len(batch)):
                response = client.embeddings.create(
                    model=settings.OPENAI_EMBEDDING_MODEL,
                    input=[text for _, _, text in batch],
                )
        except (OpenAIError, ValueError) as exc:
            errors.append({
                'scope': 'embed_chunks',
                'batch': start // BATCH_SIZE,
                'code': type(exc).__name__,
            })
            continue

        now = timezone.now()
        for (chunk, field, _), item in zip(batch, response.data):
            setattr(chunk, field, item.embedding)
            chunk.embedding_model = settings.OPENAI_EMBEDDING_MODEL
            chunk.embedded_at = now
            embedded.add(chunk)

        Chunk.objects.bulk_update(
            embedded, ['embedding', 'embedding_en', 'embedding_model', 'embedded_at']
        )

    return len(embedded), errors


# 수집 뒤에 호출한다. 청크를 만들고 번역과 임베딩까지 맞춘다.
def sync_chunks(company):
    build_chunks(company)
    _, translate_errors = translate_chunks(company)
    embedded, embed_errors = embed_chunks(company)

    return embedded, translate_errors + embed_errors
