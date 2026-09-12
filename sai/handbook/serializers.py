from django.utils import timezone
from drf_yasg.utils import swagger_serializer_method
from rest_framework import serializers

from config.errors import (
    SCOPE_KIND_NOT_ALLOWED,
    SCOPE_NAME_BLANK,
    SCOPE_NAME_TAKEN,
    SCOPE_NOT_FOUND,
)

from .finalizing import mark_stale
from .models import CompanyScope, HandbookEntry, HandbookEvidence, HandbookRevision
from .services import DEFAULT_COMPANY_SCOPE_DESCRIPTIONS_EN


class CompanyScopeSerializer(serializers.ModelSerializer):
    areaKey = serializers.CharField(source='area_key', read_only=True, allow_null=True)
    descriptionEn = serializers.SerializerMethodField()
    # 이 공간의 확정 규칙 수. scopes_with_counts() 가 세어 붙인다.
    entryCount = serializers.IntegerField(source='entry_count', read_only=True, default=0)

    class Meta:
        model = CompanyScope
        fields = [
            'id', 'kind', 'areaKey', 'name', 'description', 'descriptionEn',
            'state', 'entryCount',
        ]

    @swagger_serializer_method(serializer_or_field=serializers.CharField(allow_null=True))
    def get_descriptionEn(self, obj):
        return DEFAULT_COMPANY_SCOPE_DESCRIPTIONS_EN.get(obj.area_key)


class CompanyScopeListSerializer(serializers.Serializer):
    items = CompanyScopeSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


class CompanyScopeCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyScope
        fields = ['kind', 'name', 'description']
        extra_kwargs = {'description': {'required': False}}

    # 회사 전반 규칙 범위는 회사 생성 시 고정된 4개로 시딩되므로 추가 생성을 막는다.
    def validate_kind(self, value):
        if value != CompanyScope.Kind.PROJECT:
            raise serializers.ValidationError(
                'only PROJECT scope can be created', code=SCOPE_KIND_NOT_ALLOWED
            )

        return value

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('name must not be blank', code=SCOPE_NAME_BLANK)

        # 대소문자만 다른 이름도 중복으로 본다. 채널 연결 시 헷갈리기 때문.
        if CompanyScope.objects.filter(company=self.context['company'], name__iexact=name).exists():
            raise serializers.ValidationError('scope name already exists', code=SCOPE_NAME_TAKEN)

        return name

    def create(self, validated_data):
        return CompanyScope.objects.create(company=self.context['company'], **validated_data)


class HandbookEntryCreateSerializer(serializers.ModelSerializer):
    # 대표는 한국어로만 적는다. 영어는 저장할 때 만들어진다.
    ruleEn = serializers.CharField(source='body_en', required=False, allow_blank=True)
    originalKo = serializers.CharField(source='body_ko')
    scopeId = serializers.PrimaryKeyRelatedField(source='scope', queryset=CompanyScope.objects.all())

    class Meta:
        model = HandbookEntry
        fields = ['title', 'ruleEn', 'originalKo', 'scopeId']

    def validate_scopeId(self, value):
        if value.company != self.context['company']:
            raise serializers.ValidationError('scope not found', code=SCOPE_NOT_FOUND)

        return value

    def create(self, validated_data):
        company = self.context['company']
        validated_data['body_en'] = validated_data.get('body_en') or None

        return HandbookEntry.objects.create(
            company=company, status=HandbookEntry.Status.CONFIRMED,
            origin='DIRECT_ENTRY', confirmed_at=timezone.now(),
            **validated_data,
        )


# 항목이 어디서 왔는지. 접힌 행에 한 줄로 보여 주고, 누르면 원문으로 간다.
# 근거가 여럿이면 가장 이른 것을 대표로 쓰고 나머지는 count 로 알린다.
class EntrySourceSerializer(serializers.Serializer):
    tag = serializers.CharField()
    label = serializers.CharField(allow_null=True)
    speakerName = serializers.CharField(allow_null=True)
    permalink = serializers.CharField(allow_null=True)
    occurredAt = serializers.DateTimeField(allow_null=True)
    count = serializers.IntegerField()


def entry_source(entry):
    evidences = list(entry.evidences.all())
    if not evidences:
        return None

    first = evidences[0]

    return {
        'tag': first.tag,
        'label': first.source_label,
        'speakerName': first.speaker_name,
        'permalink': first.permalink,
        'occurredAt': first.occurred_at,
        'count': len(evidences),
    }


class HandbookEntrySerializer(serializers.ModelSerializer):
    companyId = serializers.IntegerField(source='company_id', read_only=True)
    # 팀원 화면은 titleEn 을 쓴다. 아직 번역되지 않았으면 null 이므로 title 로 떨어뜨린다.
    titleEn = serializers.CharField(source='title_en', read_only=True, allow_null=True)
    scopeId = serializers.IntegerField(source='scope_id', read_only=True)
    scopeKind = serializers.CharField(source='scope.kind', read_only=True)
    scopeName = serializers.CharField(source='scope.name', read_only=True)
    areaKey = serializers.CharField(source='scope.area_key', read_only=True, allow_null=True)
    ruleEn = serializers.CharField(source='body_en', read_only=True)
    originalKo = serializers.CharField(source='body_ko', read_only=True)
    questionCount = serializers.IntegerField(source='ask_count', read_only=True)
    sourceType = serializers.CharField(source='origin', read_only=True)
    source = serializers.SerializerMethodField()
    reviewStatus = serializers.CharField(source='review_status', read_only=True)
    promotionType = serializers.CharField(source='promotion_type', read_only=True)
    isAutoPromoted = serializers.BooleanField(source='is_auto_promoted', read_only=True)
    autoPromotionMethod = serializers.CharField(
        source='auto_promotion_method', read_only=True, allow_null=True
    )
    promotionReason = serializers.JSONField(source='promotion_reason', read_only=True)
    evidenceCount = serializers.IntegerField(source='evidence_count', read_only=True)
    riskKeywords = serializers.JSONField(source='detected_risk_keywords', read_only=True)
    hasConflict = serializers.BooleanField(source='conflict_detected', read_only=True)
    hasSimilarRule = serializers.SerializerMethodField()
    similarEntryId = serializers.IntegerField(
        source='similar_entry_id', read_only=True, allow_null=True
    )
    similarityScore = serializers.FloatField(
        source='similarity_score', read_only=True, allow_null=True
    )
    autoPromotedAt = serializers.DateTimeField(
        source='auto_promoted_at', read_only=True, allow_null=True
    )
    promotionPolicyVersion = serializers.CharField(
        source='promotion_policy_version', read_only=True, allow_null=True
    )
    reviewedAt = serializers.DateTimeField(source='reviewed_at', read_only=True)
    translatedAt = serializers.DateTimeField(source='translated_at', read_only=True)
    embeddedAt = serializers.DateTimeField(source='embedded_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = HandbookEntry
        fields = [
            'id',
            'companyId',
            'title',
            'titleEn',
            'scopeId',
            'scopeKind',
            'scopeName',
            'areaKey',
            'ruleEn',
            'originalKo',
            'status',
            'reviewStatus',
            'reviewedAt',
            'promotionType',
            'isAutoPromoted',
            'autoPromotionMethod',
            'promotionReason',
            'evidenceCount',
            'riskKeywords',
            'hasConflict',
            'hasSimilarRule',
            'similarEntryId',
            'similarityScore',
            'autoPromotedAt',
            'promotionPolicyVersion',
            'confidence',
            'questionCount',
            'sourceType',
            'source',
            'translatedAt',
            'embeddedAt',
            'createdAt',
            'updatedAt',
        ]

    @swagger_serializer_method(serializer_or_field=EntrySourceSerializer)
    def get_source(self, obj):
        return entry_source(obj)

    @swagger_serializer_method(serializer_or_field=serializers.BooleanField())
    def get_hasSimilarRule(self, obj):
        return obj.similar_entry_id is not None


class HandbookEntryListSerializer(serializers.Serializer):
    items = HandbookEntrySerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


class HandbookEntryUpdateSerializer(serializers.ModelSerializer):
    ruleEn = serializers.CharField(source='body_en', required=False)
    originalKo = serializers.CharField(source='body_ko', required=False)
    scopeId = serializers.PrimaryKeyRelatedField(source='scope', queryset=CompanyScope.objects.all(), required=False)

    class Meta:
        model = HandbookEntry
        fields = ['title', 'ruleEn', 'originalKo', 'scopeId', 'status']
        extra_kwargs = {'title': {'required': False}, 'status': {'required': False}}

    def validate_scopeId(self, value):
        if value.company != self.instance.company:
            raise serializers.ValidationError('scope not found', code=SCOPE_NOT_FOUND)

        return value

    def update(self, instance, validated_data):
        HandbookRevision.objects.create(
            company=instance.company,
            entry=instance,
            before={
                'title': instance.title,
                'title_en': instance.title_en,
                'body_ko': instance.body_ko,
                'body_en': instance.body_en,
                'scope_id': instance.scope_id,
                'status': instance.status,
            },
            reason='owner_edit',
        )

        # 원문 언어 본문이 바뀌면 기존 번역과 임베딩은 더 이상 그 내용이 아니다.
        # 낡은 벡터를 남겨 두면 검색이 옛 문장을 물어온다.
        source_field = 'body_ko' if instance.original_lang == 'ko' else 'body_en'
        if source_field in validated_data and validated_data[source_field] != getattr(instance, source_field):
            mark_stale(instance)

        # 제목을 고치면 영어 제목은 다른 규칙의 이름이 된다. 비워 두면 다음 수집이 다시 만든다.
        if 'title' in validated_data and validated_data['title'] != instance.title:
            instance.title_en = None
            validated_data['title_en'] = None

        return super().update(instance, validated_data)


class HandbookEvidenceSerializer(serializers.ModelSerializer):
    documentId = serializers.IntegerField(source='document_id', read_only=True)
    chunkId = serializers.IntegerField(source='chunk_id', read_only=True)
    sourceLabel = serializers.CharField(source='source_label', read_only=True)
    speakerName = serializers.CharField(source='speaker_name', read_only=True)
    occurredAt = serializers.DateTimeField(source='occurred_at', read_only=True)

    class Meta:
        model = HandbookEvidence
        fields = [
            'id',
            'tag',
            'quote',
            'locator',
            'sourceLabel',
            'speakerName',
            'permalink',
            'occurredAt',
            'documentId',
            'chunkId',
        ]


class HandbookEvidenceListSerializer(serializers.Serializer):
    items = HandbookEvidenceSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# 대표의 초안 검토. APPROVE=확정, REJECT=보관, HOLD=초안 유지.
class HandbookReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=['APPROVE', 'REJECT', 'HOLD'])


class HandbookBulkReviewSerializer(serializers.Serializer):
    entryIds = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)
    decision = serializers.ChoiceField(choices=['APPROVE', 'REJECT'], default='APPROVE')


class HandbookBulkReviewResultSerializer(serializers.Serializer):
    decision = serializers.CharField()
    processedCount = serializers.IntegerField()
    approvedCount = serializers.IntegerField()
    rejectedCount = serializers.IntegerField()
    results = serializers.ListField(child=serializers.DictField())
    skipped = serializers.ListField(child=serializers.DictField())
