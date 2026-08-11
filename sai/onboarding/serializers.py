from rest_framework import serializers

from .models import Question


# 온보딩 질문 응답용 시리얼라이저
class OnboardingQuestionSerializer(serializers.ModelSerializer):
    scopeId = serializers.IntegerField(source='scope_id', read_only=True, allow_null=True)
    questionKo = serializers.CharField(source='question_ko', read_only=True)
    reasonText = serializers.CharField(source='reason_text', read_only=True, allow_null=True)
    answerKo = serializers.CharField(source='answer_ko', read_only=True, allow_null=True)
    createdEntryId = serializers.IntegerField(source='created_entry_id', read_only=True, allow_null=True)

    class Meta:
        model = Question
        fields = [
            'id',
            'scopeId',
            'questionKo',
            'reasonText',
            'hint',
            'placeholder',
            'priority',
            'status',
            'answerKo',
            'createdEntryId',
        ]


# 온보딩 질문 답변 저장용 시리얼라이저
class OnboardingQuestionUpdateSerializer(serializers.Serializer):
    answerKo = serializers.CharField(required=False, allow_blank=False, max_length=10000)
    status = serializers.ChoiceField(
        choices=[Question.Status.ANSWERED, Question.Status.SKIPPED],
        required=False,
    )

    def validate(self, attrs):
        answer = attrs.get('answerKo')
        question_status = attrs.get('status')

        if not answer and question_status != Question.Status.SKIPPED:
            raise serializers.ValidationError('answerKo or SKIPPED status is required')
        if question_status == Question.Status.ANSWERED and not answer:
            raise serializers.ValidationError('answerKo is required for ANSWERED status')

        return attrs


# 온보딩 진행 상태 응답용 시리얼라이저
class OnboardingSerializer(serializers.Serializer):
    onboardingStep = serializers.IntegerField()
    questions = OnboardingQuestionSerializer(many=True)


# 온보딩 단계 저장용 시리얼라이저
class OnboardingStepSerializer(serializers.Serializer):
    onboardingStep = serializers.IntegerField(min_value=1, max_value=4)
