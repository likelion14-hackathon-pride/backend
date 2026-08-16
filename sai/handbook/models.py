from django.db import models
from pgvector.django import VectorField, HnswIndex

# 회사 규칙 / 프로젝트 범위 구분
class CompanyScope(models.Model):
    class Kind(models.TextChoices):
        COMPANY = 'COMPANY'
        PROJECT = 'PROJECT'

    # 회사 전반 규칙의 카테고리. PROJECT 범위는 area_key를 갖지 않는다.
    class AreaKey(models.TextChoices):
        COMPANY = 'COMPANY'
        PEOPLE = 'PEOPLE'
        PRODUCT_ENG = 'PRODUCT_ENG'
        SECURITY = 'SECURITY'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='scopes')
    kind = models.CharField(max_length=12, choices=Kind.choices)
    area_key = models.CharField(max_length=16, choices=AreaKey.choices, null=True, blank=True)
    name = models.CharField(max_length=80)
    description = models.TextField(null=True, blank=True)
    state = models.CharField(max_length=8, null=True, blank=True)

    class Meta:
        db_table = 'companies_scope'
        constraints = [
            # 회사 전반 규칙은 카테고리당 하나. PROJECT 범위는 area_key가 null이라
            # 이 제약에 걸리지 않는다(Postgres는 null을 서로 다른 값으로 취급).
            models.UniqueConstraint(fields=['company', 'area_key'], name='uniq_company_area_key'),
            # 채널을 범위에 연결할 때 이름으로 고르므로 같은 회사 안에서 중복되면 안 된다.
            models.UniqueConstraint(fields=['company', 'name'], name='uniq_company_scope_name'),
        ]


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

    # 항목이 어디서 만들어졌는지. 수집 소스(SLACK/GITHUB/FILE)
    # 사람이 만든 경로(ONBOARDING/ESCALATION/DIRECT_ENTRY)를 함께 담는다.
    class Origin(models.TextChoices):
        SLACK = 'SLACK'
        GITHUB = 'GITHUB'
        FILE = 'FILE'
        ONBOARDING = 'ONBOARDING'
        ESCALATION = 'ESCALATION'
        DIRECT_ENTRY = 'DIRECT_ENTRY'

    # AI 추출 신뢰도. 
    # 사람이 직접 등록한 항목은 null.
    class Confidence(models.TextChoices):
        HIGH = 'HIGH'
        MEDIUM = 'MEDIUM'
        LOW = 'LOW'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='handbook_entries')
    scope = models.ForeignKey(CompanyScope, on_delete=models.CASCADE, related_name='handbook_entries')
    title = models.CharField(max_length=200)
    body_ko = models.TextField(null=True, blank=True)
    body_en = models.TextField(null=True, blank=True)
    original_lang = models.CharField(max_length=2, default='ko')
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    confidence = models.CharField(max_length=6, choices=Confidence.choices, null=True, blank=True)
    origin = models.CharField(max_length=16, choices=Origin.choices)
    ask_count = models.SmallIntegerField(default=0)
    drift_status = models.CharField(max_length=16, choices=DriftStatus.choices, default=DriftStatus.CURRENT)
    # 대표가 이 항목을 검토한 시각. 보류(HOLD)는 상태를 바꾸지 않으므로
    # 이 값이 있어야 '아직 안 본 것'과 '보고 미뤄둔 것'이 구분된다.
    reviewed_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    # 대표가 핸드북에서 지운 시각. 행 자체는 남긴다. 두 가지 이유에서다.
    # 하나는 초안 재생성이 dedupe_key로 기존 항목을 찾는데(drafting._build_entry)
    # 행이 사라지면 원문은 그대로라 같은 규칙이 다시 만들어지기 때문이고,
    # 다른 하나는 지난 답변의 인용(qna.Citation)이 가리킬 곳을 잃기 때문이다.
    deleted_at = models.DateTimeField(null=True, blank=True)
    embedding_ko = VectorField(dimensions=1536, null=True, blank=True)
    embedding_en = VectorField(dimensions=1536, null=True, blank=True)
    embedding_model = models.CharField(max_length=40, null=True, blank=True)
    embedded_at = models.DateTimeField(null=True, blank=True)
    translated_at = models.DateTimeField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=64, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # OpenAPI의 CandidateReviewStatus. status와 reviewed_at 에서 파생되므로
    # 별도 컬럼을 두지 않는다. 두 값이 어긋날 여지를 만들지 않기 위함.
    class ReviewStatus(models.TextChoices):
        PENDING = 'PENDING'
        APPROVED = 'APPROVED'
        REJECTED = 'REJECTED'
        HELD = 'HELD'

    @property
    def review_status(self):
        if self.status == self.Status.CONFIRMED:
            return self.ReviewStatus.APPROVED
        if self.status == self.Status.ARCHIVED:
            return self.ReviewStatus.REJECTED
        if self.reviewed_at:
            return self.ReviewStatus.HELD

        return self.ReviewStatus.PENDING

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
        constraints = [
            # 같은 규칙인지 가리는 열쇠다. 두 줄이 되면 초안 재생성과 Day 0 답변이
            # 어느 쪽을 고칠지 알 수 없어진다.
            models.UniqueConstraint(
                fields=['company', 'dedupe_key'],
                condition=models.Q(dedupe_key__isnull=False),
                name='uniq_entry_dedupe_key',
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
    # 근거가 어느 출처에서 나왔는지. OWNER는 대표가 온보딩 질문에 직접 답한 경우(Day0에서)
    class Tag(models.TextChoices):
        SLACK = 'SLACK'
        GITHUB = 'GITHUB'
        NOTION = 'NOTION'
        FILE = 'FILE'
        OWNER = 'OWNER'

    company = models.ForeignKey('companies.Company', on_delete=models.CASCADE, related_name='handbook_evidences')
    entry = models.ForeignKey(HandbookEntry, on_delete=models.CASCADE, related_name='evidences')
    chunk = models.ForeignKey('sources.Chunk', on_delete=models.SET_NULL, null=True, blank=True, related_name='handbook_evidences')
    document = models.ForeignKey('sources.RawDocument', on_delete=models.SET_NULL, null=True, blank=True, related_name='handbook_evidences')
    quote = models.TextField(null=True, blank=True)
    locator = models.CharField(max_length=60, null=True, blank=True)
    tag = models.CharField(max_length=8, choices=Tag.choices)
    source_label = models.CharField(max_length=200, null=True, blank=True)
    speaker_name = models.CharField(max_length=60, null=True, blank=True)
    permalink = models.TextField(null=True, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'handbook_evidence'
