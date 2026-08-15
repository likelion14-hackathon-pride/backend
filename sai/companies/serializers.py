from rest_framework import serializers

from .models import Company


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
