from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Membership, User

from .models import Company


class SettingsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.owner = User.objects.create_user(
            email='o@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.url = f'/api/companies/{self.company.id}/settings'

    def patch(self, payload):
        return self.client.patch(self.url, payload, format='json')

    def test_timezone_can_be_changed(self):
        self.assertEqual(self.patch({'timezone': 'Asia/Tokyo'}).data['timezone'], 'Asia/Tokyo')

    def test_unknown_timezone_is_rejected(self):
        response = self.patch({'timezone': 'Seoul'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['field'], 'timezone')

    def test_working_hours_can_be_changed(self):
        response = self.patch({'workingHoursStart': '10:00', 'workingHoursEnd': '19:00'})

        self.assertEqual(response.data['workingHoursStart'], '10:00:00')
        self.assertEqual(response.data['workingHoursEnd'], '19:00:00')

    # 자정을 넘기는 근무시간은 정상이다.
    def test_overnight_working_hours_are_allowed(self):
        response = self.patch({'workingHoursStart': '22:00', 'workingHoursEnd': '06:00'})

        self.assertEqual(response.status_code, 200)

    # 길이가 0인 근무시간은 '다음 근무 시작'을 영영 찾지 못하게 만든다.
    def test_equal_start_and_end_is_rejected(self):
        response = self.patch({'workingHoursStart': '09:00', 'workingHoursEnd': '09:00'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['field'], 'workingHoursEnd')

    # 한쪽만 보내도 이미 저장된 값과 비교해야 한다.
    def test_partial_patch_still_compares_with_the_stored_value(self):
        self.assertEqual(self.patch({'workingHoursEnd': '09:00'}).status_code, 400)

    def test_member_cannot_change_settings(self):
        member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh'
        )
        Membership.objects.create(
            user=member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client.force_authenticate(user=member)

        self.assertEqual(self.patch({'timezone': 'Asia/Tokyo'}).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 200)
