from django.db import IntegrityError
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import Membership, User
from accounts.serializers import OwnerSignupSerializer
from companies.models import Company

from .models import CompanyScope
from .services import DEFAULT_COMPANY_SCOPES, seed_default_scopes


class SeedDefaultScopesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')

    def test_creates_all_company_scopes(self):
        seed_default_scopes(self.company)

        scopes = CompanyScope.objects.filter(company=self.company)
        self.assertEqual(scopes.count(), len(DEFAULT_COMPANY_SCOPES))
        self.assertEqual(
            set(scopes.values_list('area_key', flat=True)),
            {area_key for area_key, _, _ in DEFAULT_COMPANY_SCOPES},
        )
        self.assertTrue(all(scope.kind == CompanyScope.Kind.COMPANY for scope in scopes))

    # 회사 생성 경로가 여러 곳이 되어도 중복이 생기지 않아야 한다.
    def test_is_idempotent(self):
        seed_default_scopes(self.company)
        seed_default_scopes(self.company)

        self.assertEqual(
            CompanyScope.objects.filter(company=self.company).count(),
            len(DEFAULT_COMPANY_SCOPES),
        )

    def test_area_key_is_unique_per_company_only(self):
        seed_default_scopes(self.company)
        other = Company.objects.create(name='다른회사', code='TESTCODE2')

        # 다른 회사는 같은 area_key를 가질 수 있다.
        seed_default_scopes(other)
        self.assertEqual(
            CompanyScope.objects.filter(company=other).count(),
            len(DEFAULT_COMPANY_SCOPES),
        )

        # 같은 회사 안에서는 중복 불가.
        with self.assertRaises(IntegrityError):
            CompanyScope.objects.create(
                company=self.company,
                kind=CompanyScope.Kind.COMPANY,
                area_key=CompanyScope.AreaKey.SECURITY,
                name='중복 Security',
            )

    # PROJECT 범위는 area_key가 null이라 unique 제약에 걸리지 않아야 한다.
    def test_project_scopes_are_not_limited(self):
        seed_default_scopes(self.company)

        for name in ('payment-api', 'admin-web', 'landing'):
            CompanyScope.objects.create(
                company=self.company, kind=CompanyScope.Kind.PROJECT, name=name
            )

        self.assertEqual(
            CompanyScope.objects.filter(
                company=self.company, kind=CompanyScope.Kind.PROJECT
            ).count(),
            3,
        )


class CreateProjectScopeTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        seed_default_scopes(self.company)

        self.owner = User.objects.create_user(email='owner@example.com', password='pw', display_name='대표')
        Membership.objects.create(user=self.owner, company=self.company, role=Membership.Role.OWNER)

        self.member = User.objects.create_user(email='member@example.com', password='pw', display_name='팀원')
        Membership.objects.create(user=self.member, company=self.company, role=Membership.Role.MEMBER)

        self.url = f'/api/companies/{self.company.id}/handbook/scopes'
        self.client = APIClient()

    def post(self, user, payload):
        self.client.force_authenticate(user=user)
        return self.client.post(self.url, payload, format='json')

    def test_owner_creates_project_scope(self):
        response = self.post(self.owner, {'kind': 'PROJECT', 'name': 'payment-api', 'description': '결제 시스템'})

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['kind'], 'PROJECT')
        self.assertEqual(response.data['name'], 'payment-api')
        # 프로젝트 범위는 회사규칙 카테고리를 갖지 않는다.
        self.assertIsNone(response.data['areaKey'])

    def test_multiple_project_scopes_allowed(self):
        for name in ('payment-api', 'admin-web', 'landing'):
            self.assertEqual(self.post(self.owner, {'kind': 'PROJECT', 'name': name}).status_code, 201)

        self.assertEqual(
            CompanyScope.objects.filter(company=self.company, kind=CompanyScope.Kind.PROJECT).count(), 3
        )

    def test_company_scope_cannot_be_created(self):
        response = self.post(self.owner, {'kind': 'COMPANY', 'name': '새 회사 규칙'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(CompanyScope.objects.filter(company=self.company).count(), len(DEFAULT_COMPANY_SCOPES))

    def test_duplicate_name_rejected(self):
        self.post(self.owner, {'kind': 'PROJECT', 'name': 'payment-api'})
        response = self.post(self.owner, {'kind': 'PROJECT', 'name': 'Payment-API'})

        self.assertEqual(response.status_code, 400)

    # 시딩된 회사 규칙 범위와 같은 이름도 막혀야 한다.
    def test_name_colliding_with_seeded_scope_rejected(self):
        response = self.post(self.owner, {'kind': 'PROJECT', 'name': 'Security'})

        self.assertEqual(response.status_code, 400)

    def test_member_cannot_create(self):
        response = self.post(self.member, {'kind': 'PROJECT', 'name': 'payment-api'})

        self.assertEqual(response.status_code, 403)
        self.assertFalse(CompanyScope.objects.filter(name='payment-api').exists())

    def test_outsider_cannot_create(self):
        outsider = User.objects.create_user(email='out@example.com', password='pw', display_name='외부인')
        response = self.post(outsider, {'kind': 'PROJECT', 'name': 'payment-api'})

        self.assertEqual(response.status_code, 403)


class OwnerSignupScopeTests(TestCase):
    # 대표 가입만으로 핸드북 항목을 만들 수 있는 상태가 되어야 한다.
    def test_owner_signup_seeds_scopes(self):
        serializer = OwnerSignupSerializer(
            data={
                'email': 'owner@example.com',
                'password': 'sai-test-pw-2026',
                'displayName': '조상원',
                'companyName': '에코랩',
            }
        )
        serializer.is_valid(raise_exception=True)
        membership = serializer.save()

        self.assertEqual(
            CompanyScope.objects.filter(
                company=membership.company, kind=CompanyScope.Kind.COMPANY
            ).count(),
            len(DEFAULT_COMPANY_SCOPES),
        )
