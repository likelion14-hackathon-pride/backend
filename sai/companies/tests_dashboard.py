from django.test import TestCase
from django.utils import timezone

from accounts.models import Membership, User
from cards.models import Blank, InstructionCard
from handbook.models import CompanyScope, HandbookEntry
from qna.models import Citation, Escalation, Message, Thread
from sources.models import Connection, Identity, Item, RawDocument

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

    def test_waiting_questions_do_not_show_owner_as_asker(self):
        owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(
            user=owner, company=self.company, role=Membership.Role.OWNER
        )
        card = InstructionCard.objects.create(
            company=self.company, scope=self.scope, assignee=owner, purpose='로그 확인'
        )
        question = Escalation.objects.create(
            company=self.company, asked_by=owner, question_en='Which environment?',
            draft_ko='어느 환경을 보면 될까요?', status=Escalation.Status.SENT,
        )
        Blank.objects.create(
            company=self.company, card=card, question_en=question.question_en,
            escalation=question,
        )

        waiting = dashboard_data(self.company)['waitingQuestions']['items']

        self.assertIsNone(waiting[0]['askedByName'])

    def test_waiting_questions_use_unlinked_slack_author_when_member_is_missing(self):
        owner = User.objects.create_user(
            email='owner@example.com', password='pw', display_name='김대표'
        )
        Membership.objects.create(
            user=owner, company=self.company, role=Membership.Role.OWNER
        )
        connection = Connection.objects.create(company=self.company, kind=Connection.Kind.SLACK)
        item = Item.objects.create(
            company=self.company, connection=connection, external_id='C001', label='#dev'
        )
        author = Identity.objects.create(
            company=self.company, connection=connection,
            external_user_id='U_MING', external_handle='Ming',
        )
        document = RawDocument.objects.create(
            company=self.company, item=item, external_ref='1.1',
            author_identity=author, raw_text='대표님 확인 부탁드립니다',
            content_hash='a' * 64,
        )
        card = InstructionCard.objects.create(
            company=self.company, scope=self.scope, document=document,
            assignee=owner, purpose='로그 확인',
        )
        question = Escalation.objects.create(
            company=self.company, asked_by=owner, question_en='Which environment?',
            draft_ko='어느 환경을 보면 될까요?', status=Escalation.Status.SENT,
        )
        Blank.objects.create(
            company=self.company, card=card, question_en=question.question_en,
            escalation=question,
        )

        waiting = dashboard_data(self.company)['waitingQuestions']['items']

        self.assertEqual(waiting[0]['askedByName'], 'Ming')
