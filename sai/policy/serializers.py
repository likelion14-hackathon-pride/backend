from rest_framework import serializers

from .models import RiskKeyword


# 위험 작업 키워드 등록 및 응답용 시리얼라이저
class RiskKeywordSerializer(serializers.ModelSerializer):
    keyword = serializers.CharField(source='word', max_length=50)
    message = serializers.CharField(source='note', required=False, allow_null=True)
    severity = serializers.ChoiceField(
        source='level',
        choices=RiskKeyword.Level.choices,
        required=False,
    )
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = RiskKeyword
        fields = ['id', 'keyword', 'message', 'severity', 'createdAt']
        read_only_fields = ['id', 'createdAt']

    def validate_keyword(self, value):
        company = self.context.get('company')

        # 이미 등록된 키워드가 중복으로 저장되지 않도록 확인한다.
        if company and RiskKeyword.objects.filter(
            company=company,
            word=value,
        ).exists():
            raise serializers.ValidationError('keyword already registered')

        return value
