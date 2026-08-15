from rest_framework import serializers

from .models import Blank, InstructionCard, Step, ToneEvidence


class StepSerializer(serializers.ModelSerializer):
    entryId = serializers.IntegerField(source='entry_id', read_only=True)
    entryTitle = serializers.CharField(source='entry.title', read_only=True, default=None)

    class Meta:
        model = Step
        fields = ['id', 'ord', 'text', 'entryId', 'entryTitle']


class BlankSerializer(serializers.ModelSerializer):
    questionEn = serializers.CharField(source='question_en', read_only=True)
    saiAnswerEn = serializers.CharField(source='sai_answer_en', read_only=True)
    saiAnswerKo = serializers.CharField(source='sai_answer_ko', read_only=True)
    escalationId = serializers.IntegerField(source='escalation_id', read_only=True)

    class Meta:
        model = Blank
        fields = ['id', 'questionEn', 'saiAnswerEn', 'saiAnswerKo', 'escalationId']


# 말투 해석의 근거가 된 과거 대화
class ToneEvidenceSerializer(serializers.ModelSerializer):
    sourceLabel = serializers.CharField(source='source_label', read_only=True)
    outcomeNote = serializers.CharField(source='outcome_note', read_only=True)
    occurredAt = serializers.DateTimeField(source='occurred_at', read_only=True)
    documentId = serializers.IntegerField(source='document_id', read_only=True)

    class Meta:
        model = ToneEvidence
        fields = [
            'id', 'quote', 'outcomeNote', 'sourceLabel', 'permalink', 'occurredAt', 'documentId'
        ]


class CardListItemSerializer(serializers.ModelSerializer):
    assigneeId = serializers.IntegerField(source='assignee_id', read_only=True)
    assigneeName = serializers.CharField(source='assignee.display_name', read_only=True, default=None)
    scopeId = serializers.IntegerField(source='scope_id', read_only=True)
    scopeName = serializers.CharField(source='scope.name', read_only=True, default=None)
    deadlineText = serializers.CharField(source='deadline_text', read_only=True)
    deadlineAt = serializers.DateTimeField(source='deadline_at', read_only=True)
    isDeadlineInferred = serializers.BooleanField(source='is_deadline_inferred', read_only=True)
    toneNote = serializers.CharField(source='tone_note', read_only=True)
    sourceLabel = serializers.CharField(source='document.item.label', read_only=True, default=None)
    requestedBy = serializers.CharField(
        source='document.author_identity.external_handle', read_only=True, default=None
    )
    blankCount = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = InstructionCard
        fields = [
            'id',
            'status',
            'purpose',
            'deliverable',
            'deadlineText',
            'deadlineAt',
            'isDeadlineInferred',
            'toneNote',
            'assigneeId',
            'assigneeName',
            'scopeId',
            'scopeName',
            'sourceLabel',
            'requestedBy',
            'blankCount',
            'createdAt',
        ]

    def get_blankCount(self, obj):
        return obj.blanks.count()


class CardDetailSerializer(CardListItemSerializer):
    documentId = serializers.IntegerField(source='document_id', read_only=True)
    permalink = serializers.CharField(source='document.permalink', read_only=True, default=None)
    originalText = serializers.CharField(source='document.raw_text', read_only=True, default=None)
    steps = StepSerializer(many=True, read_only=True)
    blanks = BlankSerializer(many=True, read_only=True)
    toneEvidences = ToneEvidenceSerializer(source='tone_evidences', many=True, read_only=True)

    class Meta(CardListItemSerializer.Meta):
        fields = CardListItemSerializer.Meta.fields + [
            'documentId', 'permalink', 'originalText', 'steps', 'blanks', 'toneEvidences',
        ]


class CardListSerializer(serializers.Serializer):
    items = CardListItemSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


class CardStatusUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=InstructionCard.Status.choices)

    def update(self, instance, validated_data):
        instance.status = validated_data['status']
        instance.save(update_fields=['status'])

        return instance
