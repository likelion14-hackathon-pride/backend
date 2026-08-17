from django.utils import timezone
from drf_yasg.utils import swagger_serializer_method
from rest_framework import serializers

from accounts.models import Membership
from companies.timing import STATES
from config.errors import (
    CARD_FIELD_REQUIRED,
    NOT_A_MEMBER,
    SCOPE_NOT_FOUND,
    TASK_FIELD_REQUIRED,
)
from handbook.models import CompanyScope
from handbook.serializers import (
    CompanyScopeSerializer,
    EntrySourceSerializer,
    entry_source,
)
from qna.serializers import RiskWarningSerializer

from .models import Blank, InstructionCard, Step, Task, ToneEvidence
from .timing import BASES


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
    answeredBy = serializers.CharField(source='answered_by', read_only=True)
    # 아직 아무도 답하지 않은 빈칸만 사람을 기다린다.
    needsOwner = serializers.SerializerMethodField()
    citations = serializers.JSONField(source='answer_citations', read_only=True)
    escalationId = serializers.IntegerField(source='escalation_id', read_only=True)

    class Meta:
        model = Blank
        fields = [
            'id', 'questionEn', 'saiAnswerEn', 'saiAnswerKo', 'answeredBy',
            'needsOwner', 'citations', 'escalationId',
        ]

    def get_needsOwner(self, obj):
        return obj.answered_by is None


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


class CardSerializer(serializers.ModelSerializer):
    # 보드 열. cards_for() 가 계산해 붙인다.
    column = serializers.CharField(read_only=True)
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
    permalink = serializers.CharField(source='document.permalink', read_only=True, default=None)
    requestedBy = serializers.CharField(
        source='document.author_identity.external_handle', read_only=True, default=None
    )
    blankCount = serializers.SerializerMethodField()
    openQuestionCount = serializers.IntegerField(source='open_question_count', read_only=True)
    answeredQuestionCount = serializers.IntegerField(
        source='answered_question_count', read_only=True
    )
    isRead = serializers.SerializerMethodField()
    createdAt = serializers.DateTimeField(source='created_at', read_only=True)

    class Meta:
        model = InstructionCard
        fields = [
            'id',
            'column',
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
            'permalink',
            'requestedBy',
            'blankCount',
            'openQuestionCount',
            'answeredQuestionCount',
            'isRead',
            'createdAt',
        ]

    def get_blankCount(self, obj):
        return obj.blanks.count()

    def get_isRead(self, obj):
        return obj.read_at is not None

    # 같은 요청이 슬랙에 올라온 횟수. 1이면 한 번만 올라온 것이다.
    # 목록에서는 view 가 미리 세어 둔다. 없으면 직접 세되 그만큼 쿼리가 늘어난다.
    def get_duplicateCount(self, obj):
        counted = getattr(obj, 'duplicate_count', None)

        return (obj.duplicates.count() if counted is None else counted) + 1


class RelatedRuleSerializer(serializers.Serializer):
    entryId = serializers.IntegerField(source='id')
    title = serializers.CharField()
    bodyKo = serializers.CharField(source='body_ko', allow_null=True)
    bodyEn = serializers.CharField(source='body_en', allow_null=True)
    scopeName = serializers.CharField(source='scope.name')
    # 카드에서도 규칙의 출처를 눌러 원문으로 갈 수 있어야 한다.
    source = serializers.SerializerMethodField()

    @swagger_serializer_method(serializer_or_field=EntrySourceSerializer)
    def get_source(self, obj):
        return entry_source(obj)


class CardQuestionSerializer(serializers.Serializer):
    escalationId = serializers.IntegerField(source='escalation.id')
    questionEn = serializers.CharField(source='escalation.question_en', allow_null=True)
    draftKo = serializers.CharField(source='escalation.draft_ko', allow_null=True)
    status = serializers.CharField(source='escalation.status')
    answerEn = serializers.CharField(source='escalation.answer_en', allow_null=True)
    answerKo = serializers.CharField(source='escalation.answer_ko', allow_null=True)
    sentAt = serializers.DateTimeField(source='escalation.sent_at', allow_null=True)
    answeredAt = serializers.DateTimeField(source='escalation.answered_at', allow_null=True)
    acknowledgedAt = serializers.DateTimeField(
        source='escalation.acknowledged_at', allow_null=True
    )
    blankId = serializers.IntegerField(source='id')


class CardDetailSerializer(CardSerializer):
    documentId = serializers.IntegerField(source='document_id', read_only=True)
    originalText = serializers.CharField(source='document.raw_text', read_only=True, default=None)
    steps = StepSerializer(many=True, read_only=True)
    blanks = BlankSerializer(many=True, read_only=True)
    toneEvidences = ToneEvidenceSerializer(source='tone_evidences', many=True, read_only=True)
    duplicateSources = serializers.SerializerMethodField()
    relatedRules = RelatedRuleSerializer(many=True, read_only=True)
    riskWarnings = RiskWarningSerializer(many=True, read_only=True)
    questions = CardQuestionSerializer(many=True, read_only=True)

    class Meta(CardSerializer.Meta):
        fields = CardSerializer.Meta.fields + [
            'documentId', 'originalText', 'steps', 'blanks', 'toneEvidences',
            'duplicateSources', 'relatedRules', 'riskWarnings', 'questions',
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
    items = CardSerializer(many=True)
    nextCursor = serializers.CharField(allow_null=True)


class CardAskSerializer(serializers.Serializer):
    question = serializers.CharField(max_length=2000, trim_whitespace=True)


class TaskSerializer(serializers.ModelSerializer):
    cardId = serializers.IntegerField(source='card_id', read_only=True)
    scopeId = serializers.IntegerField(source='scope_id', read_only=True)
    scopeName = serializers.CharField(source='scope.name', read_only=True, default=None)
    dueAt = serializers.DateTimeField(source='due_at', read_only=True)
    doneAt = serializers.DateTimeField(source='done_at', read_only=True)
    done = serializers.SerializerMethodField()
    # 카드에서 담긴 것인지 직접 적은 것인지. 화면이 'from 김대표' 와 'self-created' 를 가른다.
    origin = serializers.SerializerMethodField()
    requestedBy = serializers.CharField(
        source='card.document.author_identity.external_handle', read_only=True, default=None
    )
    sourceLabel = serializers.CharField(
        source='card.document.item.label', read_only=True, default=None
    )

    class Meta:
        model = Task
        fields = [
            'id', 'title', 'status', 'done', 'doneAt', 'dueAt',
            'cardId', 'scopeId', 'scopeName', 'origin', 'requestedBy', 'sourceLabel',
        ]

    def get_done(self, obj):
        return obj.status == Task.Status.DONE

    def get_origin(self, obj):
        return 'CARD' if obj.card_id else 'SELF'


class TaskListSerializer(serializers.Serializer):
    items = TaskSerializer(many=True)


class TaskCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True)
    dueAt = serializers.DateTimeField(source='due_at', required=False, allow_null=True)
    scopeId = serializers.IntegerField(source='scope_id', required=False, allow_null=True)

    def validate_scopeId(self, value):
        if value is None:
            return None

        if not CompanyScope.objects.filter(
            id=value, company=self.context['company']
        ).exists():
            raise serializers.ValidationError('scope not found', code=SCOPE_NOT_FOUND)

        return value

    def create(self, validated_data):
        return Task.objects.create(
            company=self.context['company'], user=self.context['user'], **validated_data
        )


class TaskUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=200, trim_whitespace=True, required=False)
    status = serializers.ChoiceField(choices=Task.Status.choices, required=False)
    dueAt = serializers.DateTimeField(source='due_at', required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                'title, status or dueAt is required', code=TASK_FIELD_REQUIRED
            )

        return attrs

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        # 체크하면 끝낸 시각을 남긴다. 이 값으로 하루 뒤에 지운다.
        if 'status' in validated_data:
            instance.done_at = (
                timezone.now() if instance.status == Task.Status.DONE else None
            )
            validated_data['done_at'] = instance.done_at
        instance.save(update_fields=list(validated_data))

        return instance


class ReadTodaySerializer(serializers.Serializer):
    messages = serializers.IntegerField()
    cards = serializers.IntegerField()
    waiting = serializers.IntegerField()


class UnreadInstructionSerializer(serializers.Serializer):
    cardId = serializers.IntegerField()
    purpose = serializers.CharField()
    text = serializers.CharField(allow_null=True)
    requestedBy = serializers.CharField(allow_null=True)
    occurredAt = serializers.DateTimeField(allow_null=True)


class UnreadSerializer(serializers.Serializer):
    count = serializers.IntegerField()
    latest = UnreadInstructionSerializer(allow_null=True)


class ResolutionSerializer(serializers.Serializer):
    answered = serializers.IntegerField()
    total = serializers.IntegerField()
    since = serializers.DateTimeField()

    class Meta:
        # companies.ResolutionSerializer와 클래스 이름이 같다. ref_name을 안 주면
        # 둘 다 'Resolution'으로 등록돼서 스키마 생성이 통째로 실패한다.
        ref_name = 'HomeResolution'


class HandbookGrowthSerializer(serializers.Serializer):
    confirmed = serializers.IntegerField()
    addedThisMonth = serializers.IntegerField()
    weekly = serializers.ListField(child=serializers.IntegerField())
    scopes = CompanyScopeSerializer(many=True)


class HomeSerializer(serializers.Serializer):
    readToday = ReadTodaySerializer()
    unread = UnreadSerializer()
    todos = TaskSerializer(many=True)
    resolution = ResolutionSerializer()
    handbook = HandbookGrowthSerializer()


class PersonTimingSerializer(serializers.Serializer):
    name = serializers.CharField(allow_null=True)
    timezone = serializers.CharField()
    localNow = serializers.DateTimeField()
    state = serializers.ChoiceField(choices=STATES)
    available = serializers.BooleanField()


class WorkingHoursSerializer(serializers.Serializer):
    enabled = serializers.BooleanField()
    start = serializers.TimeField()
    end = serializers.TimeField()
    timezone = serializers.CharField()


class ReplyExpectedSerializer(serializers.Serializer):
    at = serializers.DateTimeField()
    basis = serializers.ChoiceField(choices=BASES)
    sampleSize = serializers.IntegerField()


class CanDoSerializer(serializers.Serializer):
    cardId = serializers.IntegerField()
    stepId = serializers.IntegerField()
    title = serializers.CharField()
    entryId = serializers.IntegerField(allow_null=True)
    entryTitle = serializers.CharField(allow_null=True)
    scopeName = serializers.CharField(allow_null=True)


class NeedsPersonSerializer(serializers.Serializer):
    cardId = serializers.IntegerField()
    blankId = serializers.IntegerField()
    title = serializers.CharField()
    scopeName = serializers.CharField(allow_null=True)
    escalationStatus = serializers.CharField(allow_null=True)


class TimingSerializer(serializers.Serializer):
    now = serializers.DateTimeField()
    you = PersonTimingSerializer()
    owner = PersonTimingSerializer()
    workingHours = WorkingHoursSerializer()
    replyExpected = ReplyExpectedSerializer()
    canDo = CanDoSerializer(many=True)
    canDoTotal = serializers.IntegerField()
    needsPerson = NeedsPersonSerializer(many=True)
    needsPersonTotal = serializers.IntegerField()


# 담당자는 슬랙 멘션으로 자동 지정된다. 멘션이 없으면 비어 있고, 잘못 잡히기도 한다.
# 사람이 고칠 수 없으면 그 카드는 아무의 일도 아닌 채로 남는다.
class CardUpdateSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=InstructionCard.Status.choices, required=False)
    assigneeId = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                'status or assigneeId is required', code=CARD_FIELD_REQUIRED
            )

        return attrs

    def validate_assigneeId(self, value):
        if value is None:
            return None

        membership = Membership.objects.filter(
            user_id=value, company=self.context['company'], left_at__isnull=True
        ).select_related('user').first()
        if membership is None:
            raise serializers.ValidationError('not a member of this company', code=NOT_A_MEMBER)

        return membership.user

    def update(self, instance, validated_data):
        fields = []
        if 'status' in validated_data:
            instance.status = validated_data['status']
            fields.append('status')
        if 'assigneeId' in validated_data:
            instance.assignee = validated_data['assigneeId']
            fields.append('assignee')

        instance.save(update_fields=fields)

        return instance
