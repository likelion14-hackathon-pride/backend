from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from rest_framework.exceptions import ValidationError
from rest_framework.request import Request
from rest_framework.test import APIClient

from accounts.models import Membership, User
from cards.models import InstructionCard
from companies.models import Company
from sources.models import Connection
from sources.slack import SlackError

from .pagination import paginate


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

        response = self.client.get(f'/api/companies/{self.company.id}/cards?column=NOPE')

        self.assertEqual(response.status_code, 400)
        self.assertEnvelope(response, field='column')
        self.assertEqual(response.data['error']['message'], 'invalid column')

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

    # 밖에서 온 예외도 뷰가 옮겨 담지 않고 그대로 봉투가 된다.
    # 슬랙이 준 코드가 message 가 아니라 code 로 나가야 프론트가 분기할 수 있다.
    def test_domain_error_carries_its_own_code(self):
        self.client.force_authenticate(user=self.owner)
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK,
            status=Connection.Status.CONNECTED, bot_token='xoxb-test',
        )

        with patch(
            'sources.services.add_channel', side_effect=SlackError('invalid_auth')
        ):
            response = self.client.post(
                f'/api/companies/{self.company.id}/source-connections/{connection.id}/channels',
                {'externalId': 'C1'}, format='json',
            )

        self.assertEqual(response.status_code, 400)
        self.assertEnvelope(response, code='invalid_auth')

    # 429 는 어디서 났든 같은 코드로 나가야 한다. DRF 스로틀은 throttled 를 쓰므로 이름을 바꾼다.
    def test_rate_limited(self):
        for _ in range(6):
            response = self.client.post(
                '/api/auth/signup/owner',
                {'email': 'a@b.com', 'password': 'pw', 'displayName': 'x', 'companyName': 'y'},
                format='json',
            )

        self.assertEqual(response.status_code, 429)
        self.assertEnvelope(response, code='rate_limited')
        self.assertIn('Retry-After', response)

    # 503 은 원인을 밖으로 내보내지 않는다. 설정 파일 내용이 화면에 뜨면 안 된다.
    def test_upstream_error_hides_its_reason(self):
        self.client.force_authenticate(user=self.owner)

        with patch(
            'qna.services._ask',
            side_effect=ImproperlyConfigured('OPENAI_API_KEY 설정이 없습니다'),
        ):
            response = self.client.post(
                f'/api/companies/{self.company.id}/ask',
                {'question': 'what time'}, format='json',
            )

        self.assertEqual(response.status_code, 503)
        self.assertEnvelope(response, code='ai_unavailable')
        self.assertNotIn('OPENAI_API_KEY', response.data['error']['message'])

    # 아무도 잡지 않은 예외도 봉투로 나가야 한다. 여기서 HTML 이 나가면
    # JSON 을 기대하던 클라이언트는 파싱에서 죽는다.
    @override_settings(DEBUG=False)
    def test_unhandled_exception_is_wrapped(self):
        self.client.force_authenticate(user=self.owner)

        with patch('qna.services._ask', side_effect=TypeError('boom')):
            response = self.client.post(
                f'/api/companies/{self.company.id}/ask',
                {'question': 'what time'}, format='json',
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response['Content-Type'], 'application/json')
        self.assertEnvelope(response, code='server_error')


class PaginationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.cards = [
            InstructionCard.objects.create(company=self.company, purpose=f'일 {i}')
            for i in range(5)
        ]
        self.factory = APIClient()

    def paginate(self, query=''):
        request = Request(self.factory.get(f'/?{query}').wsgi_request)

        return paginate(InstructionCard.objects.all(), request)

    # 커서는 마지막 항목의 id 다. 오프셋과 달리 앞에 행이 늘어도 페이지가 밀리지 않는다.
    def test_pages_do_not_overlap(self):
        first, cursor = self.paginate('limit=2')
        second, _ = self.paginate(f'limit=2&cursor={cursor}')

        self.assertEqual([c.id for c in first], [self.cards[4].id, self.cards[3].id])
        self.assertEqual([c.id for c in second], [self.cards[2].id, self.cards[1].id])

    # 마지막 페이지에서는 더 없다고 알려야 한다.
    def test_last_page_has_no_cursor(self):
        items, cursor = self.paginate('limit=10')

        self.assertEqual(len(items), 5)
        self.assertIsNone(cursor)

    def test_invalid_limit(self):
        with self.assertRaises(ValidationError):
            self.paginate('limit=abc')

    def test_limit_out_of_range(self):
        with self.assertRaises(ValidationError):
            self.paginate('limit=0')
        with self.assertRaises(ValidationError):
            self.paginate('limit=101')

    def test_invalid_cursor(self):
        with self.assertRaises(ValidationError):
            self.paginate('cursor=nope')
