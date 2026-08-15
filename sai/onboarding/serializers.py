from rest_framework import serializers

from .models import Question


class OnboardingQuestionSerializer(serializers.Serializer):
    templateKey = serializers.CharField()
    category = serializers.CharField()
    title = serializers.CharField()
    question = serializers.CharField()
    placeholder = serializers.CharField(allow_null=True)
    # 비어 있으면 자유 입력만 받는 질문이다.
    options = serializers.ListField(child=serializers.CharField())
    status = serializers.CharField()
    answerKo = serializers.CharField(allow_null=True)
    entryId = serializers.IntegerField(allow_null=True)


class OnboardingQuestionUpdateSerializer(serializers.Serializer):
    answerKo = serializers.CharField(required=False, allow_blank=False, max_length=10000)
    # 프로젝트 질문일 때만 보낸다. 어느 프로젝트의 답인지 가린다.
    scopeId = serializers.IntegerField(required=False, allow_null=True)
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


class OnboardingSerializer(serializers.Serializer):
    onboardingStep = serializers.IntegerField()
    questions = OnboardingQuestionSerializer(many=True)


class OnboardingStepSerializer(serializers.Serializer):
    onboardingStep = serializers.IntegerField(min_value=1, max_value=4)


class OnboardingSummarySerializer(serializers.Serializer):
    sourceCount = serializers.IntegerField()
    handbookEntryCount = serializers.IntegerField()
    riskKeywordCount = serializers.IntegerField()


class OnboardingCompleteSerializer(serializers.Serializer):
    onboardingStep = serializers.IntegerField()
    nextRoute = serializers.CharField()
    summary = OnboardingSummarySerializer()
