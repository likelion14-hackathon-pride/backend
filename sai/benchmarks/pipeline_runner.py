import hashlib
import os
import subprocess
import sys
import time
from contextlib import nullcontext
from datetime import datetime, timezone


OWNER_VERDICTS = {'NO_SOURCE', 'NEEDS_DECISION'}
PIPELINE_PROFILES = {'llm-only', 'dense-only', 'hybrid', 'current'}
PROFILED_TASKS = {'retrieval', 'qna'}


def _django_setup():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django

    django.setup()


def _model_id(prefix, value):
    return f'{prefix}:{value}'


def _document_ids(entry):
    evidences = getattr(entry, 'evidences', None)
    if evidences is None:
        return []
    return [
        _model_id('document', evidence.document_id)
        for evidence in evidences.all()
        if evidence.document_id is not None
    ]


def _unique(values):
    return list(dict.fromkeys(values))


def _ranked_document_ids(entries, chunks):
    values = []
    for entry in entries:
        values.extend(_document_ids(entry))
    values.extend(
        _model_id('document', chunk.document_id)
        for chunk in chunks
        if getattr(chunk, 'document_id', None) is not None
    )
    return _unique(values)


def _profile_context(profile):
    if profile not in PIPELINE_PROFILES:
        raise ValueError(f'unknown pipeline profile: {profile}')
    if profile not in {'dense-only', 'hybrid'}:
        return nullcontext()

    from django.test.utils import override_settings

    return override_settings(
        HANDBOOK_HYBRID_RETRIEVAL_ENABLED=(profile == 'hybrid')
    )


def _serialize_entry(entry):
    return {
        'entryId': entry.id,
        'title': entry.title,
        'body': entry.body_ko or entry.body_en or '',
        'scopeId': entry.scope_id,
        'confidence': entry.confidence,
        'evidenceIds': _document_ids(entry),
    }


def _serialize_card(card):
    return {
        'cardId': card.id,
        'purpose': card.purpose or '',
        'deliverable': card.deliverable or '',
        'deadlineText': card.deadline_text or '',
        'isDeadlineInferred': card.is_deadline_inferred,
        'urgency': card.urgency,
        'steps': list(card.steps.order_by('ord').values_list('text', flat=True)),
        'blanks': list(card.blanks.order_by('id').values_list('question_en', flat=True)),
        'assigneeId': card.assignee_id,
        'duplicateOfId': card.duplicate_of_id,
    }


def _classification(inputs):
    from sources.classifier import (
        _classify_batch, _get_client, build_lookup, build_parents,
    )
    from sources.models import RawDocument

    document = RawDocument.objects.select_related(
        'item__connection', 'author_identity'
    ).get(id=inputs['documentId'])
    channels, users = build_lookup(document.company_id)
    parents = build_parents(document.company_id, [document])
    labels = _classify_batch(_get_client(), [document], channels, users, parents)
    return {'label': labels.get(0)}


def _drafting(inputs):
    from django.db import transaction
    from companies.models import Company
    from handbook.drafting import (
        _draft_batch, _get_client, build_lookup, build_parents,
    )
    from handbook.models import CompanyScope
    from sources.models import RawDocument

    company = Company.objects.get(id=inputs['companyId'])
    scope = CompanyScope.objects.get(id=inputs['scopeId'], company=company)
    documents_by_id = {
        document.id: document
        for document in RawDocument.objects.filter(
            company=company, id__in=inputs['documentIds']
        ).select_related('item__connection', 'item__scope', 'author_identity')
    }
    documents = [documents_by_id[value] for value in inputs['documentIds']]
    channels, users = build_lookup(company.id)
    parents = build_parents(company.id, documents)

    # The production builder writes entries/evidence. Keep the exact behavior but
    # roll the entire case back after serializing its output.
    with transaction.atomic():
        entries, errors = _draft_batch(
            _get_client(), company, scope, documents, channels, users, parents
        )
        output = {
            'rules': [_serialize_entry(entry) for entry in entries],
            'pipelineErrors': errors,
        }
        transaction.set_rollback(True)
    return output


def _promotion(inputs):
    from handbook.models import HandbookEntry
    from handbook.promotion import evaluate_entry

    entry = HandbookEntry.objects.get(id=inputs['entryId'])
    evaluation = evaluate_entry(
        entry,
        method=inputs.get('method'),
        explicit_owner_scope=inputs.get('explicitOwnerScope', False),
        owner_verified=inputs.get('ownerVerified', False),
        answer_needs_review=inputs.get('answerNeedsReview', False),
    )
    return {
        'promotionType': evaluation.promotion_type,
        'reasonCodes': evaluation.codes,
    }


def _company_and_scope(inputs):
    from companies.models import Company
    from handbook.models import CompanyScope

    company = Company.objects.get(id=inputs['companyId'])
    scope_id = inputs.get('scopeId')
    scope = (
        CompanyScope.objects.get(id=scope_id, company=company)
        if scope_id is not None else None
    )
    return company, scope


def _retrieval(inputs, profile='current'):
    from qna.answering import _get_client, embed_question, retrieve

    if profile == 'llm-only':
        return {'rankedIds': [], 'rankedDocumentIds': []}

    company, scope = _company_and_scope(inputs)
    vector = embed_question(_get_client(), inputs['query'])
    with _profile_context(profile):
        entries, chunks = retrieve(vector, company, scope, query=inputs['query'])
    ranked = [_model_id('handbook', entry.id) for entry in entries]
    ranked += [_model_id('chunk', chunk.id) for chunk in chunks]
    return {
        'rankedIds': ranked,
        'rankedDocumentIds': _ranked_document_ids(entries, chunks),
    }


def _source_id(source):
    if source.entry:
        return _model_id('handbook', source.entry.id)
    return _model_id('chunk', source.chunk.id)


def _source_document_ids(source):
    if source.entry:
        return _document_ids(source.entry)
    document_id = getattr(source.chunk, 'document_id', None)
    return [_model_id('document', document_id)] if document_id is not None else []


def _llm_only_qna(inputs, company, scope):
    from django.conf import settings
    from qna.answering import _ask, _get_client, _tokens

    started = time.time()
    completion = _ask(
        _get_client(),
        inputs['question'],
        inputs.get('lang', 'en'),
        [],
        [],
        scope,
    )
    result = completion.choices[0].message.parsed
    usage = {
        'model': settings.OPENAI_ANSWER_MODEL,
        'promptTokens': _tokens(completion, 'prompt_tokens'),
        'completionTokens': _tokens(completion, 'completion_tokens'),
        'latencyMs': int((time.time() - started) * 1000),
    }
    return result, [], [], usage


def _qna(inputs, profile='current'):
    from qna.answering import answer_question

    company, scope = _company_and_scope(inputs)
    if profile == 'llm-only':
        result, cited, retrieval, usage = _llm_only_qna(inputs, company, scope)
    else:
        with _profile_context(profile):
            result, cited, retrieval, usage = answer_question(
                company, inputs['question'], inputs.get('lang', 'en'), scope
            )
    citation_document_ids = _unique(
        document_id
        for source in cited
        for document_id in _source_document_ids(source)
    )
    output = {
        'verdict': result.verdict,
        'escalated': result.verdict in OWNER_VERDICTS,
        'answer': result.answer,
        'citationIds': [_source_id(source) for source in cited],
        'citationDocumentIds': citation_document_ids,
        'retrieval': retrieval,
    }
    telemetry = {
        key: value for key, value in {
            'latencyMs': usage.get('latencyMs'),
            'promptTokens': usage.get('promptTokens'),
            'completionTokens': usage.get('completionTokens'),
            'model': usage.get('model'),
        }.items() if value is not None
    }
    return output, telemetry


def _cards(inputs):
    from django.db import transaction
    from cards.generation import (
        _build_card, _get_client, _judge_batch, build_lookup,
    )
    from sources.models import RawDocument

    document = RawDocument.objects.select_related(
        'company', 'item__connection', 'item__scope', 'author_identity'
    ).get(id=inputs['documentId'])
    company = document.company
    channels, users = build_lookup(company.id)
    client = _get_client()
    decided = _judge_batch(client, [document], channels, users)
    is_instruction = bool(decided.get(0))
    if not is_instruction:
        return {'isInstruction': False, 'card': None}

    with transaction.atomic():
        card = _build_card(client, company, document, channels, users)
        output = {
            'isInstruction': True,
            'card': _serialize_card(card) if card is not None else None,
        }
        transaction.set_rollback(True)
    return output


RUNNERS = {
    'classification': _classification,
    'drafting': _drafting,
    'promotion': _promotion,
    'retrieval': _retrieval,
    'qna': _qna,
    'cards': _cards,
}


def current_predictions(dataset, tasks=None, profile='current'):
    _django_setup()
    if profile not in PIPELINE_PROFILES:
        raise ValueError(f'unknown pipeline profile: {profile}')
    selected = set(tasks or RUNNERS)
    rows = []
    for case in dataset:
        if case['task'] not in selected:
            continue
        started = time.perf_counter()
        telemetry = {}
        try:
            runner = RUNNERS[case['task']]
            result = (
                runner(case['input'], profile=profile)
                if case['task'] in PROFILED_TASKS
                else runner(case['input'])
            )
            if isinstance(result, tuple):
                output, telemetry = result
            else:
                output = result
            row = {'caseId': case['caseId'], 'output': output}
        except Exception as exc:  # Preserve failures and continue the benchmark run.
            row = {'caseId': case['caseId'], 'error': f'{type(exc).__name__}: {exc}'}
        telemetry.setdefault(
            'latencyMs', round((time.perf_counter() - started) * 1000, 3)
        )
        row['telemetry'] = telemetry
        rows.append(row)
    return rows


def environment_manifest(dataset_path=None, profile='current'):
    from django.conf import settings
    from cards.generation import GENERATOR_VERSION
    from handbook.drafting import DRAFTER_VERSION
    from sources.classifier import CLASSIFIER_VERSION

    try:
        commit = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    dataset_sha256 = None
    if dataset_path is not None:
        digest = hashlib.sha256()
        with open(dataset_path, 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        dataset_sha256 = digest.hexdigest()

    return {
        'schemaVersion': 1,
        'createdAt': datetime.now(timezone.utc).isoformat(),
        'gitCommit': commit,
        'datasetSha256': dataset_sha256,
        'pipelineProfile': profile,
        'python': sys.version,
        'models': {
            'classifier': settings.OPENAI_CLASSIFIER_MODEL,
            'drafter': settings.OPENAI_DRAFTER_MODEL,
            'translator': settings.OPENAI_TRANSLATOR_MODEL,
            'answer': settings.OPENAI_ANSWER_MODEL,
            'embedding': settings.OPENAI_EMBEDDING_MODEL,
        },
        'generationConfiguration': {
            'classifier': {
                'reasoningEffort': settings.OPENAI_CLASSIFIER_REASONING_EFFORT,
                'verbosity': settings.OPENAI_CLASSIFIER_VERBOSITY,
            },
            'classifierFallback': {
                'model': settings.OPENAI_CLASSIFIER_FALLBACK_MODEL,
                'reasoningEffort': (
                    settings.OPENAI_CLASSIFIER_FALLBACK_REASONING_EFFORT
                ),
            },
            'drafter': {
                'reasoningEffort': settings.OPENAI_DRAFTER_REASONING_EFFORT,
                'verbosity': settings.OPENAI_DRAFTER_VERBOSITY,
            },
            'translator': {
                'reasoningEffort': settings.OPENAI_TRANSLATOR_REASONING_EFFORT,
                'verbosity': settings.OPENAI_TRANSLATOR_VERBOSITY,
            },
            'answer': {
                'reasoningEffort': settings.OPENAI_ANSWER_REASONING_EFFORT,
                'verbosity': settings.OPENAI_ANSWER_VERBOSITY,
            },
            'timeoutSeconds': settings.OPENAI_TIMEOUT,
            'maxRetries': settings.OPENAI_MAX_RETRIES,
        },
        'chunking': {
            'maxChars': settings.SOURCE_LONG_DOCUMENT_CHUNK_CHARS,
            'overlapChars': settings.SOURCE_LONG_DOCUMENT_CHUNK_OVERLAP_CHARS,
        },
        'promotionPolicyVersion': getattr(
            settings, 'HANDBOOK_PROMOTION_POLICY_VERSION', None
        ),
        'retrieval': {
            'hybridEnabled': (
                False if profile in {'llm-only', 'dense-only'}
                else True if profile == 'hybrid'
                else settings.HANDBOOK_HYBRID_RETRIEVAL_ENABLED
            ),
            'chunkStrategy': (
                'none' if profile == 'llm-only' else 'database-indexed'
            ),
            'candidateMultiplier': getattr(
                settings, 'HANDBOOK_RETRIEVAL_CANDIDATE_MULTIPLIER', None
            ),
            'vectorWeight': getattr(
                settings, 'HANDBOOK_RETRIEVAL_VECTOR_WEIGHT', None
            ),
        },
        'promptVersions': {
            'classifier': CLASSIFIER_VERSION,
            'drafter': DRAFTER_VERSION,
            'cards': GENERATOR_VERSION,
        },
    }
