import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.utils import timezone
from openai import OpenAI, OpenAIError
from pydantic import BaseModel

from config.ai import client_options, generation_options, record_usage, timed_call

from .classifier import build_lookup
from .models import Chunk, Connection, RawDocument
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


def _estimated_tokens(text):
    # tokenizer 의존성을 추가하지 않고 관측용 크기만 남긴다. 한국어/영어 혼합 문서에서
    # 정확한 과금 token으로 쓰지 않으며 SmallInteger 범위를 넘지 않게 제한한다.
    return min(32767, max(1, (len(text) + 3) // 4))


def _split_long_text(text):
    max_chars = max(400, settings.SOURCE_LONG_DOCUMENT_CHUNK_CHARS)
    overlap = max(
        0,
        min(settings.SOURCE_LONG_DOCUMENT_CHUNK_OVERLAP_CHARS, max_chars // 3),
    )
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        hard_end = min(start + max_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            # 문단 > 줄 > 문장 > 공백 순으로 가능한 뒤쪽 경계를 고른다. 너무 앞에서
            # 자르면 작은 청크가 쌓이므로 윈도우의 절반 이후만 탐색한다.
            search_from = start + max_chars // 2
            boundaries = [
                text.rfind(marker, search_from, hard_end)
                for marker in ('\n\n', '\n', '. ', '다. ', '요. ', ' ')
            ]
            boundary = max(boundaries)
            if boundary >= search_from:
                end = boundary + 1

        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break

        next_start = max(start + 1, end - overlap)
        # overlap의 시작이 단어 한가운데면 다음 공백까지 이동한다.
        while (
            next_start < end
            and next_start > 0
            and not text[next_start - 1].isspace()
        ):
            next_start += 1
        start = next_start if next_start < end else end

    return chunks


def _document_chunks(document, text):
    # Slack 한 메시지를 자르면 작성자/스레드 맥락이 분리된다. 긴 구조화 문서만 나눈다.
    if document.item.connection.kind not in (
        Connection.Kind.GITHUB,
        Connection.Kind.LOCAL,
    ):
        return [text]

    return _split_long_text(text)


# 짧은 원문과 Slack 메시지는 기존처럼 ord=0 한 건이다. 긴 GitHub/업로드 문서만 문단
# 경계를 우선해 여러 청크로 나누며, 재수집 시 사라진 뒷 청크도 함께 정리한다.
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
    channels, users = build_lookup(company.id)
    existing = {
        (chunk.document_id, chunk.ord): chunk
        for chunk in Chunk.objects.filter(company=company)
    }

    touched = []
    active_keys = set()
    for document in documents:
        text, was_redacted = redact_secrets(
            normalize_document_text(document, channels, users)
        )
        if not text:
            continue

        for ord_, piece in enumerate(_document_chunks(document, text)):
            key = (document.id, ord_)
            active_keys.add(key)
            chunk = existing.get(key)
            if chunk and chunk.text == piece:
                # 내용이 그대로면 번역/벡터를 유지한다. Scope 같은 검색 metadata만
                # 바뀐 경우 외부 호출 없이 갱신한다.
                changed = []
                values = {
                    'scope_id': document.item.scope_id,
                    'lang': _language(piece),
                    'token_count': _estimated_tokens(piece),
                    'is_secret_filtered': was_redacted,
                }
                for field, value in values.items():
                    if getattr(chunk, field) != value:
                        setattr(chunk, field, value)
                        changed.append(field)
                if changed:
                    chunk.save(update_fields=changed)
                touched.append(chunk)
                continue

            chunk, _ = Chunk.objects.update_or_create(
                document=document,
                ord=ord_,
                defaults={
                    'company_id': company.id,
                    'scope': document.item.scope,
                    'text': piece,
                    'lang': _language(piece),
                    'token_count': _estimated_tokens(piece),
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

    stale_ids = [chunk.id for key, chunk in existing.items() if key not in active_keys]
    if stale_ids:
        Chunk.objects.filter(id__in=stale_ids).delete()

    return touched


def untranslated_chunks(company):
    return Chunk.objects.filter(
        company=company, lang='ko', translated_at__isnull=True
    ).exclude(text='')


# 한국어 원문의 영어판을 채운다. 영어로 쓰인 청크는 원문이 곧 영어라 번역하지 않는다.
#
# 벡터는 읽지 않는다. 1536차원 하나가 6KB라 수천 건이면 그것만으로 수십 MB가 되는데,
# 번역에는 원문 텍스트 말고 쓸 것이 없다.
def translate_chunks(company):
    pending = list(untranslated_chunks(company).only('id', 'text').order_by('id'))
    if not pending:
        return 0, []

    by_text = {}
    for chunk in pending:
        by_text.setdefault(chunk.text, []).append(chunk)
    errors = []
    translated = []

    # 이전 수집에서 같은 본문을 번역했다면 DB 결과를 재사용한다. 원문이 여러 채널이나
    # 저장소에 복사된 경우 worker 실행이 달라도 다시 과금되지 않는다.
    cached = dict(
        Chunk.objects.filter(
            company=company,
            text__in=by_text,
            text_en__isnull=False,
        ).exclude(text_en='').values_list('text', 'text_en')
    )
    now = timezone.now()
    for source_text, translated_text in cached.items():
        for chunk in by_text.pop(source_text, []):
            chunk.text_en = translated_text
            chunk.translated_at = now
            translated.append(chunk)

    unique_texts = list(by_text)
    client = _get_client() if unique_texts else None

    for start in range(0, len(unique_texts), TRANSLATE_BATCH_SIZE):
        batch = unique_texts[start:start + TRANSLATE_BATCH_SIZE]
        prompt = '\n\n'.join(
            f'[{index}]\n{text}' for index, text in enumerate(batch)
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
                    **generation_options(
                        settings.OPENAI_TRANSLATOR_MODEL,
                        reasoning_effort=settings.OPENAI_TRANSLATOR_REASONING_EFFORT,
                        verbosity=settings.OPENAI_TRANSLATOR_VERBOSITY,
                    ),
                )
            record_usage(
                'chunk_translation',
                settings.OPENAI_TRANSLATOR_MODEL,
                completion,
                len(batch),
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
        for index, source_text in enumerate(batch):
            text = by_index.get(index)
            if not text:
                continue
            # 같은 문장이 여러 소스에 복사돼도 번역 호출은 한 번만 하고 결과를 공유한다.
            for chunk in by_text[source_text]:
                chunk.text_en = text
                chunk.translated_at = now
                translated.append(chunk)

    Chunk.objects.bulk_update(
        translated, ['text_en', 'translated_at'], batch_size=TRANSLATE_BATCH_SIZE * 10
    )

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

    # 이미 같은 모델로 만든 동일 텍스트 벡터를 같은 회사 안에서 재사용한다. 벡터는
    # 모델과 텍스트만의 함수라 문서가 달라도 결과가 같다.
    target_texts = {text for _, _, text in targets}
    reusable = {}
    candidates = Chunk.objects.filter(
        company=company,
        embedding_model=settings.OPENAI_EMBEDDING_MODEL,
    ).filter(
        Q(text__in=target_texts, embedding__isnull=False)
        | Q(text_en__in=target_texts, embedding_en__isnull=False)
    ).only('text', 'text_en', 'embedding', 'embedding_en')
    for candidate in candidates:
        if candidate.embedding is not None:
            reusable[candidate.text] = candidate.embedding
        if candidate.text_en and candidate.embedding_en is not None:
            reusable[candidate.text_en] = candidate.embedding_en

    embedded = set()
    cached_touched = {}
    remaining = []
    cached_at = timezone.now()
    for chunk, field, text in targets:
        vector = reusable.get(text)
        if vector is None:
            remaining.append((chunk, field, text))
            continue
        setattr(chunk, field, vector)
        chunk.embedding_model = settings.OPENAI_EMBEDDING_MODEL
        chunk.embedded_at = cached_at
        cached_touched[chunk.id] = chunk
    if cached_touched:
        Chunk.objects.bulk_update(
            cached_touched.values(),
            ['embedding', 'embedding_en', 'embedding_model', 'embedded_at'],
            batch_size=BATCH_SIZE,
        )
        embedded.update(cached_touched)
    if not remaining:
        return len(embedded), []

    by_text = {}
    for chunk, field, text in remaining:
        by_text.setdefault(text, []).append((chunk, field))
    unique_targets = list(by_text.items())

    client = _get_client()
    errors = []

    for start in range(0, len(unique_targets), BATCH_SIZE):
        batch = unique_targets[start:start + BATCH_SIZE]
        try:
            with timed_call(settings.OPENAI_EMBEDDING_MODEL, len(batch)):
                response = client.embeddings.create(
                    model=settings.OPENAI_EMBEDDING_MODEL,
                    input=[text for text, _ in batch],
                )
            record_usage(
                'chunk_embedding',
                settings.OPENAI_EMBEDDING_MODEL,
                response,
                len(batch),
            )
        except (OpenAIError, ValueError) as exc:
            errors.append({
                'scope': 'embed_chunks',
                'batch': start // BATCH_SIZE,
                'code': type(exc).__name__,
            })
            continue

        now = timezone.now()
        # 청크 하나가 한국어와 영어 두 목표로 갈라져 같은 배치에 두 번 들어올 수 있다.
        # 이 배치에서 손댄 것만 쓴다. 누적분을 매번 넘기면 쓰기가 배치 수의 제곱으로 는다.
        touched = {}
        for (text, text_targets), item in zip(batch, response.data):
            # 원문이 같은 청크와 한 청크의 동일한 양언어 본문은 같은 벡터를 재사용한다.
            for chunk, field in text_targets:
                setattr(chunk, field, item.embedding)
                chunk.embedding_model = settings.OPENAI_EMBEDDING_MODEL
                chunk.embedded_at = now
                touched[chunk.id] = chunk

        Chunk.objects.bulk_update(
            touched.values(),
            ['embedding', 'embedding_en', 'embedding_model', 'embedded_at'],
            batch_size=BATCH_SIZE,
        )
        embedded.update(touched)

    return len(embedded), errors


# 수집 뒤에 호출한다. 청크를 만들고 번역과 임베딩까지 맞춘다.
def sync_chunks(company):
    build_chunks(company)
    _, translate_errors = translate_chunks(company)
    embedded, embed_errors = embed_chunks(company)

    return embedded, translate_errors + embed_errors
