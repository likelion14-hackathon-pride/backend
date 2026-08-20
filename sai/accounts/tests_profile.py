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

    def me(self):
        return self.client.get('/api/me').data['user']

    # --- 초기 설정 전 ---

    # 위치가 비어 있는 것이 곧 '아직 설정하지 않음'이다. 별도 플래그를 두지 않는다.
    def test_a_new_account_has_no_location_or_role(self):
        user = self.me()

        self.assertIsNone(user['location'])
        self.assertIsNone(user['role'])

    # --- 초기 설정 ---

    def test_setup_saves_location_and_role(self):
        response = self.patch({'location': 'HANOI', 'role': 'BACKEND'})

        self.assertEqual(response.status_code, 200)
        user = response.data['user']
        self.assertEqual(user['location'], 'HANOI')
        self.assertEqual(user['role'], 'BACKEND')
        # 이름은 가입 때 받은 값이 그대로 남는다.
        self.assertEqual(user['name'], 'Minh')

    # 위치가 시각을 정한다. 이 값이 시차 화면의 '내 시각'이 된다.
    def test_location_sets_the_timezone(self):
        self.assertEqual(self.patch({'location': 'HANOI'}).data['user']['timezone'],
                         'Asia/Ho_Chi_Minh')

    def test_da_nang_shares_the_vietnam_timezone(self):
        self.assertEqual(self.patch({'location': 'DA_NANG'}).data['user']['timezone'],
                         'Asia/Ho_Chi_Minh')

    def test_new_york_has_its_own_timezone(self):
        self.assertEqual(self.patch({'location': 'NEW_YORK'}).data['user']['timezone'],
                         'America/New_York')

    # 위치를 바꾸면 시각도 따라 바뀌어야 한다. 옛 시각이 남으면 시차가 틀린다.
    def test_changing_the_location_moves_the_clock(self):
        self.patch({'location': 'HANOI'})

        self.patch({'location': 'TOKYO'})

        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, 'Asia/Tokyo')

    # --- 부분 수정 ---

    def test_role_alone_leaves_the_location(self):
        self.patch({'location': 'HANOI', 'role': 'BACKEND'})

        user = self.patch({'role': 'QA'}).data['user']

        self.assertEqual(user['role'], 'QA')
        self.assertEqual(user['location'], 'HANOI')

    def test_locale_can_be_changed(self):
        self.assertEqual(self.patch({'locale': 'ko'}).data['user']['locale'], 'ko')

    # --- 거부 ---

    # 목록에 없는 곳은 고를 수 없다. 임의 타임존을 받으면 위치와 시각이 어긋난다.
    def test_unknown_location_is_rejected(self):
        response = self.patch({'location': 'BERLIN'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data['error']['field'], 'location')

    def test_timezone_cannot_be_set_directly(self):
        response = self.patch({'timezone': 'America/New_York'})

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, 'Asia/Seoul')

    def test_unknown_role_is_rejected(self):
        self.assertEqual(self.patch({'role': 'DEVOPS'}).status_code, 400)

    # 이름은 가입 첫 화면에서 받는다. 여기로는 안 들어온다.
    def test_name_cannot_be_set_here(self):
        response = self.patch({'name': 'Someone Else'})

        self.assertEqual(response.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.display_name, 'Minh')

    def test_unknown_locale_is_rejected(self):
        self.assertEqual(self.patch({'locale': 'fr'}).status_code, 400)

    def test_empty_body_is_rejected(self):
        self.assertEqual(self.patch({}).status_code, 400)

    def test_anonymous_is_rejected(self):
        self.client.force_authenticate(user=None)

        self.assertEqual(self.patch({'location': 'HANOI'}).status_code, 401)

    # 소속이 없으면 회사 정보를 붙일 수 없다. 에러 형식은 다른 곳과 같아야 한다.
    def test_missing_membership_uses_the_error_envelope(self):
        Membership.objects.filter(user=self.user).delete()

        response = self.client.get('/api/me')

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data['error']['code'], 'not_found')
