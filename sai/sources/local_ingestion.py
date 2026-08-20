import hashlib

from django.db import transaction
from django.utils import timezone

from .file_extraction import FileExtractionError, extract_file_text, split_file_text
from .classifier import CLASSIFIER_VERSION
from .local_files import LocalFileStorageError, download_file
from .models import IngestionJob, Item, RawDocument


def _content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _save_part(item, external_ref, text):
    content_hash = _content_hash(text)
    document = RawDocument.objects.filter(item=item, external_ref=external_ref).first()

    if document is None:
        RawDocument.objects.create(
            company_id=item.company_id,
            item=item,
            external_ref=external_ref,
            raw_text=text,
            content_hash=content_hash,
            classified_as=RawDocument.ClassifiedAs.INSTRUCTION,
            classifier_version=CLASSIFIER_VERSION,
            sync_state=RawDocument.SyncState.CHANGED,
        )
        return 'created'

    is_changed = document.content_hash != content_hash
    document.raw_text = text
    document.content_hash = content_hash
    document.classified_as = RawDocument.ClassifiedAs.INSTRUCTION
    document.classifier_version = CLASSIFIER_VERSION

    if is_changed:
        document.sync_state = RawDocument.SyncState.CHANGED
        document.card_version = None
    elif document.sync_state in {
        RawDocument.SyncState.CURRENT,
        RawDocument.SyncState.REMOVED,
    }:
        document.sync_state = RawDocument.SyncState.CHANGED

    document.save()

    return 'changed' if is_changed else 'unchanged'


@transaction.atomic
def _save_documents(item, text):
    parts = split_file_text(text)
    seen_refs = set()
    result = {'created': 0, 'changed': 0, 'removed': 0, 'total': len(parts)}

    for index, part in enumerate(parts):
        external_ref = f'file:{item.external_id}:{index}'
        seen_refs.add(external_ref)
        state = _save_part(item, external_ref, part)
        if state in result:
            result[state] += 1

    removed = RawDocument.objects.filter(item=item).exclude(external_ref__in=seen_refs)
    result['removed'] = removed.exclude(sync_state=RawDocument.SyncState.REMOVED).count()
    removed.update(sync_state=RawDocument.SyncState.REMOVED)

    item.item_count = len(parts)
    item.last_synced_at = timezone.now()
    item.save(update_fields=['item_count', 'last_synced_at'])

    return result


def ingest_local_file(item):
    if not item.storage_key or item.byte_size is None:
        raise LocalFileStorageError('file_metadata_missing')

    data = download_file(item.storage_key, item.byte_size)
    text = extract_file_text(item.label, data)

    return _save_documents(item, text)


def run_local_ingestion(job, connection):
    from .ingestion import PROGRESS_COLLECTED, _set_progress, process_documents

    job.status = IngestionJob.Status.RUNNING
    job.started_at = job.started_at or timezone.now()
    job.save(update_fields=['status', 'started_at'])

    items = list(
        Item.objects.filter(
            connection=connection,
            removed_at__isnull=True,
            id__in=job.item_ids or [],
        ).order_by('id')
    )
    errors = []
    if not items:
        return process_documents(
            job,
            [{'scope': 'local', 'code': 'no_file_registered'}],
            collection_failed=True,
        )

    if job.kind == IngestionJob.Kind.COLLECT:
        for index, item in enumerate(items, start=1):
            try:
                ingest_local_file(item)
            except (LocalFileStorageError, FileExtractionError) as exc:
                errors.append({'itemId': item.id, 'label': item.label, 'code': exc.code})

            _set_progress(job, int(PROGRESS_COLLECTED * index / len(items)))

    collection_failed = bool(errors) and len(errors) == len(items)

    draft_documents = None
    if job.kind == IngestionJob.Kind.COLLECT:
        draft_documents = RawDocument.objects.filter(
            company_id=job.company_id,
            item_id__in=[item.id for item in items],
        )

    return process_documents(job, errors, collection_failed, draft_documents=draft_documents)
