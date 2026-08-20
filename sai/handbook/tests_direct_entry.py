from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings
from openai import OpenAIError
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company

from .finalizing import Translation, TranslationResult
from .models import CompanyScope, HandbookEntry
from .retrieval import search_rules

VECTOR = [0.1] * 1536


def finalizer(translation='No deploys on Friday afternoon.',
              title='Deployments do not happen on Friday afternoons.'):
    parsed = TranslationResult(
        translations=[Translation(index=0, title=title, text=translation)]
    )

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(
            parse=lambda **kwargs: SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(parsed=parsed))]
            )
        )),
        embeddings=SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
            data=[SimpleNamespace(embedding=VECTOR) for _ in kwargs['input']]
        )),
    )


def broken_finalizer():
    def fail(**kwargs):
        raise OpenAIError('down')

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=fail)),
        embeddings=SimpleNamespace(create=fail),
    )


@override_settings(OPENAI_API_KEY='test-key')
class DirectEntryTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.owner = User.objects.create_user(
            email='o@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(
            user=self.owner, company=self.company, role=Membership.Role.OWNER
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.owner)
        self.url = f'/api/companies/{self.company.id}/handbook/entries'

    def add(self, payload=None, translation='No deploys on Friday afternoon.'):
        body = {
            'title': '금요일 오후 배포 금지',
            'originalKo': '금요일 오후에는 운영 배포를 하지 않습니다.',
            'scopeId': self.scope.id,
        }
        with patch('handbook.finalizing.OpenAI', return_value=finalizer(translation)):
            return self.client.post(self.url, payload or body, format='json')

    # --- 영어 자동 생성 ---

    # 화면에는 한국어 칸만 있다. 대표에게 영어를 쓰게 하면 제품 취지에 어긋난다.
    def test_english_is_written_by_the_server(self):
        response = self.add()

        self.assertEqual(response.status_code, 201)
        entry = HandbookEntry.objects.get()
        self.assertEqual(entry.body_en, 'No deploys on Friday afternoon.')
        self.assertIsNotNone(entry.translated_at)

    def test_given_english_is_kept(self):
        self.add({
            'title': '금요일 오후 배포 금지',
            'originalKo': '금요일 오후에는 운영 배포를 하지 않습니다.',
            'ruleEn': 'Owner-written English.',
            'scopeId': self.scope.id,
        })

        self.assertEqual(HandbookEntry.objects.get().body_en, 'Owner-written English.')

    def test_korean_is_still_required(self):
        response = self.client.post(
            self.url, {'title': '제목만', 'scopeId': self.scope.id}, format='json'
        )

        self.assertEqual(response.status_code, 400)

    # --- 답변 근거로 쓰이는가 ---

    # 임베딩이 없으면 확정 상태여도 검색에 안 걸린다. 대표가 쓴 규칙이 답변에 안 쓰인다.
    def test_the_entry_is_searchable(self):
        self.add()

        found = search_rules(VECTOR, self.company)

        self.assertEqual([e.title for e in found], ['금요일 오후 배포 금지'])

    def test_the_entry_is_confirmed_on_save(self):
        self.add()
        entry = HandbookEntry.objects.get()

        self.assertEqual(entry.status, HandbookEntry.Status.CONFIRMED)
        self.assertEqual(entry.origin, 'DIRECT_ENTRY')
        self.assertIsNotNone(entry.confirmed_at)

    # OpenAI 가 죽어도 대표가 누른 저장은 되돌리지 않는다.
    def test_a_failed_translation_still_saves(self):
        with patch('handbook.finalizing.OpenAI', return_value=broken_finalizer()):
            response = self.client.post(self.url, {
                'title': '금요일 오후 배포 금지',
                'originalKo': '금요일 오후에는 운영 배포를 하지 않습니다.',
                'scopeId': self.scope.id,
            }, format='json')

        self.assertEqual(response.status_code, 201)
        entry = HandbookEntry.objects.get()
        self.assertIsNone(entry.body_en)
        self.assertIsNone(entry.embedded_at)

    def test_member_cannot_add(self):
        member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh'
        )
        Membership.objects.create(
            user=member, company=self.company, role=Membership.Role.MEMBER
        )
        self.client.force_authenticate(user=member)

        self.assertEqual(self.add().status_code, 403)
