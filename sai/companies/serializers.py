from rest_framework import serializers

from config.fields import TimeZoneField

from .models import Company


class WeeklyQuestionsSerializer(serializers.Serializer):
    totalCount = serializers.IntegerField()
    saiAnsweredCount = serializers.IntegerField()
    ownerRequiredCount = serializers.IntegerField()


class ResolutionSerializer(serializers.Serializer):
    saiCount = serializers.IntegerField()
    ownerCount = serializers.IntegerField()
    saiRate = serializers.FloatField()
    ownerRate = serializers.FloatField()

    class Meta:
        # cards.ResolutionSerializer와 클래스 이름이 같다. ref_name을 안 주면
        # 둘 다 'Resolution'으로 등록돼서 스키마 생성이 통째로 실패한다.
        ref_name = 'DashboardResolution'


class ReusedEntrySerializer(serializers.Serializer):
    entryId = serializers.IntegerField()
    title = serializers.CharField()
    reuseCount = serializers.IntegerField()


class AnswerReuseSerializer(serializers.Serializer):
    averageCount = serializers.FloatField()
    totalCount = serializers.IntegerField()
    topEntries = ReusedEntrySerializer(many=True)


class WeeklySavedTimeSerializer(serializers.Serializer):
    weekStart = serializers.DateField()
    minutes = serializers.IntegerField()


class OwnerTimeSavedSerializer(serializers.Serializer):
    minutes = serializers.IntegerField()
    changeMinutes = serializers.IntegerField()
    minutesPerAnswer = serializers.IntegerField()
    weeklyTrend = WeeklySavedTimeSerializer(many=True)


class MonthlyHandbookCountSerializer(serializers.Serializer):
    month = serializers.CharField()
    count = serializers.IntegerField()


class DashboardHandbookSerializer(serializers.Serializer):
    totalCount = serializers.IntegerField()
    thisWeekCount = serializers.IntegerField()
    lastConfirmedAt = serializers.DateTimeField(allow_null=True)
    monthlyTrend = MonthlyHandbookCountSerializer(many=True)


class RecentAnswerSerializer(serializers.Serializer):
    question = serializers.CharField()
    answer = serializers.CharField()
    resolutionType = serializers.ChoiceField(choices=['SAI', 'OWNER'])
    sourceLabels = serializers.ListField(child=serializers.CharField())
    resolvedAt = serializers.DateTimeField()


class RecentAnswerListSerializer(serializers.Serializer):
    todayCount = serializers.IntegerField()
    items = RecentAnswerSerializer(many=True)


class WaitingQuestionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    question = serializers.CharField()
    askedByName = serializers.CharField()
    scopeName = serializers.CharField(allow_null=True)
    status = serializers.CharField()
    createdAt = serializers.DateTimeField()


class WaitingQuestionListSerializer(serializers.Serializer):
    totalCount = serializers.IntegerField()
    items = WaitingQuestionSerializer(many=True)


class OwnerDashboardSerializer(serializers.Serializer):
    weeklyQuestions = WeeklyQuestionsSerializer()
    resolution = ResolutionSerializer()
    answerReuse = AnswerReuseSerializer()
    ownerTimeSaved = OwnerTimeSavedSerializer()
    handbook = DashboardHandbookSerializer()
    recentAnswers = RecentAnswerListSerializer()
    waitingQuestions = WaitingQuestionListSerializer()


class CompanySettingsSerializer(serializers.ModelSerializer):
    timezone = TimeZoneField()
    workingHoursEnabled = serializers.BooleanField(source='working_hours_enabled')
    workingHoursStart = serializers.TimeField(source='working_hours_start')
    workingHoursEnd = serializers.TimeField(source='working_hours_end')

    class Meta:
        model = Company
        fields = [
            'timezone',
            'workingHoursEnabled',
            'workingHoursStart',
            'workingHoursEnd',
        ]

    # 시작과 끝이 같으면 근무시간의 길이가 0이 된다. 그러면 '다음 근무 시작'을
    # 영영 찾지 못한다. 자정을 넘기는 근무시간(22:00~06:00)은 정상이므로 막지 않는다.
    def validate(self, attrs):
        instance = self.instance
        start = attrs.get('working_hours_start', instance and instance.working_hours_start)
        end = attrs.get('working_hours_end', instance and instance.working_hours_end)
        if start == end:
            raise serializers.ValidationError(
                {'workingHoursEnd': ['must differ from workingHoursStart']}
            )

        return attrs


class WorkLocationSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()
    timezone = serializers.CharField()
    # 대표 근무시간을 이 위치의 벽시계로 읽은 값.
    ownerHoursStart = serializers.TimeField()
    ownerHoursEnd = serializers.TimeField()
    # 양쪽이 같은 근무시간을 쓴다고 볼 때 하루에 겹치는 시간.
    overlapHours = serializers.FloatField()


class JobRoleSerializer(serializers.Serializer):
    value = serializers.CharField()
    label = serializers.CharField()


class ProfileOptionsSerializer(serializers.Serializer):
    locations = WorkLocationSerializer(many=True)
    roles = JobRoleSerializer(many=True)
