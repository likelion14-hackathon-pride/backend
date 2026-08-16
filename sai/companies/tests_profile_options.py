from datetime import time

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Membership, User

from .models import Company


class ProfileOptionsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh', ui_language='en'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.url = f'/api/companies/{self.company.id}/profile-options'

    def options(self):
        return self.client.get(self.url).data

    def location(self, value):
        return next(l for l in self.options()['locations'] if l['value'] == value)

    # --- 목록 ---

    def test_locations_are_a_fixed_list(self):
        values = [l['value'] for l in self.options()['locations']]

        self.assertEqual(
            values, ['HANOI', 'DA_NANG', 'JAKARTA', 'NEW_YORK', 'SEOUL', 'TOKYO']
        )

    def test_roles_are_a_fixed_list(self):
        values = [r['value'] for r in self.options()['roles']]

        self.assertEqual(values, ['BACKEND', 'FRONTEND', 'DESIGN', 'PM', 'QA', 'DATA'])

    def test_labels_are_readable(self):
        self.assertEqual(self.location('DA_NANG')['label'], 'Da Nang')

    # 프론트가 도시 목록을 따로 들고 있으면 서버 매핑과 어긋난다.
    def test_each_location_carries_its_timezone(self):
        self.assertEqual(self.location('HANOI')['timezone'], 'Asia/Ho_Chi_Minh')
        self.assertEqual(self.location('NEW_YORK')['timezone'], 'America/New_York')

    # --- 겹치는 시간 ---

    # 대표 근무시간을 그 위치의 시계로 읽은 값이 그대로 화면 문구가 된다.
    def test_owner_hours_are_read_in_local_time(self):
        hanoi = self.location('HANOI')

        self.assertEqual(hanoi['ownerHoursStart'], '07:00:00')
        self.assertEqual(hanoi['ownerHoursEnd'], '16:00:00')

    def test_seoul_reads_the_owner_hours_unchanged(self):
        seoul = self.location('SEOUL')

        self.assertEqual(seoul['ownerHoursStart'], '09:00:00')
        self.assertEqual(seoul['overlapHours'], 9)

    def test_hanoi_overlaps_seven_hours(self):
        self.assertEqual(self.location('HANOI')['overlapHours'], 7)

    # 뉴욕에서는 대표 근무시간과 겹치는 구간이 없다.
    def test_new_york_does_not_overlap(self):
        self.assertEqual(self.location('NEW_YORK')['overlapHours'], 0)

    # 회사 근무시간을 바꾸면 목록도 따라간다. 값이 굳어 있으면 안 된다.
    def test_the_list_follows_the_company_working_hours(self):
        Company.objects.filter(id=self.company.id).update(
            working_hours_start=time(11, 0), working_hours_end=time(20, 0)
        )

        self.assertEqual(self.location('HANOI')['ownerHoursStart'], '09:00:00')

    # --- 접근 권한 ---

    def test_outsider_is_rejected(self):
        outsider = User.objects.create_user(
            email='x@example.com', password='pw', display_name='X'
        )
        self.client.force_authenticate(user=outsider)

        self.assertEqual(self.client.get(self.url).status_code, 403)
