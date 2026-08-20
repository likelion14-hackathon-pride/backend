from django.db import models


# 근무 위치. 고른 위치가 타임존을 정하고, 시각 계산은 전부 타임존만 읽는다.
# 목록에 없는 곳은 고를 수 없다. 임의 UTC 오프셋을 받으면 뉴욕처럼 서머타임을 쓰는 곳에서
# 한 해의 절반이 한 시간씩 틀리기 때문이다.
class WorkLocation(models.TextChoices):
    HANOI = 'HANOI', 'Hanoi'
    DA_NANG = 'DA_NANG', 'Da Nang'
    JAKARTA = 'JAKARTA', 'Jakarta'
    NEW_YORK = 'NEW_YORK', 'New York'
    SEOUL = 'SEOUL', 'Seoul'
    TOKYO = 'TOKYO', 'Tokyo'


LOCATION_ZONES = {
    WorkLocation.HANOI: 'Asia/Ho_Chi_Minh',
    WorkLocation.DA_NANG: 'Asia/Ho_Chi_Minh',
    WorkLocation.JAKARTA: 'Asia/Jakarta',
    WorkLocation.NEW_YORK: 'America/New_York',
    WorkLocation.SEOUL: 'Asia/Seoul',
    WorkLocation.TOKYO: 'Asia/Tokyo',
}


class JobRole(models.TextChoices):
    BACKEND = 'BACKEND', 'Backend'
    FRONTEND = 'FRONTEND', 'Frontend'
    DESIGN = 'DESIGN', 'Design'
    PM = 'PM', 'PM'
    QA = 'QA', 'QA'
    DATA = 'DATA', 'Data'


def zone_of(location):
    return LOCATION_ZONES[WorkLocation(location)]
