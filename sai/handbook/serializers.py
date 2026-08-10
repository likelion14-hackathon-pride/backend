from django.utils import timezone
from rest_framework import serializers

from .models import CompanyScope, HandbookEntry


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
            raise serializers.ValidationError({
                'projectId': 'projectId is required',
            })

        return attrs

    def create(self, validated_data):
        company = self.context['company']
        scope_kind = validated_data.pop('scope')
        project_id = validated_data.pop('projectId', None)

        scope, _ = CompanyScope.objects.get_or_create(
            company=company,
            kind=scope_kind,
            area_key=project_id,
            defaults={
                'name': project_id or '회사 규칙',
                'state': 'ACTIVE',
            },
        )

        return HandbookEntry.objects.create(
            company=company,
            scope=scope,
            status=HandbookEntry.Status.CONFIRMED,
            origin='DIRECT_ENTRY',
            confirmed_at=timezone.now(),
            **validated_data,
        )
