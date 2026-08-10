from django.utils import timezone
from drf_yasg.utils import swagger_serializer_method
from rest_framework import serializers

from .models import CompanyScope, HandbookEntry, HandbookRevision


# 핸드북 항목 직접 등록용 시리얼라이저
class HandbookEntryCreateSerializer(serializers.ModelSerializer):
    ruleEn = serializers.CharField(source='body_en')
    originalKo = serializers.CharField(source='body_ko')
    scope = serializers.ChoiceField(choices=CompanyScope.Kind.choices)
    projectId = serializers.CharField(required=False, allow_null=True)

    class Meta:
        model = HandbookEntry
        fields = ['title', 'ruleEn', 'originalKo', 'scope', 'projectId']

    def validate(self, attrs):
        if attrs['scope'] == CompanyScope.Kind.PROJECT and not attrs.get('projectId'):
            raise serializers.ValidationError({'projectId': 'projectId is required'})

        return attrs

    def create(self, validated_data):
        company = self.context['company']
        scope_kind = validated_data.pop('scope')
        project_id = validated_data.pop('projectId', None)

        scope, _ = CompanyScope.objects.get_or_create(
            company=company,
            kind=scope_kind,
            area_key=project_id,
            defaults={'name': project_id or '회사 규칙', 'state': 'ACTIVE'},
        )

        return HandbookEntry.objects.create(
            company=company, scope=scope, status=HandbookEntry.Status.CONFIRMED,
            origin='DIRECT_ENTRY', confirmed_at=timezone.now(),
            **validated_data,
        )


# 핸드북 현재 내용 응답용 시리얼라이저
class HandbookEntryVersionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    version = serializers.IntegerField()
    ruleEn = serializers.CharField()
    originalKo = serializers.CharField()
    citations = serializers.ListField(child=serializers.DictField())
    createdAt = serializers.DateTimeField()


# 핸드북 항목 응답용 시리얼라이저
class HandbookEntrySerializer(serializers.ModelSerializer):
    companyId = serializers.IntegerField(source='company_id', read_only=True)
    scope = serializers.CharField(source='scope.kind', read_only=True)
    projectId = serializers.CharField(source='scope.area_key', read_only=True, allow_null=True)
    questionCount = serializers.IntegerField(source='ask_count', read_only=True)
    sourceType = serializers.CharField(source='origin', read_only=True)
    currentVersion = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)

    class Meta:
        model = HandbookEntry
        fields = [
            'id',
            'companyId',
            'title',
            'scope',
            'projectId',
            'status',
            'questionCount',
            'sourceType',
            'currentVersion',
            'createdAt',
            'updatedAt',
        ]

    @swagger_serializer_method(serializer_or_field=HandbookEntryVersionSerializer)
    def get_currentVersion(self, obj):
        return {
            'id': obj.id,
            'version': obj.revisions.count() + 1,
            'ruleEn': obj.body_en,
            'originalKo': obj.body_ko,
            'citations': [],
            'createdAt': obj.created_at,
        }


# 핸드북 목록 응답용 시리얼라이저
class HandbookEntryListSerializer(serializers.Serializer):
    items = HandbookEntrySerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# 핸드북 항목 수정용 시리얼라이저
class HandbookEntryUpdateSerializer(serializers.ModelSerializer):
    ruleEn = serializers.CharField(source='body_en', required=False)
    originalKo = serializers.CharField(source='body_ko', required=False)
    scope = serializers.ChoiceField(choices=CompanyScope.Kind.choices, required=False)
    projectId = serializers.CharField(required=False, allow_null=True)

    class Meta:
        model = HandbookEntry
        fields = ['title', 'ruleEn', 'originalKo', 'scope', 'projectId', 'status']
        extra_kwargs = {'title': {'required': False}, 'status': {'required': False}}

    def validate(self, attrs):
        scope_kind = attrs.get('scope', self.instance.scope.kind)
        project_id = attrs.get('projectId', self.instance.scope.area_key)

        if scope_kind == CompanyScope.Kind.PROJECT and not project_id:
            raise serializers.ValidationError({'projectId': 'projectId is required'})

        return attrs

    def update(self, instance, validated_data):
        scope_kind = validated_data.pop('scope', instance.scope.kind)
        project_id = validated_data.pop('projectId', instance.scope.area_key)

        if scope_kind == CompanyScope.Kind.COMPANY:
            project_id = None

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

        scope, _ = CompanyScope.objects.get_or_create(
            company=instance.company,
            kind=scope_kind,
            area_key=project_id,
            defaults={'name': project_id or '회사 규칙', 'state': 'ACTIVE'},
        )
        instance.scope = scope

        return super().update(instance, validated_data)
