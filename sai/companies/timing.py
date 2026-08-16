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


# 대표 근무시간을 다른 지역의 벽시계로 읽는다.
# 하노이에서 서울 09:00~18:00 은 07:00~16:00 이다. 자정을 넘길 수도 있다.
def local_window(hours, zone, moment):
    begin, end = _window(hours, moment.astimezone(hours.zone).date())
    there = ZoneInfo(zone)

    return begin.astimezone(there).time(), end.astimezone(there).time()


# 양쪽이 같은 근무시간을 각자의 지역에서 쓴다고 볼 때 하루에 겹치는 시간.
# 시차 때문에 상대의 근무일이 하루 밀리거나 당겨질 수 있어 앞뒤 하루까지 본다.
def overlap_hours(hours, zone, moment):
    begin, end = _window(hours, moment.astimezone(hours.zone).date())
    theirs = WorkingHours(zone, hours.start, hours.end, hours.enabled)

    total = timedelta()
    day = begin.astimezone(theirs.zone).date() - DAY
    for _ in range(3):
        start, finish = _window(theirs, day)
        shared = min(end, finish) - max(begin, start)
        if shared > timedelta():
            total += shared
        day += DAY

    return total.total_seconds() / 3600


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
