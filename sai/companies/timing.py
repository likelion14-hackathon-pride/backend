from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

WORKING = 'WORKING'
OFF_HOURS = 'OFF_HOURS'
UNKNOWN = 'UNKNOWN'
STATES = (WORKING, OFF_HOURS, UNKNOWN)

# 회사별 근무 요일 설정은 아직 없다. 토요일을 근무일로 세는 것보다 월~금 고정이 덜 틀린다.
WORKING_WEEKDAYS = frozenset({0, 1, 2, 3, 4})

DAY = timedelta(days=1)

# 다음 근무 구간을 찾을 때 며칠까지 걸어볼지. 연휴가 아무리 길어도 2주면 닿는다.
MAX_DAYS = 14


@dataclass(frozen=True)
class WorkingHours:
    timezone: str
    start: time
    end: time
    enabled: bool = True

    @property
    def zone(self):
        return ZoneInfo(self.timezone)

    # 자정을 넘기는 근무시간(22:00~06:00)도 길이로 보면 한 구간이다.
    # 시작 시각에 이 길이를 더하는 식으로 두 경우를 함께 다룬다.
    @property
    def length(self):
        return (_offset(self.end) - _offset(self.start)) % DAY


def _offset(value):
    return timedelta(hours=value.hour, minutes=value.minute, seconds=value.second)


# day(로컬 날짜)에 시작하는 근무 구간.
def _window(hours, day):
    begin = datetime.combine(day, hours.start, hours.zone)

    return begin, begin + hours.length


# 어제 시작해 아직 안 끝난 구간이 있을 수 있어 하루 앞에서부터 걷는다.
def _windows(hours, moment, days=MAX_DAYS):
    day = moment.astimezone(hours.zone).date() - DAY
    for _ in range(days + 1):
        if day.weekday() in WORKING_WEEKDAYS:
            yield _window(hours, day)
        day += DAY


def state(hours, moment):
    if not hours.enabled:
        return UNKNOWN

    for begin, end in _windows(hours, moment, days=1):
        if begin <= moment < end:
            return WORKING

    return OFF_HOURS


# moment 이후 처음으로 일이 시작되는 시각. 이미 근무 중이면 moment 그대로다.
def next_start(hours, moment):
    if not hours.enabled:
        return moment

    for begin, end in _windows(hours, moment):
        if begin <= moment < end:
            return moment
        if begin > moment:
            return begin

    return moment


# a 부터 b 까지 중 근무시간에 해당하는 분. 밤과 주말은 세지 않는다.
def working_minutes_between(hours, a, b):
    if b <= a:
        return 0.0
    if not hours.enabled:
        return (b - a).total_seconds() / 60

    total = timedelta()
    day = a.astimezone(hours.zone).date() - DAY
    last = b.astimezone(hours.zone).date() + DAY
    while day <= last:
        if day.weekday() in WORKING_WEEKDAYS:
            begin, end = _window(hours, day)
            overlap = min(end, b) - max(begin, a)
            if overlap > timedelta():
                total += overlap
        day += DAY

    return total.total_seconds() / 60


# moment 에서 근무시간으로만 minutes 만큼 흐른 뒤의 시각.
def add_working_minutes(hours, moment, minutes):
    if not hours.enabled:
        return moment + timedelta(minutes=minutes)

    left = timedelta(minutes=minutes)
    for begin, end in _windows(hours, moment):
        cursor = max(begin, moment)
        if cursor >= end:
            continue
        if end - cursor >= left:
            return cursor + left
        left -= end - cursor

    return moment + timedelta(minutes=minutes)
