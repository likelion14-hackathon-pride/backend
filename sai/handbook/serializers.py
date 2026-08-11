from django.utils import timezone
from rest_framework import serializers

from .models import CompanyScope, HandbookEntry, HandbookRevision


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
            'questionCount',
            'sourceType',
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

        return super().update(instance, validated_data)
