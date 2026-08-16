from django.test import TestCase
from rest_framework.test import APIClient

from companies.models import Company

from .models import Membership, User


class ProfileTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.user = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh', ui_language='en'
        )
        Membership.objects.create(
            user=self.user, company=self.company, role=Membership.Role.MEMBER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def patch(self, payload):
        return self.client.patch('/api/me', payload, format='json')

    def test_default_timezone_is_the_company_one(self):
        self.assertEqual(self.client.get('/api/me').data['user']['timezone'], 'Asia/Seoul')

    def test_timezone_can_be_changed(self):
        response = self.patch({'timezone': 'Asia/Ho_Chi_Minh'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['timezone'], 'Asia/Ho_Chi_Minh')
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, 'Asia/Ho_Chi_Minh')

    def test_locale_can_be_changed(self):
        self.assertEqual(self.patch({'locale': 'ko'}).data['user']['locale'], 'ko')

    # 저장은 되고 시차를 계산할 때 터지는 것을 막는다.
    def test_unknown_timezone_is_rejected(self):
        response = self.patch({'timezone': 'Asia/Nowhere'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['field'], 'timezone')

    def test_path_shaped_timezone_is_rejected(self):
        self.assertEqual(self.patch({'timezone': '../etc/passwd'}).status_code, 400)

    def test_unknown_locale_is_rejected(self):
        self.assertEqual(self.patch({'locale': 'fr'}).status_code, 400)

    def test_empty_body_is_rejected(self):
        self.assertEqual(self.patch({}).status_code, 400)

    def test_anonymous_is_rejected(self):
        self.client.force_authenticate(user=None)

        self.assertEqual(self.patch({'timezone': 'Asia/Seoul'}).status_code, 401)

    # 소속이 없으면 회사 정보를 붙일 수 없다. 에러 형식은 다른 곳과 같아야 한다.
    def test_missing_membership_uses_the_error_envelope(self):
        Membership.objects.filter(user=self.user).delete()

        response = self.client.get('/api/me')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['error']['code'], 'not_found')
