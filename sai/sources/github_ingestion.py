import hashlib
from datetime import datetime

from django.db import transaction
from django.utils import timezone

from .github import GitHubError
from .models import Identity, IngestionJob, Item, RawDocument
from .services import github_client


INTERNAL_ASSOCIATIONS = {'OWNER', 'MEMBER', 'COLLABORATOR'}


def _content_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _occurred_at(value):
    if not value:
        return None

    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _identity(connection, source):
    user = source.get('user') or {}
    external_id = user.get('id')
    if external_id is None:
        return None

    identity, _ = Identity.objects.update_or_create(
        connection=connection,
        external_user_id=str(external_id),
        defaults={
            'company_id': connection.company_id,
            'external_handle': user.get('login'),
            'is_internal': source.get('author_association') in INTERNAL_ASSOCIATIONS,
            'is_bot': user.get('type') == 'Bot',
        },
    )

    return identity


def _issue_text(issue):
    return '\n\n'.join(
        value.strip()
        for value in [issue.get('title') or '', issue.get('body') or '']
        if value.strip()
    )


def _readme_record(readme):
    if not readme or not readme['text'].strip():
        return None

    return {
        'external_ref': f'readme:{readme["path"]}',
        'thread_ref': None,
        'occurred_at': None,
        'permalink': readme.get('html_url'),
        'raw_text': readme['text'].strip(),
        'source': {},
    }


def _issue_record(issue):
    text = _issue_text(issue)
    if not text:
        return None

    return {
        'external_ref': f'issue:{issue["number"]}',
        'thread_ref': None,
        'occurred_at': _occurred_at(issue.get('created_at')),
        'permalink': issue.get('html_url'),
        'raw_text': text,
        'source': issue,
    }


def _pull_record(pull):
    text = _issue_text(pull)
    if not text:
        return None

    return {
        'external_ref': f'pull:{pull["number"]}',
        'thread_ref': None,
        'occurred_at': _occurred_at(pull.get('created_at')),
        'permalink': pull.get('html_url'),
        'raw_text': text,
        'source': pull,
    }


def _comment_record(comment, pull_numbers):
    text = (comment.get('body') or '').strip()
    if not text:
        return None

    number = int(comment['issue_url'].rstrip('/').rsplit('/', 1)[-1])
    parent = f'pull:{number}' if number in pull_numbers else f'issue:{number}'

    return {
        'external_ref': f'issue-comment:{comment["id"]}',
        'thread_ref': parent,
        'occurred_at': _occurred_at(comment.get('created_at')),
        'permalink': comment.get('html_url'),
        'raw_text': text,
        'source': comment,
    }


def _review_comment_record(comment):
    text = (comment.get('body') or '').strip()
    if not text:
        return None

    number = int(comment['pull_request_url'].rstrip('/').rsplit('/', 1)[-1])

    return {
        'external_ref': f'pull-review-comment:{comment["id"]}',
        'thread_ref': f'pull:{number}',
        'occurred_at': _occurred_at(comment.get('created_at')),
        'permalink': comment.get('html_url'),
        'raw_text': text,
        'source': comment,
    }


def _repository_records(repository, client):
    readme = client.readme(repository)
    issues = client.issues(repository)
    comments = client.issue_comments(repository)
    pulls = client.pull_requests(repository)
    review_comments = client.pull_review_comments(repository)
    pull_numbers = {pull['number'] for pull in pulls}

    records = [_readme_record(readme)]
    records += [_issue_record(issue) for issue in issues]
    records += [_pull_record(pull) for pull in pulls]
    records += [_comment_record(comment, pull_numbers) for comment in comments]
    records += [_review_comment_record(comment) for comment in review_comments]

    return [record for record in records if record is not None]


def _save_record(item, record):
    content_hash = _content_hash(record['raw_text'])
    document = RawDocument.objects.filter(
        item=item,
        external_ref=record['external_ref'],
    ).first()

    if document is None:
        RawDocument.objects.create(
            company_id=item.company_id,
            item=item,
            external_ref=record['external_ref'],
            thread_ref=record['thread_ref'],
            author_identity=_identity(item.connection, record['source']),
            occurred_at=record['occurred_at'],
            permalink=record['permalink'],
            raw_text=record['raw_text'],
            content_hash=content_hash,
        )
        return 'created'

    is_changed = document.content_hash != content_hash
    document.thread_ref = record['thread_ref']
    document.author_identity = _identity(item.connection, record['source'])
    document.occurred_at = record['occurred_at']
    document.permalink = record['permalink']
    document.raw_text = record['raw_text']
    document.content_hash = content_hash

    if is_changed:
        document.sync_state = RawDocument.SyncState.CHANGED
        document.classified_as = RawDocument.ClassifiedAs.UNCLASSIFIED
        document.classifier_version = None
        document.card_version = None
    elif document.sync_state == RawDocument.SyncState.REMOVED:
        document.sync_state = RawDocument.SyncState.CURRENT

    document.save()

    return 'changed' if is_changed else 'unchanged'


@transaction.atomic
def _sync_records(item, records):
    seen_refs = {record['external_ref'] for record in records}
    result = {'created': 0, 'changed': 0, 'removed': 0, 'total': len(records)}

    for record in records:
        state = _save_record(item, record)
        if state in result:
            result[state] += 1

    removed = RawDocument.objects.filter(item=item).exclude(external_ref__in=seen_refs)
    result['removed'] = removed.exclude(sync_state=RawDocument.SyncState.REMOVED).count()
    removed.update(sync_state=RawDocument.SyncState.REMOVED)

    item.item_count = len(records)
    item.last_synced_at = timezone.now()
    item.save(update_fields=['item_count', 'last_synced_at'])

    return result


def ingest_repository(item, client):
    # GitHub 응답을 전부 받은 뒤 DB 작업을 시작한다. API가 느릴 때 트랜잭션을 오래 잡지 않기 위함이다.
    records = _repository_records(item.label, client)

    return _sync_records(item, records)


def run_github_ingestion(job, connection):
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
    if not items:
        job.status = IngestionJob.Status.FAILED
        job.errors = [{'scope': 'github', 'code': 'no_repository_registered'}]
        job.progress = 100
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'errors', 'progress', 'completed_at'])
        return job

    errors = []
    collection_failed = False

    # PROCESS는 웹훅 등으로 이미 저장된 GitHub 원문만 AI 처리한다.
    if job.kind == IngestionJob.Kind.COLLECT:
        client = github_client(connection)
        for index, item in enumerate(items, start=1):
            try:
                ingest_repository(item, client)
            except GitHubError as exc:
                errors.append({'itemId': item.id, 'label': item.label, 'code': exc.code})

            _set_progress(job, int(PROGRESS_COLLECTED * index / len(items)))

        collection_failed = bool(errors) and len(errors) == len(items)

    return process_documents(job, errors, collection_failed)
