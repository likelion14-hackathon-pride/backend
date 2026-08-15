from django.utils import timezone
from rest_framework import serializers

from .finalizing import mark_stale
from .models import CompanyScope, HandbookEntry, HandbookEvidence, HandbookRevision


# 핸드북 범위 응답용 시리얼라이저
class CompanyScopeSerializer(serializers.ModelSerializer):
    areaKey = serializers.CharField(source='area_key', read_only=True, allow_null=True)

    class Meta:
        model = CompanyScope
        fields = ['id', 'kind', 'areaKey', 'name', 'description', 'state']


class CompanyScopeListSerializer(serializers.Serializer):
    items = CompanyScopeSerializer(many=True)


# 프로젝트 범위 생성용 시리얼라이저
class CompanyScopeCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyScope
        fields = ['kind', 'name', 'description']
        extra_kwargs = {'description': {'required': False}}

    # 회사 전반 규칙 범위는 회사 생성 시 고정된 4개로 시딩되므로 추가 생성을 막는다.
    def validate_kind(self, value):
        if value != CompanyScope.Kind.PROJECT:
            raise serializers.ValidationError('only PROJECT scope can be created')

        return value

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('name must not be blank')

        # 대소문자만 다른 이름도 중복으로 본다. 채널 연결 시 헷갈리기 때문.
        if CompanyScope.objects.filter(company=self.context['company'], name__iexact=name).exists():
            raise serializers.ValidationError('scope name already exists')

        return name

    def create(self, validated_data):
        return CompanyScope.objects.create(company=self.context['company'], **validated_data)


# 핸드북 항목 직접 등록용 시리얼라이저
class HandbookEntryCreateSerializer(serializers.ModelSerializer):
    ruleEn = serializers.CharField(source='body_en')
    originalKo = serializers.CharField(source='body_ko')
    scopeId = serializers.PrimaryKeyRelatedField(source='scope', queryset=CompanyScope.objects.all())

    class Meta:
        model = HandbookEntry
        fields = ['title', 'ruleEn', 'originalKo', 'scopeId']

    def validate_scopeId(self, value):
        if value.company != self.context['company']:
            raise serializers.ValidationError('scope not found')

        return value

    def create(self, validated_data):
        company = self.context['company']

        return HandbookEntry.objects.create(
            company=company, status=HandbookEntry.Status.CONFIRMED,
            origin='DIRECT_ENTRY', confirmed_at=timezone.now(),
            **validated_data,
        )


# 핸드북 항목 응답용 시리얼라이저
class HandbookEntrySerializer(serializers.ModelSerializer):
    companyId = serializers.IntegerField(source='company_id', read_only=True)
    scopeId = serializers.IntegerField(source='scope_id', read_only=True)
    scopeKind = serializers.CharField(source='scope.kind', read_only=True)
    scopeName = serializers.CharField(source='scope.name', read_only=True)
    areaKey = serializers.CharField(source='scope.area_key', read_only=True, allow_null=True)
    ruleEn = serializers.CharField(source='body_en', read_only=True)
    originalKo = serializers.CharField(source='body_ko', read_only=True)
    questionCount = serializers.IntegerField(source='ask_count', read_only=True)
    sourceType = serializers.CharField(source='origin', read_only=True)
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
            'scopeId',
            'scopeKind',
            'scopeName',
            'areaKey',
            'ruleEn',
            'originalKo',
            'status',
            'confidence',
            'questionCount',
            'sourceType',
            'translatedAt',
            'embeddedAt',
            'createdAt',
            'updatedAt',
        ]


# 핸드북 목록 응답용 시리얼라이저
class HandbookEntryListSerializer(serializers.Serializer):
    items = HandbookEntrySerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# 핸드북 항목 수정용 시리얼라이저
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
            raise serializers.ValidationError('scope not found')

        return value

    def update(self, instance, validated_data):
        HandbookRevision.objects.create(
            company=instance.company,
            entry=instance,
            before={
                'title': instance.title,
                'body_ko': instance.body_ko,
                'body_en': instance.body_en,
                'scope_id': instance.scope_id,
                'status': instance.status,
            },
        )

        # 원문 언어 본문이 바뀌면 기존 번역과 임베딩은 더 이상 그 내용이 아니다.
        # 낡은 벡터를 남겨 두면 검색이 옛 문장을 물어온다.
        source_field = 'body_ko' if instance.original_lang == 'ko' else 'body_en'
        if source_field in validated_data and validated_data[source_field] != getattr(instance, source_field):
            mark_stale(instance)

        return super().update(instance, validated_data)


# 핸드북 항목 근거 응답용 시리얼라이저
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


# 대표의 초안 검토. APPROVE=확정, REJECT=보관, HOLD=초안 유지.
class HandbookReviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=['APPROVE', 'REJECT', 'HOLD'])


class HandbookBulkReviewSerializer(serializers.Serializer):
    entryIds = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)


class HandbookBulkReviewResultSerializer(serializers.Serializer):
    approvedCount = serializers.IntegerField()
    skipped = serializers.ListField(child=serializers.DictField())
