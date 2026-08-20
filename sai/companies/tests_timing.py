from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from .timing import (
    OFF_HOURS,
    UNKNOWN,
    WORKING,
    WorkingHours,
    add_working_minutes,
    local_window,
    next_start,
    overlap_hours,
    state,
    working_minutes_between,
)

SEOUL = ZoneInfo('Asia/Seoul')
HANOI = ZoneInfo('Asia/Ho_Chi_Minh')

# 2026-08-12 수 / 08-14 금 / 08-15 토 / 08-17 월
DAY_HOURS = WorkingHours('Asia/Seoul', time(9, 0), time(18, 0))
NIGHT_HOURS = WorkingHours('Asia/Seoul', time(22, 0), time(6, 0))
ALWAYS = WorkingHours('Asia/Seoul', time(9, 0), time(18, 0), enabled=False)


def seoul(day, hour, minute=0):
    return datetime(2026, 8, day, hour, minute, tzinfo=SEOUL)


class StateTests(SimpleTestCase):
    def test_inside_hours(self):
        self.assertEqual(state(DAY_HOURS, seoul(12, 14)), WORKING)

    def test_before_hours(self):
        self.assertEqual(state(DAY_HOURS, seoul(12, 8)), OFF_HOURS)

    # 목업의 21:40. 근무가 끝난 시각이다.
    def test_after_hours(self):
        self.assertEqual(state(DAY_HOURS, seoul(12, 21, 40)), OFF_HOURS)

    def test_end_is_not_working(self):
        self.assertEqual(state(DAY_HOURS, seoul(12, 18)), OFF_HOURS)

    def test_weekend(self):
        self.assertEqual(state(DAY_HOURS, seoul(15, 14)), OFF_HOURS)

    # 자정을 넘기는 근무시간. 새벽 2시는 전날 밤에 시작한 구간 안이다.
    def test_overnight_hours_after_midnight(self):
        self.assertEqual(state(NIGHT_HOURS, seoul(13, 2)), WORKING)

    def test_overnight_hours_in_the_afternoon(self):
        self.assertEqual(state(NIGHT_HOURS, seoul(12, 14)), OFF_HOURS)

    # 근무시간을 안 쓰는 회사는 근무 중인지 판단하지 않는다.
    def test_disabled(self):
        self.assertEqual(state(ALWAYS, seoul(12, 3)), UNKNOWN)

    # 같은 순간을 각자의 벽시계로 읽는다. 하노이 17시는 서울 19시라 이미 퇴근이다.
    def test_same_hours_in_another_timezone(self):
        hanoi = WorkingHours('Asia/Ho_Chi_Minh', time(9, 0), time(18, 0))
        moment = datetime(2026, 8, 12, 17, 0, tzinfo=HANOI)

        self.assertEqual(state(hanoi, moment), WORKING)
        self.assertEqual(state(DAY_HOURS, moment), OFF_HOURS)


class NextStartTests(SimpleTestCase):
    def test_already_working(self):
        moment = seoul(12, 14)

        self.assertEqual(next_start(DAY_HOURS, moment), moment)

    def test_evening_rolls_to_next_morning(self):
        self.assertEqual(next_start(DAY_HOURS, seoul(12, 21, 40)), seoul(13, 9))

    def test_before_opening_waits_for_opening(self):
        self.assertEqual(next_start(DAY_HOURS, seoul(12, 7)), seoul(12, 9))

    # 금요일 밤에 물어보면 월요일 아침이다.
    def test_friday_night_rolls_to_monday(self):
        self.assertEqual(next_start(DAY_HOURS, seoul(14, 21)), seoul(17, 9))

    def test_disabled_is_now(self):
        moment = seoul(15, 3)

        self.assertEqual(next_start(ALWAYS, moment), moment)


class WorkingMinutesTests(SimpleTestCase):
    def test_within_one_day(self):
        self.assertEqual(
            working_minutes_between(DAY_HOURS, seoul(12, 10), seoul(12, 12)), 120
        )

    # 밤은 빼고 센다. 17시에 물어 다음 날 10시에 답을 받으면 2시간이다.
    def test_night_is_not_counted(self):
        self.assertEqual(
            working_minutes_between(DAY_HOURS, seoul(12, 17), seoul(13, 10)), 120
        )

    # 주말도 빼고 센다. 금요일 17시 -> 월요일 10시 역시 2시간이다.
    def test_weekend_is_not_counted(self):
        self.assertEqual(
            working_minutes_between(DAY_HOURS, seoul(14, 17), seoul(17, 10)), 120
        )

    def test_entirely_outside_hours(self):
        self.assertEqual(
            working_minutes_between(DAY_HOURS, seoul(12, 20), seoul(12, 22)), 0
        )

    def test_backwards_is_zero(self):
        self.assertEqual(
            working_minutes_between(DAY_HOURS, seoul(12, 14), seoul(12, 10)), 0
        )

    def test_disabled_counts_everything(self):
        self.assertEqual(
            working_minutes_between(ALWAYS, seoul(12, 20), seoul(12, 22)), 120
        )


class LocalWindowTests(SimpleTestCase):
    # 하노이는 서울보다 두 시간 느리다. 대표의 09~18 이 07~16 으로 읽힌다.
    def test_owner_hours_read_from_hanoi(self):
        self.assertEqual(
            local_window(DAY_HOURS, 'Asia/Ho_Chi_Minh', seoul(12, 12)),
            (time(7, 0), time(16, 0)),
        )

    def test_same_zone_is_unchanged(self):
        self.assertEqual(
            local_window(DAY_HOURS, 'Asia/Seoul', seoul(12, 12)),
            (time(9, 0), time(18, 0)),
        )

    # 뉴욕에서는 대표 근무시간이 밤에 걸린다. 자정을 넘겨도 값이 나와야 한다.
    def test_owner_hours_read_from_new_york(self):
        start, end = local_window(DAY_HOURS, 'America/New_York', seoul(12, 12))

        self.assertEqual((start, end), (time(20, 0), time(5, 0)))

    # 뉴욕은 서머타임을 쓴다. 정수 오프셋으로 저장했다면 겨울에 한 시간 틀렸을 값이다.
    def test_new_york_shifts_in_winter(self):
        winter = datetime(2026, 1, 14, 12, 0, tzinfo=SEOUL)

        self.assertEqual(
            local_window(DAY_HOURS, 'America/New_York', winter),
            (time(19, 0), time(4, 0)),
        )


class OverlapTests(SimpleTestCase):
    def test_same_zone_overlaps_entirely(self):
        self.assertEqual(overlap_hours(DAY_HOURS, 'Asia/Seoul', seoul(12, 12)), 9)

    # 서울 09~18 은 하노이 07~16. 하노이 사람의 09~18 과 겹치는 구간은 09~16 이다.
    def test_hanoi_overlaps_seven_hours(self):
        self.assertEqual(overlap_hours(DAY_HOURS, 'Asia/Ho_Chi_Minh', seoul(12, 12)), 7)

    def test_tokyo_overlaps_entirely(self):
        self.assertEqual(overlap_hours(DAY_HOURS, 'Asia/Tokyo', seoul(12, 12)), 9)

    # 반대편 지구와는 겹치는 시간이 없다. 이때 물으면 다음 근무일까지 기다린다.
    def test_new_york_does_not_overlap(self):
        self.assertEqual(overlap_hours(DAY_HOURS, 'America/New_York', seoul(12, 12)), 0)

    def test_jakarta_overlaps_seven_hours(self):
        self.assertEqual(overlap_hours(DAY_HOURS, 'Asia/Jakarta', seoul(12, 12)), 7)


class AddWorkingMinutesTests(SimpleTestCase):
    def test_within_one_day(self):
        self.assertEqual(
            add_working_minutes(DAY_HOURS, seoul(12, 10), 120), seoul(12, 12)
        )

    # 남은 근무시간보다 길면 다음 날 아침으로 넘어간다.
    def test_spills_into_the_next_morning(self):
        self.assertEqual(
            add_working_minutes(DAY_HOURS, seoul(12, 17), 120), seoul(13, 10)
        )

    # 퇴근 후에 더하면 다음 근무 시작부터 센다. 목업의 Tomorrow 11:00 이 이것이다.
    def test_after_hours_starts_from_the_next_opening(self):
        self.assertEqual(
            add_working_minutes(DAY_HOURS, seoul(12, 21, 40), 120), seoul(13, 11)
        )

    def test_friday_night_spills_to_monday(self):
        self.assertEqual(
            add_working_minutes(DAY_HOURS, seoul(14, 21), 120), seoul(17, 11)
        )

    def test_zero_is_the_next_opening(self):
        self.assertEqual(add_working_minutes(DAY_HOURS, seoul(12, 21), 0), seoul(13, 9))

    def test_disabled_adds_plain_time(self):
        self.assertEqual(add_working_minutes(ALWAYS, seoul(12, 21), 120), seoul(12, 23))
