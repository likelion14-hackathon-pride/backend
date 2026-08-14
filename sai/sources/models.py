from django.db import models
from pgvector.django import VectorField, HnswIndex


class Connection(models.Model):
    class Kind(models.TextChoices):
        GITHUB = 'GITHUB'
        SLACK = 'SLACK'
        NOTION = 'NOTION'
        LOCAL = 'LOCAL'

    class Status(models.TextChoices):
        CONNECTED = 'CONNECTED'
        ERROR = 'ERROR'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='connections')
    kind = models.CharField(max_length=10, choices=Kind.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.CONNECTED)
    credential_ref = models.CharField(max_length=200, null=True, blank=True)
    external_workspace_id = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    display_name = models.CharField(max_length=200, null=True, blank=True)
    bot_token = models.CharField(max_length=200, null=True, blank=True)
    signing_secret = models.CharField(max_length=100, null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sources_connection'
        constraints = [
            # 회사당 소스 종류별로 살아 있는 연결은 하나.
            models.UniqueConstraint(
                fields=['company', 'kind'],
                condition=models.Q(disconnected_at__isnull=True),
                name='uniq_active_connection_per_kind',
            ),
            # 한 워크스페이스가 두 회사에 연결되면 웹훅이 어느 회사인지 정할 수 없다.
            models.UniqueConstraint(
                fields=['external_workspace_id'],
                condition=models.Q(disconnected_at__isnull=True),
                name='uniq_active_workspace',
            ),
        ]


# 외부 계정 <-> SAI 사용자 매핑
class Identity(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='source_identities')
    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='identities')
    external_user_id = models.CharField(max_length=64)
    external_handle = models.CharField(max_length=120, null=True, blank=True)
    is_internal = models.BooleanField(default=False)
    is_bot = models.BooleanField(default=False)
    user = models.ForeignKey('accounts.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='source_identities')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sources_identity'
        constraints = [
            models.UniqueConstraint(fields=['connection', 'external_user_id'], name='uniq_identity_external_user'),
        ]


# 수집 대상 채널 / 업로드 파일
class Item(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='source_items')
    connection = models.ForeignKey(Connection, on_delete=models.CASCADE, related_name='items')
    external_id = models.CharField(max_length=200)
    label = models.CharField(max_length=200)
    scope = models.ForeignKey('handbook.CompanyScope', on_delete=models.SET_NULL, null=True, blank=True, related_name='source_items')
    is_scope_confirmed = models.BooleanField(default=False)
    item_count = models.IntegerField(default=0)
    storage_key = models.CharField(max_length=255, null=True, blank=True)
    mime_type = models.CharField(max_length=100, null=True, blank=True)
    byte_size = models.BigIntegerField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'sources_item'
        constraints = [
            models.UniqueConstraint(fields=['connection', 'external_id'], name='uniq_item_external_id'),
        ]


# 수집한 원문 1건
class RawDocument(models.Model):
    class ClassifiedAs(models.TextChoices):
        INSTRUCTION = 'INSTRUCTION'
        CONTEXT = 'CONTEXT'
        AMBIGUOUS = 'AMBIGUOUS'
        UNCLASSIFIED = 'UNCLASSIFIED'

    class SyncState(models.TextChoices):
        CURRENT = 'CURRENT'
        CHANGED = 'CHANGED'
        REMOVED = 'REMOVED'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='raw_documents')
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='documents')
    external_ref = models.CharField(max_length=200)
    thread_ref = models.CharField(max_length=200, null=True, blank=True)
    author_identity = models.ForeignKey(Identity, on_delete=models.SET_NULL, null=True, blank=True, related_name='documents')
    occurred_at = models.DateTimeField(null=True, blank=True)
    permalink = models.TextField(null=True, blank=True)
    raw_text = models.TextField()
    content_hash = models.CharField(max_length=64)
    classified_as = models.CharField(max_length=14, choices=ClassifiedAs.choices, default=ClassifiedAs.UNCLASSIFIED)
    classifier_version = models.CharField(max_length=20, null=True, blank=True)
    sync_state = models.CharField(max_length=10, choices=SyncState.choices, default=SyncState.CURRENT)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'sources_rawdocument'
        constraints = [
            models.UniqueConstraint(fields=['item', 'external_ref'], name='uniq_document_external_ref'),
        ]


class Chunk(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='chunks')
    document = models.ForeignKey(RawDocument, on_delete=models.CASCADE, related_name='chunks')
    scope = models.ForeignKey('handbook.CompanyScope', on_delete=models.PROTECT, null=True, blank=True, related_name='chunks')
    ord = models.SmallIntegerField(default=0)
    text = models.TextField()
    lang = models.CharField(max_length=2, default='ko')
    embedding = VectorField(dimensions=1024, null=True, blank=True)
    embedding_model = models.CharField(max_length=40, null=True, blank=True)
    embedded_at = models.DateTimeField(null=True, blank=True)
    token_count = models.SmallIntegerField(default=0)
    is_secret_filtered = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'sources_chunk'
        indexes = [
            HnswIndex(
                name='chunk_emb_idx',
                fields=['embedding'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ]


class IngestionJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = 'QUEUED'
        RUNNING = 'RUNNING'
        SUCCEEDED = 'SUCCEEDED'
        PARTIAL = 'PARTIAL'
        FAILED = 'FAILED'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='ingestion_jobs')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    progress = models.SmallIntegerField(default=0)
    item_ids = models.JSONField(null=True, blank=True)
    entry_count = models.IntegerField(default=0)
    errors = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'sources_ingestionjob'
