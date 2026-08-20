from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rest_framework import serializers

from .errors import UNKNOWN_TIMEZONE


# 타임존은 화면에 뜨는 시각을 계산하는 근거다. 검증 없이 받으면 저장은 되고
# 시차를 계산하는 시점에 터진다.
class TimeZoneField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault('max_length', 40)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data).strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise serializers.ValidationError('unknown timezone', code=UNKNOWN_TIMEZONE)

        return value
