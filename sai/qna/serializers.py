from rest_framework import serializers

from config.errors import (
    CONFLICTING_SOURCE,
    ESCALATION_SOURCE_REQUIRED,
    SCOPE_NOT_FOUND,
)
from handbook.models import CompanyScope

from .models import Escalation, Message


class AskInputSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=2000, trim_whitespace=True)
    threadId = serializers.IntegerField(required=False)
    scopeId = serializers.PrimaryKeyRelatedField(
        source='scope', queryset=CompanyScope.objects.all(), required=False, allow_null=True
    )

    def validate_scopeId(self, value):
        if value is not None and value.company_id != self.context['company'].id:
            raise serializers.ValidationError('scope not found', code=SCOPE_NOT_FOUND)

        return value


# 근거 한 건. entryId 가 있으면 확정 규칙, chunkId 가 있으면 과거 대화다.
class CitationSerializer(serializers.Serializer):
    entryId = serializers.IntegerField(allow_null=True)
    title = serializers.CharField(allow_null=True)
    scopeName = serializers.CharField(allow_null=True)
    chunkId = serializers.IntegerField(allow_null=True)
    # 과거 대화일 때 슬랙 원문으로 가는 링크.
    permalink = serializers.CharField(allow_null=True)


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


# 규칙이든 사례든 같은 모양으로 나가야 화면이 한 가지만 그리면 된다.
def _stored_citation(citation):
    if citation.entry:
        return {
            'entryId': citation.entry_id,
            'title': citation.entry.title,
            'scopeName': citation.entry.scope.name,
            'chunkId': None,
            'permalink': None,
        }

    if citation.chunk is None:
        return {
            'entryId': None, 'title': None, 'scopeName': None,
            'chunkId': None, 'permalink': None,
        }

    document = citation.chunk.document
    when = f'{document.occurred_at:%Y-%m-%d}' if document.occurred_at else None

    return {
        'entryId': None,
        'title': ' '.join(filter(None, [document.item.label, when])),
        'scopeName': citation.chunk.scope.name if citation.chunk.scope else None,
        'chunkId': citation.chunk_id,
        'permalink': document.permalink,
    }


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
        return [_stored_citation(citation) for citation in obj.citations.all()]


class MessageListSerializer(serializers.Serializer):
    items = MessageSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


# --- 에스컬레이션 (대표 확인 질문) ---


class EscalationSerializer(serializers.ModelSerializer):
    askedById = serializers.SerializerMethodField()
    askedByName = serializers.SerializerMethodField()
    questionEn = serializers.CharField(source='question_en', read_only=True)
    draftKo = serializers.CharField(source='draft_ko', read_only=True)
    sentText = serializers.CharField(source='sent_text', read_only=True)
    slackThreadRef = serializers.CharField(source='slack_thread_ref', read_only=True)
    answerKo = serializers.CharField(source='answer_ko', read_only=True)
    answerEn = serializers.CharField(source='answer_en', read_only=True)
    answerIsAnswer = serializers.BooleanField(source='answer_is_answer', read_only=True)
    answerReason = serializers.CharField(source='answer_reason', read_only=True)
    answerNeedsReview = serializers.BooleanField(source='answer_needs_review', read_only=True)
    proposedEntryId = serializers.IntegerField(source='proposed_entry_id', read_only=True)
    originMessageId = serializers.IntegerField(source='origin_message_id', read_only=True)
    # SAI가 답하지 않은 이유. 카드 미정 항목에서 올라온 질문은 물어본 적이 없어 비어 있다.
    originVerdict = serializers.CharField(
        source='origin_message.verdict', read_only=True, default=None
    )
    scopeId = serializers.IntegerField(source='scope_id', read_only=True)
    scopeName = serializers.CharField(source='scope.name', read_only=True, default=None)
    sentAt = serializers.DateTimeField(source='sent_at', read_only=True)
    answeredAt = serializers.DateTimeField(source='answered_at', read_only=True)
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = Escalation
        fields = [
            'id',
            'status',
            'askedById',
            'askedByName',
            'questionEn',
            'draftKo',
            'sentText',
            'slackThreadRef',
            'answerKo',
            'answerEn',
            'answerIsAnswer',
            'answerReason',
            'answerNeedsReview',
            'proposedEntryId',
            'originMessageId',
            'originVerdict',
            'scopeId',
            'scopeName',
            'sentAt',
            'answeredAt',
            'createdAt',
        ]

    def _asker(self, obj):
        for blank in obj.card_blanks.all():
            assignee = blank.card.assignee
            if assignee is not None:
                return assignee

        origin = obj.origin_message
        if origin is not None:
            return origin.thread.user

        return obj.asked_by

    def get_askedById(self, obj):
        return self._asker(obj).id

    def get_askedByName(self, obj):
        return self._asker(obj).display_name


class EscalationListSerializer(serializers.Serializer):
    items = EscalationSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


class ProposalSerializer(serializers.Serializer):
    title = serializers.CharField()
    bodyKo = serializers.CharField()
    bodyEn = serializers.CharField(allow_null=True)
    scopeId = serializers.IntegerField(allow_null=True)
    scopeName = serializers.CharField(allow_null=True)
    scopeKind = serializers.CharField(allow_null=True)
    sourceLabel = serializers.CharField(allow_null=True)
    answeredAt = serializers.DateTimeField(allow_null=True)


class EscalationDetailSerializer(EscalationSerializer):
    proposal = ProposalSerializer(allow_null=True, read_only=True)

    class Meta(EscalationSerializer.Meta):
        fields = EscalationSerializer.Meta.fields + ['proposal']


# 미리보기에서 고친 값만 보낸다. 안 보내면 제안 그대로 저장된다.
class EscalationApproveSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, required=False, trim_whitespace=True)
    ruleEn = serializers.CharField(max_length=2000, required=False, trim_whitespace=True)
    scopeId = serializers.PrimaryKeyRelatedField(
        source='scope', queryset=CompanyScope.objects.all(), required=False
    )

    def validate_scopeId(self, value):
        if value.company_id != self.context['company'].id:
            raise serializers.ValidationError('scope not found', code=SCOPE_NOT_FOUND)

        return value


# 출처는 세 가지다. Ask SAI 답변(messageId), 지시 카드의 미정 항목(blankId), 직접 입력.
class EscalationCreateSerializer(serializers.Serializer):
    messageId = serializers.IntegerField(required=False)
    blankId = serializers.IntegerField(required=False)
    questionEn = serializers.CharField(max_length=2000, required=False)
    draftKo = serializers.CharField(max_length=2000, required=False)

    def validate(self, attrs):
        if attrs.get('messageId') and attrs.get('blankId'):
            raise serializers.ValidationError(
                'messageId and blankId cannot be used together', code=CONFLICTING_SOURCE
            )

        if not any(attrs.get(key) for key in ('messageId', 'blankId', 'questionEn')):
            raise serializers.ValidationError(
                'one of messageId, blankId, questionEn is required',
                code=ESCALATION_SOURCE_REQUIRED,
            )

        return attrs


class EscalationDraftUpdateSerializer(serializers.Serializer):
    draftKo = serializers.CharField(source='draft_ko', max_length=2000, trim_whitespace=True)

    def update(self, instance, validated_data):
        instance.draft_ko = validated_data['draft_ko']
        instance.save(update_fields=['draft_ko'])

        return instance


# 보내기 직전 화면에서 팀원이 한 줄씩 덧붙일 수 있다. 자기 언어로 적으면
# 보낼 때 SAI가 한국어 문장으로 바꿔 초안 뒤에 붙인다.
MAX_ADDITIONS = 5


class EscalationSendSerializer(serializers.Serializer):
    itemId = serializers.IntegerField(help_text='질문을 올릴 슬랙 채널(수집 대상 채널) ID')
    extraEn = serializers.ListField(
        child=serializers.CharField(max_length=500, trim_whitespace=True),
        required=False,
        max_length=MAX_ADDITIONS,
        help_text='팀원이 덧붙인 줄. 보낼 때 한국어 문장으로 바뀌어 초안 뒤에 붙습니다.',
    )
