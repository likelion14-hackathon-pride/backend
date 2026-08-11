from rest_framework import serializers

from .models import Company


# 회사 근무시간 설정용 시리얼라이저
class CompanySettingsSerializer(serializers.ModelSerializer):
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
