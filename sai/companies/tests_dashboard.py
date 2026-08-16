from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership, User
from handbook.models import CompanyScope, HandbookEntry
from qna.models import Citation, Message, Thread

from .dashboard import dashboard_data
from .models import Company


class AnswerReuseTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG, name='Product / Engineering',
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.thread = Thread.objects.create(company=self.company, user=self.member)

    def entry(self, title='금요일 오후 배포 금지'):
        return HandbookEntry.objects.create(
            company=self.company, scope=self.scope, title=title, body_ko='본문',
            status=HandbookEntry.Status.CONFIRMED, origin=HandbookEntry.Origin.SLACK,
            confirmed_at=timezone.now(),
        )

    def cite(self, entry):
        message = Message.objects.create(
            company=self.company, thread=self.thread,
            role=Message.Role.AI, body_ko='답변',
        )

        return Citation.objects.create(
            company=self.company, message=message, entry=entry
        )

    def reuse(self):
        return dashboard_data(self.company)['answerReuse']

    def test_a_cited_rule_is_counted(self):
        entry = self.entry()
        self.cite(entry)
        self.cite(entry)

        reuse = self.reuse()

        self.assertEqual(reuse['totalCount'], 2)
        self.assertEqual(reuse['topEntries'][0]['entryId'], entry.id)
        self.assertEqual(reuse['topEntries'][0]['reuseCount'], 2)

    # 지운 규칙이 '많이 쓰인 규칙'에 남으면 대표는 삭제가 안 먹혔다고 본다.
    # 인용 행 자체는 지난 답변의 근거라 지우지 않는다.
    def test_a_deleted_rule_drops_out(self):
        entry = self.entry()
        self.cite(entry)
        entry.deleted_at = timezone.now()
        entry.save(update_fields=['deleted_at'])

        reuse = self.reuse()

        self.assertEqual(reuse['totalCount'], 0)
        self.assertEqual(reuse['topEntries'], [])
        self.assertTrue(Citation.objects.filter(entry=entry).exists())

    def test_a_deleted_rule_does_not_skew_the_average(self):
        self.cite(self.entry('살아 있는 규칙'))
        deleted = self.entry('지운 규칙')
        self.cite(deleted)
        self.cite(deleted)
        deleted.deleted_at = timezone.now()
        deleted.save(update_fields=['deleted_at'])

        reuse = self.reuse()

        self.assertEqual(reuse['totalCount'], 1)
        self.assertEqual(reuse['averageCount'], 1.0)
        self.assertEqual([row['title'] for row in reuse['topEntries']], ['살아 있는 규칙'])
