from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from companies.models import Company

from .models import Membership, User

URL = '/api/auth/login'


class LoginTests(TestCase):
    def setUp(self):
        # 로그인은 IP 당 분당 10회로 묶여 있다. 캐시를 비우지 않으면 앞 테스트가
        # 쓴 횟수가 남아 뒤 테스트가 429 를 받는다.
        cache.clear()
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.user = User.objects.create_user(
            email='owner@example.com', password='sai-pw-2026!', display_name='김대표'
        )
        self.membership = Membership.objects.create(
            user=self.user, company=self.company, role=Membership.Role.OWNER
        )
        self.client = APIClient()

    def login(self, **overrides):
        payload = {'email': 'owner@example.com', 'password': 'sai-pw-2026!', **overrides}

        return self.client.post(URL, payload, format='json')

    def error(self, response):
        return response.data.get('error', {})

    def test_correct_credentials(self):
        response = self.login()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['accessToken'])

    def test_wrong_password(self):
        response = self.login(password='wrong-password')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.error(response)['code'], 'invalid_credentials')

    # 없는 이메일과 틀린 비밀번호가 다른 응답이면 가입 여부를 물어볼 수 있다.
    def test_unknown_email_looks_the_same(self):
        wrong_password = self.login(password='wrong-password')
        unknown_email = self.login(email='nobody@example.com')

        self.assertEqual(unknown_email.status_code, wrong_password.status_code)
        self.assertEqual(self.error(unknown_email), self.error(wrong_password))

    def test_left_member_cannot_log_in(self):
        self.membership.left_at = timezone.now()
        self.membership.save(update_fields=['left_at'])

        response = self.login()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.error(response)['code'], 'invalid_credentials')

    def test_inactive_user_cannot_log_in(self):
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])

        response = self.login()

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.error(response)['code'], 'invalid_credentials')

    def test_email_case_does_not_matter(self):
        self.assertEqual(self.login(email='Owner@Example.com').status_code, 200)

    def test_surrounding_spaces_are_ignored(self):
        self.assertEqual(self.login(email='  owner@example.com  ').status_code, 200)

    # 빈 칸은 자격 증명이 틀린 것이 아니라 폼이 덜 찬 것이다. 화면이 칸에 붙여 줘야 한다.
    def test_missing_password_points_at_the_field(self):
        response = self.client.post(URL, {'email': 'owner@example.com'}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.error(response)['field'], 'password')

    def test_missing_email_points_at_the_field(self):
        response = self.client.post(URL, {'password': 'sai-pw-2026!'}, format='json')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.error(response)['field'], 'email')

    def test_malformed_email_points_at_the_field(self):
        response = self.login(email='not-an-email')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.error(response)['field'], 'email')
