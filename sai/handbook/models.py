from django.db import models
from pgvector.django import VectorField, HnswIndex

# 회사 규칙 / 프로젝트 범위 구분
class CompanyScope(models.Model):
    class Kind(models.TextChoices):
        COMPANY = 'COMPANY'
        PROJECT = 'PROJECT'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='scopes')
    kind = models.CharField(max_length=12, choices=Kind.choices)
    area_key = models.CharField(max_length=16, null=True, blank=True)
    name = models.CharField(max_length=80)
    description = models.TextField(null=True, blank=True)
    state = models.CharField(max_length=8, null=True, blank=True)

    class Meta:
        db_table = 'companies_scope'


# 핸드북 항목
class HandbookEntry(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT'
        CONFIRMED = 'CONFIRMED'
        BLANK = 'BLANK'
        ARCHIVED = 'ARCHIVED'

    class DriftStatus(models.TextChoices):
        CURRENT = 'CURRENT'
        DRIFTED = 'DRIFTED'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='handbook_entries')
    scope = models.ForeignKey(CompanyScope, on_delete=models.CASCADE, related_name='handbook_entries')
    title = models.CharField(max_length=200)
    body_ko = models.TextField(null=True, blank=True)
    body_en = models.TextField(null=True, blank=True)
    original_lang = models.CharField(max_length=2, default='ko')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    confidence = models.CharField(max_length=6, null=True, blank=True)
    origin = models.CharField(max_length=16)
    ask_count = models.SmallIntegerField(default=0)
    drift_status = models.CharField(max_length=16, choices=DriftStatus.choices, default=DriftStatus.CURRENT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    embedding_ko = VectorField(dimensions=1536, null=True, blank=True)
    embedding_en = VectorField(dimensions=1536, null=True, blank=True)
    embedding_model = models.CharField(max_length=40, null=True, blank=True)
    embedded_at = models.DateTimeField(null=True, blank=True)
    translated_at = models.DateTimeField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'handbook_entry'
        indexes = [
            HnswIndex(
                name='hb_emb_ko_idx',
                fields=['embedding_ko'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
            HnswIndex(
                name='hb_emb_en_idx',
                fields=['embedding_en'],
                m=16,
                ef_construction=64,
                opclasses=['vector_cosine_ops'],
            ),
        ]

# 핸드북 항목 수정 이력 -> 수정 전 내용 기록 (버전관리?)
class HandbookRevision(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='handbook_revisions')
    entry = models.ForeignKey(HandbookEntry, on_delete=models.CASCADE, related_name='revisions')
    before = models.JSONField(null=True, blank=True)
    reason = models.CharField(max_length=200, null=True, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'handbook_revision'


# 핸드북 항목 근거
class HandbookEvidence(models.Model):
    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='handbook_evidences')
    entry = models.ForeignKey(HandbookEntry, on_delete=models.CASCADE, related_name='evidences')
    chunk = models.ForeignKey('sources.Chunk', on_delete=models.SET_NULL, null=True, blank=True, related_name='handbook_evidences')
    document = models.ForeignKey('sources.RawDocument', on_delete=models.SET_NULL, null=True, blank=True, related_name='handbook_evidences')
    quote = models.TextField(null=True, blank=True)
    locator = models.CharField(max_length=60, null=True, blank=True)
    tag = models.CharField(max_length=8)
    source_label = models.CharField(max_length=200, null=True, blank=True)
    speaker_name = models.CharField(max_length=60, null=True, blank=True)
    permalink = models.TextField(null=True, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'handbook_evidence'
