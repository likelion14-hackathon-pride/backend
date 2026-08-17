from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from companies.models import Company

from .models import Membership, User
from .presence import ONLINE_WITHIN, TOUCH_EVERY, is_online, touch


class PresenceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.owner = User.objects.create_user(
            email='owner@example.com', password='sai-pw-2026!', display_name='김대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='sai-pw-2026!', display_name='Alex'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client = APIClient()

    # --- 판정 ---

    def test_a_user_who_never_visited_is_offline(self):
        self.assertFalse(is_online(self.member))

    def test_a_recent_visit_is_online(self):
        self.member.last_seen_at = timezone.now()

        self.assertTrue(is_online(self.member))

    def test_an_old_visit_is_offline(self):
        self.member.last_seen_at = timezone.now() - ONLINE_WITHIN - timedelta(seconds=1)

        self.assertFalse(is_online(self.member))

    # --- 기록 ---

    def test_the_first_request_records_the_time(self):
        self.assertTrue(touch(self.member))
        self.member.refresh_from_db()
        self.assertIsNotNone(self.member.last_seen_at)

    # 요청마다 쓰면 화면 한 번 여는 데 UPDATE 가 수십 번 나간다.
    def test_a_second_request_does_not_write_again(self):
        touch(self.member)
        written_at = self.member.last_seen_at

        self.assertFalse(touch(self.member))
        self.member.refresh_from_db()
        self.assertEqual(self.member.last_seen_at, written_at)

    def test_it_writes_again_after_the_interval(self):
        self.member.last_seen_at = timezone.now() - TOUCH_EVERY - timedelta(seconds=1)
        self.member.save(update_fields=['last_seen_at'])

        self.assertTrue(touch(self.member))

    # --- 화면에 나가는 값 ---

    # force_authenticate 는 인증 클래스를 건너뛴다. 실제 토큰으로 불러야
    # 마지막 접속 시각이 기록되는 경로가 검증된다.
    def test_calling_the_api_marks_me_online(self):
        token = RefreshToken.for_user(self.owner)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token.access_token}')

        response = self.client.get('/api/me')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['user']['online'])
        self.owner.refresh_from_db()
        self.assertIsNotNone(self.owner.last_seen_at)

    def test_the_member_list_shows_who_is_online(self):
        self.member.last_seen_at = timezone.now()
        self.member.save(update_fields=['last_seen_at'])
        self.client.force_authenticate(user=self.owner)

        response = self.client.get(f'/api/companies/{self.company.id}/members')

        by_id = {item['user']['id']: item['user'] for item in response.data['items']}
        self.assertTrue(by_id[self.member.id]['online'])
        self.assertIsNotNone(by_id[self.member.id]['lastSeenAt'])

    def test_a_member_who_never_visited_is_not_online(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.get(f'/api/companies/{self.company.id}/members')

        by_id = {item['user']['id']: item['user'] for item in response.data['items']}
        self.assertFalse(by_id[self.member.id]['online'])
        self.assertIsNone(by_id[self.member.id]['lastSeenAt'])
