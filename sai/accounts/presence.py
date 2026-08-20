from datetime import timedelta

from django.utils import timezone

from companies.timing import WORKING, WorkingHours, state

# 이 시간 안에 요청이 있었으면 접속 중으로 본다.
# 화면을 열어 둔 채 읽고만 있어도 접속 중이어야 하므로, 브라우저가 아무 요청도
# 보내지 않는 구간을 견딜 만큼 길게 잡는다.
ONLINE_WITHIN = timedelta(minutes=5)

# 마지막 접속 시각을 다시 쓰기까지의 간격.
# 요청마다 쓰면 화면 한 번 여는 데 UPDATE 가 수십 번 나간다.
TOUCH_EVERY = timedelta(minutes=1)


def _company_is_working(company, now):
    hours = WorkingHours(
        company.timezone,
        company.working_hours_start,
        company.working_hours_end,
        company.working_hours_enabled,
    )

    return state(hours, now) == WORKING


def is_online(user, now=None, company=None):
    now = now or timezone.now()
    if (
        company is not None
        and company.working_hours_enabled
        and _company_is_working(company, now)
    ):
        return True

    if user is None or user.last_seen_at is None:
        return False

    return now - user.last_seen_at <= ONLINE_WITHIN


# 인증에 성공할 때마다 불린다. 실제 쓰기는 TOUCH_EVERY 마다 한 번만 한다.
def touch(user, now=None):
    now = now or timezone.now()
    if user.last_seen_at and now - user.last_seen_at < TOUCH_EVERY:
        return False

    type(user).objects.filter(pk=user.pk).update(last_seen_at=now)
    user.last_seen_at = now

    return True
