from rest_framework import serializers

from handbook.models import CompanyScope

from .models import Message


class AskInputSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=2000, trim_whitespace=True)
    threadId = serializers.IntegerField(required=False)
    scopeId = serializers.PrimaryKeyRelatedField(
        source='scope', queryset=CompanyScope.objects.all(), required=False, allow_null=True
    )

    def validate_scopeId(self, value):
        if value is not None and value.company_id != self.context['company'].id:
            raise serializers.ValidationError('scope not found')

        return value


class CitationSerializer(serializers.Serializer):
    entryId = serializers.IntegerField(allow_null=True)
    title = serializers.CharField(allow_null=True)
    scopeName = serializers.CharField(allow_null=True)
    chunkId = serializers.IntegerField(allow_null=True)


class RiskWarningSerializer(serializers.Serializer):
    keyword = serializers.CharField()
    level = serializers.CharField()
    note = serializers.CharField()


class AskResultSerializer(serializers.Serializer):
    threadId = serializers.IntegerField()
    messageId = serializers.IntegerField()
    # OpenAPI의 AskResult.resultType. verdict에서 파생된다.
    resultType = serializers.CharField()
    verdict = serializers.CharField()
    answer = serializers.CharField(allow_null=True)
    # 근거가 없을 때 대표에게 보낼 한국어 질문 초안.
    draftKo = serializers.CharField(allow_null=True)
    citations = CitationSerializer(many=True)
    warnings = RiskWarningSerializer(many=True)
    latencyMs = serializers.IntegerField()


# 대화 이력 응답용
class MessageSerializer(serializers.ModelSerializer):
    threadId = serializers.IntegerField(source='thread_id', read_only=True)
    bodyKo = serializers.CharField(source='body_ko', read_only=True)
    bodyEn = serializers.CharField(source='body_en', read_only=True)
    latencyMs = serializers.IntegerField(source='latency_ms', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)
    citations = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            'id',
            'threadId',
            'role',
            'bodyKo',
            'bodyEn',
            'verdict',
            'citations',
            'latencyMs',
            'createdAt',
        ]

    def get_citations(self, obj):
        return [
            {
                'entryId': citation.entry_id,
                'title': citation.entry.title if citation.entry else None,
                'scopeName': citation.entry.scope.name if citation.entry else None,
                'chunkId': citation.chunk_id,
            }
            for citation in obj.citations.all()
        ]


class MessageListSerializer(serializers.Serializer):
    items = MessageSerializer(many=True)
