import hashlib

from django.db import transaction
from django.utils import timezone

from .file_extraction import extract_file_text
from .local_files import download_file
from .models import RawDocument


def _content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


@transaction.atomic
def _save_document(item, text):
    external_ref = f'file:{item.external_id}'
    content_hash = _content_hash(text)
    document = RawDocument.objects.filter(
        item=item,
        external_ref=external_ref,
    ).first()

    if document is None:
        RawDocument.objects.create(
            company_id=item.company_id,
            item=item,
            external_ref=external_ref,
            raw_text=text,
            content_hash=content_hash,
        )
        state = 'created'
    else:
        is_changed = document.content_hash != content_hash
        document.raw_text = text
        document.content_hash = content_hash

        if is_changed:
            document.sync_state = RawDocument.SyncState.CHANGED
            document.classified_as = RawDocument.ClassifiedAs.UNCLASSIFIED
            document.classifier_version = None
            document.card_version = None
        elif document.sync_state == RawDocument.SyncState.REMOVED:
            document.sync_state = RawDocument.SyncState.CURRENT

        document.save()
        state = 'changed' if is_changed else 'unchanged'

    item.item_count = 1
    item.last_synced_at = timezone.now()
    item.save(update_fields=['item_count', 'last_synced_at'])

    return state


def ingest_local_file(item):
    data = download_file(item.storage_key, item.byte_size)
    text = extract_file_text(item.label, data)

    return _save_document(item, text)
