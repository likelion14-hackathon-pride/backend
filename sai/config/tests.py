from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company


# 에러 응답은 종류를 가리지 않고 한 가지 모양으로 나가야 한다.
# 인증만 {"error": {...}} 이고 나머지는 DRF 기본 형태였다.
class ErrorEnvelopeTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.owner = User.objects.create_user(
            email='owner@example.com', password='sai-test-pw-2026', display_name='대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.client = APIClient()

    def assertEnvelope(self, response, code=None, field=None):
        self.assertEqual(set(response.data), {'error'})
        error = response.data['error']
        self.assertEqual(set(error), {'code', 'field', 'message'})
        self.assertTrue(error['message'])
        if code is not None:
            self.assertEqual(error['code'], code)
        self.assertEqual(error['field'], field)

    def test_unauthenticated(self):
        response = self.client.get(f'/api/companies/{self.company.id}/cards')

        self.assertEqual(response.status_code, 401)
        self.assertEnvelope(response, code='not_authenticated')

    def test_permission_denied(self):
        outsider = User.objects.create_user(
            email='out@example.com', password='pw', display_name='남'
        )
        self.client.force_authenticate(user=outsider)

        response = self.client.get(f'/api/companies/{self.company.id}/cards')

        self.assertEqual(response.status_code, 403)
        self.assertEnvelope(response, code='permission_denied')

    def test_not_found(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.get('/api/companies/999999/cards')

        self.assertEqual(response.status_code, 404)
        self.assertEnvelope(response, code='not_found')

    # 칸 이름이 붙는 검증 에러.
    def test_field_validation(self):
        self.client.force_authenticate(user=self.owner)

        response = self.client.get(f'/api/companies/{self.company.id}/cards?status=NOPE')

        self.assertEqual(response.status_code, 400)
        self.assertEnvelope(response, field='status')
        self.assertEqual(response.data['error']['message'], 'invalid status')

    # 업무 코드는 붙을 칸이 정해져 있다.
    def test_business_code_carries_its_field(self):
        response = self.client.post(
            '/api/auth/signup/owner',
            {
                'email': self.owner.email,
                'password': 'sai-test-pw-2026',
                'displayName': '조상원',
                'companyName': '에코랩',
            },
            format='json',
        )

        self.assertEqual(response.status_code, 400)
        self.assertEnvelope(response, code='email_taken', field='email')

    # 칸과 무관한 검증 에러는 field 가 비어 있어야 한다.
    # DRF 는 이런 것을 non_field_errors 에 담는데 그건 칸 이름이 아니다.
    def test_non_field_validation_has_no_field(self):
        member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Alex'
        )
        Membership.objects.create(
            user=member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client.force_authenticate(user=member)

        response = self.client.post(
            f'/api/companies/{self.company.id}/questions', {}, format='json'
        )

        self.assertEqual(response.status_code, 400)
        self.assertEnvelope(response, field=None)
