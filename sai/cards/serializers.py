from rest_framework import serializers

from .models import Blank, InstructionCard, Step, ToneEvidence


class StepSerializer(serializers.ModelSerializer):
    textEn = serializers.CharField(source='text_en', read_only=True)
    entryId = serializers.IntegerField(source='entry_id', read_only=True)
    entryTitle = serializers.CharField(source='entry.title', read_only=True, default=None)

    class Meta:
        model = Step
        fields = ['id', 'ord', 'text', 'textEn', 'entryId', 'entryTitle']


class BlankSerializer(serializers.ModelSerializer):
    questionEn = serializers.CharField(source='question_en', read_only=True)
    saiAnswerEn = serializers.CharField(source='sai_answer_en', read_only=True)
    saiAnswerKo = serializers.CharField(source='sai_answer_ko', read_only=True)
    escalationId = serializers.IntegerField(source='escalation_id', read_only=True)

    class Meta:
        model = Blank
        fields = ['id', 'questionEn', 'saiAnswerEn', 'saiAnswerKo', 'escalationId']


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
    purposeEn = serializers.CharField(source='purpose_en', read_only=True)
    deliverableEn = serializers.CharField(source='deliverable_en', read_only=True)
    deadlineText = serializers.CharField(source='deadline_text', read_only=True)
    deadlineTextEn = serializers.CharField(source='deadline_text_en', read_only=True)
    deadlineAt = serializers.DateTimeField(source='deadline_at', read_only=True)
    isDeadlineInferred = serializers.BooleanField(source='is_deadline_inferred', read_only=True)
    toneNote = serializers.CharField(source='tone_note', read_only=True)
    toneNoteEn = serializers.CharField(source='tone_note_en', read_only=True)
    duplicateOfId = serializers.IntegerField(source='duplicate_of_id', read_only=True)
    duplicateCount = serializers.SerializerMethodField()
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
            'purposeEn',
            'deliverable',
            'deliverableEn',
            'deadlineText',
            'deadlineTextEn',
            'deadlineAt',
            'isDeadlineInferred',
            'urgency',
            'toneNote',
            'toneNoteEn',
            'duplicateOfId',
            'duplicateCount',
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

    # 같은 요청이 슬랙에 올라온 횟수. 1이면 한 번만 올라온 것이다.
    # 목록에서는 view 가 미리 세어 둔다. 없으면 직접 세되 그만큼 쿼리가 늘어난다.
    def get_duplicateCount(self, obj):
        counted = getattr(obj, 'duplicate_count', None)

        return (obj.duplicates.count() if counted is None else counted) + 1


class CardDetailSerializer(CardListItemSerializer):
    documentId = serializers.IntegerField(source='document_id', read_only=True)
    permalink = serializers.CharField(source='document.permalink', read_only=True, default=None)
    originalText = serializers.CharField(source='document.raw_text', read_only=True, default=None)
    steps = StepSerializer(many=True, read_only=True)
    blanks = BlankSerializer(many=True, read_only=True)
    toneEvidences = ToneEvidenceSerializer(source='tone_evidences', many=True, read_only=True)
    duplicateSources = serializers.SerializerMethodField()

    class Meta(CardListItemSerializer.Meta):
        fields = CardListItemSerializer.Meta.fields + [
            'documentId', 'permalink', 'originalText', 'steps', 'blanks', 'toneEvidences',
            'duplicateSources',
        ]

    # 같은 요청이 다시 올라온 슬랙 원문들. 언제 또 재촉했는지 볼 수 있어야 한다.
    def get_duplicateSources(self, obj):
        return [
            {
                'documentId': card.document_id,
                'permalink': card.document.permalink if card.document else None,
                'occurredAt': card.document.occurred_at if card.document else None,
            }
            for card in obj.duplicates.select_related('document').order_by('id')
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
