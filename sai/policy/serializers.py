from rest_framework import serializers

from config.errors import KEYWORD_TAKEN

from .models import RiskKeyword


class RiskKeywordSerializer(serializers.ModelSerializer):
    keyword = serializers.CharField(source='word', max_length=50)
    message = serializers.CharField(source='note', required=False, allow_null=True)
    severity = serializers.ChoiceField(source='level', choices=RiskKeyword.Level.choices, required=False)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = RiskKeyword
        fields = ['id', 'keyword', 'message', 'severity', 'createdAt']
        read_only_fields = ['id', 'createdAt']

    def validate_keyword(self, value):
        company = self.context.get('company')

        # 이미 등록된 키워드가 중복으로 저장되지 않도록 확인한다.
        if company and RiskKeyword.objects.filter(company=company, word=value).exists():
            raise serializers.ValidationError('keyword already registered', code=KEYWORD_TAKEN)

        return value
