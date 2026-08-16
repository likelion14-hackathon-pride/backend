from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone
from openai import OpenAI, OpenAIError
from pydantic import BaseModel

from config.ai import client_options, timed_call

from .models import HandbookEntry

# 번역과 임베딩을 한 번에 묶는 크기. 일괄 승인에서 항목 수만큼 호출하지 않기 위함.
BATCH_SIZE = 20

TRANSLATE_PROMPT = """You translate company handbook rules between Korean and English.

The readers are foreign employees at a Korean startup. They must be able to follow the rule
without knowing Korean.

- Translate the rule itself. Keep it as an instruction the reader must follow.
- Keep it the same length and structure. Do not add explanation, do not summarise.
- Leave these untouched: channel names (#dev), tool and product names, file names,
  code, URLs, numbers, times, and weekday names' meaning.
- Use plain workplace English. No honorific padding, no "please be advised".
- Return one translation per index, in the same order you received them."""


class Translation(BaseModel):
    index: int
    text: str


class TranslationResult(BaseModel):
    translations: list[Translation]


def _get_client():
    if not settings.OPENAI_API_KEY:
        raise ImproperlyConfigured('OPENAI_API_KEY 설정이 없어 번역/임베딩을 실행할 수 없습니다')

    return OpenAI(api_key=settings.OPENAI_API_KEY, **client_options())


def _source_body(entry):
    return entry.body_ko if entry.original_lang == 'ko' else entry.body_en


def _needs_translation(entry):
    target = entry.body_en if entry.original_lang == 'ko' else entry.body_ko

    return bool(_source_body(entry)) and not target


# 원문 언어의 반대쪽 본문을 채운다.
def _translate(client, entries):
    pending = [entry for entry in entries if _needs_translation(entry)]
    if not pending:
        return []

    translated = []
    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        prompt = '\n\n'.join(
            f'[{index}] from={entry.original_lang}\n{_source_body(entry)}'
            for index, entry in enumerate(batch)
        )
        with timed_call(settings.OPENAI_TRANSLATOR_MODEL, len(batch)):
            completion = client.chat.completions.parse(
                model=settings.OPENAI_TRANSLATOR_MODEL,
                messages=[
                    {'role': 'system', 'content': TRANSLATE_PROMPT},
                    {'role': 'user', 'content': prompt},
                ],
                response_format=TranslationResult,
                temperature=0,
            )
        by_index = {t.index: t.text for t in completion.choices[0].message.parsed.translations}

        for index, entry in enumerate(batch):
            text = by_index.get(index)
            if not text:
                continue
            if entry.original_lang == 'ko':
                entry.body_en = text
            else:
                entry.body_ko = text
            entry.translated_at = timezone.now()
            translated.append(entry)

    HandbookEntry.objects.bulk_update(translated, ['body_ko', 'body_en', 'translated_at'])

    return translated


# 한국어/영어 본문을 각각 임베딩한다.
# 두 벡터가 같은 차원이어야 한 쿼리로 양쪽을 검색할 수 있다.
def _embed(client, entries):
    targets = []
    for entry in entries:
        if entry.body_ko:
            targets.append((entry, 'ko', entry.body_ko))
        if entry.body_en:
            targets.append((entry, 'en', entry.body_en))
    if not targets:
        return []

    embedded = set()
    for start in range(0, len(targets), BATCH_SIZE):
        batch = targets[start:start + BATCH_SIZE]
        with timed_call(settings.OPENAI_EMBEDDING_MODEL, len(batch)):
            response = client.embeddings.create(
                model=settings.OPENAI_EMBEDDING_MODEL,
                input=[text for _, _, text in batch],
            )
        for (entry, lang, _), item in zip(batch, response.data):
            if lang == 'ko':
                entry.embedding_ko = item.embedding
            else:
                entry.embedding_en = item.embedding
            entry.embedding_model = settings.OPENAI_EMBEDDING_MODEL
            entry.embedded_at = timezone.now()
            embedded.add(entry)

    HandbookEntry.objects.bulk_update(
        embedded, ['embedding_ko', 'embedding_en', 'embedding_model', 'embedded_at']
    )

    return list(embedded)


# 확정된 항목을 검색 가능한 상태로 만든다. 번역 -> 임베딩 순서로 돈다.
# 실패해도 예외를 올리지 않는다. 확정은 사람의 결정이고, OpenAI 장애로 되돌릴 일이 아니다.
# 실패하면 translated_at / embedded_at 이 비어 있으므로 나중에 다시 부르면 이어서 처리된다.
def finalize_entries(entries):
    entries = [entry for entry in entries if entry.status == HandbookEntry.Status.CONFIRMED]
    if not entries:
        return {'translated': 0, 'embedded': 0, 'errors': []}

    try:
        client = _get_client()
    except ImproperlyConfigured:
        return {'translated': 0, 'embedded': 0, 'errors': [{'code': 'openai_not_configured'}]}

    errors = []
    translated = []
    try:
        translated = _translate(client, entries)
    except (OpenAIError, ValueError) as exc:
        errors.append({'step': 'translate', 'code': type(exc).__name__})

    embedded = []
    try:
        embedded = _embed(client, entries)
    except (OpenAIError, ValueError) as exc:
        errors.append({'step': 'embed', 'code': type(exc).__name__})

    return {'translated': len(translated), 'embedded': len(embedded), 'errors': errors}


# 본문이 바뀌면 번역과 임베딩이 낡는다. 다시 만들도록 표시를 지운다.
def mark_stale(entry):
    entry.body_en = None if entry.original_lang == 'ko' else entry.body_en
    entry.body_ko = None if entry.original_lang == 'en' else entry.body_ko
    entry.translated_at = None
    entry.embedding_ko = None
    entry.embedding_en = None
    entry.embedded_at = None
