from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import Membership, User
from companies.models import Company
from handbook.models import CompanyScope
from sources.models import Connection, Item, RawDocument

from .models import InstructionCard, Task
from .todos import KEEP_DONE, TODO_SLOTS, purge_done

# assignee=None(미배정)과 인자를 생략한 경우를 구분한다.
UNSET = object()


class TodoTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.project = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.PROJECT, name='payment-api'
        )
        self.member = User.objects.create_user(
            email='m@example.com', password='pw', display_name='Minh', ui_language='en'
        )
        Membership.objects.create(
            user=self.member, company=self.company, role=Membership.Role.MEMBER
        )
        self.other = User.objects.create_user(
            email='o@example.com', password='pw', display_name='Linh'
        )
        Membership.objects.create(
            user=self.other, company=self.company, role=Membership.Role.MEMBER
        )
        connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=connection,
            external_id='C001', label='#payment-api', scope=self.project,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.member)
        self.url = f'/api/companies/{self.company.id}/tasks'

    def card(self, ref='1.1', assignee=UNSET, status=InstructionCard.Status.READY, **extra):
        document = RawDocument.objects.create(
            company=self.company, item=self.item, external_ref=ref,
            raw_text='결제 로그 좀 봐주세요', content_hash=ref.ljust(64, '0'),
            occurred_at=timezone.now(),
        )

        return InstructionCard.objects.create(
            company=self.company, scope=self.project, document=document,
            assignee=self.member if assignee is UNSET else assignee,
            purpose='결제 실패 로그의 원인을 파악한다',
            purpose_en='Find the cause of the payment failures',
            status=status, **extra,
        )

    def cards(self, count):
        return [self.card(ref=f'c.{i}') for i in range(count)]

    def get(self):
        return self.client.get(self.url).data['items']

    def add(self, title='Write the summary doc', **extra):
        return self.client.post(self.url, {'title': title, **extra}, format='json')

    def check(self, task_id, done=True):
        return self.client.patch(
            f'{self.url}/{task_id}',
            {'status': Task.Status.DONE if done else Task.Status.TODO},
            format='json',
        )

    # --- Ready 카드에서 채우기 ---

    def test_empty_when_there_are_no_cards(self):
        self.assertEqual(self.get(), [])

    def test_ready_cards_fill_the_slots(self):
        self.cards(3)

        items = self.get()

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]['origin'], 'CARD')
        self.assertEqual(items[0]['title'], 'Find the cause of the payment failures')

    # 다섯 칸까지만 채운다.
    def test_never_fills_beyond_the_slots(self):
        self.cards(8)

        self.assertEqual(len(self.get()), TODO_SLOTS)

    # 최근 것부터 담는다.
    def test_newest_cards_come_first(self):
        first = self.card(ref='a.1')
        last = self.card(ref='a.2')

        self.assertEqual([i['cardId'] for i in self.get()], [last.id, first.id])

    # 같은 카드가 두 번 담기면 안 된다.
    def test_a_card_is_taken_only_once(self):
        self.cards(2)
        self.get()

        self.assertEqual(len(self.get()), 2)

    def test_only_my_cards_are_taken(self):
        self.card(ref='b.1', assignee=self.other)

        self.assertEqual(self.get(), [])

    def test_unassigned_cards_are_not_taken(self):
        self.card(ref='b.2', assignee=None)

        self.assertEqual(self.get(), [])

    # Ready 가 아닌 카드는 이미 손을 댔거나 끝난 것이다.
    def test_only_ready_cards_are_taken(self):
        self.card(ref='b.3', status=InstructionCard.Status.IN_PROGRESS)
        self.card(ref='b.4', status=InstructionCard.Status.DONE)

        self.assertEqual(self.get(), [])

    # --- 직접 추가 ---

    def test_direct_add(self):
        response = self.add()

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['origin'], 'SELF')
        self.assertIsNone(response.data['cardId'])

    def test_direct_add_takes_a_due_date(self):
        due = timezone.now() + timedelta(days=1)

        self.assertIsNotNone(self.add(dueAt=due.isoformat()).data['dueAt'])

    def test_direct_add_rejects_an_outside_scope(self):
        other = Company.objects.create(name='다른회사', code='OTHERCODE')
        scope = CompanyScope.objects.create(
            company=other, kind=CompanyScope.Kind.PROJECT, name='x'
        )

        self.assertEqual(self.add(scopeId=scope.id).status_code, 400)

    def test_direct_add_requires_a_title(self):
        self.assertEqual(self.client.post(self.url, {}, format='json').status_code, 400)

    # 직접 넣은 것까지 다섯 칸이 차면 카드를 더 담지 않는다.
    def test_direct_adds_take_up_slots(self):
        for index in range(TODO_SLOTS):
            self.add(title=f'mine {index}')
        self.cards(3)

        items = self.get()

        self.assertEqual(len(items), TODO_SLOTS)
        self.assertEqual({i['origin'] for i in items}, {'SELF'})

    # --- 완료 ---

    def test_checking_marks_it_done(self):
        task = self.add().data

        response = self.check(task['id'])

        self.assertTrue(response.data['done'])
        self.assertIsNotNone(response.data['doneAt'])

    def test_unchecking_clears_the_time(self):
        task = self.add().data
        self.check(task['id'])

        response = self.check(task['id'], done=False)

        self.assertFalse(response.data['done'])
        self.assertIsNone(response.data['doneAt'])

    # 체크한 것은 아래로 내려간다.
    def test_done_items_sink_to_the_bottom(self):
        first = self.add(title='first').data
        self.add(title='second')

        self.check(first['id'])

        self.assertEqual([i['title'] for i in self.get()], ['second', 'first'])

    # 완료하면 칸이 비므로 다음 카드가 올라온다.
    def test_completing_frees_a_slot(self):
        self.cards(6)
        items = self.get()

        self.check(items[0]['id'])

        self.assertEqual(len([i for i in self.get() if not i['done']]), TODO_SLOTS)

    # 담긴 할 일을 끝내도 카드는 그대로다. 개인 체크리스트와 팀의 작업은 다르다.
    def test_completing_does_not_touch_the_card(self):
        card = self.card()
        task = self.get()[0]

        self.check(task['id'])

        card.refresh_from_db()
        self.assertEqual(card.status, InstructionCard.Status.READY)

    # --- 삭제 ---

    def test_delete(self):
        task = self.add().data

        self.assertEqual(self.client.delete(f'{self.url}/{task["id"]}').status_code, 204)
        self.assertEqual(self.get(), [])

    def test_deleting_a_card_task_keeps_the_card(self):
        card = self.card()
        task = self.get()[0]

        self.client.delete(f'{self.url}/{task["id"]}')

        self.assertTrue(InstructionCard.objects.filter(id=card.id).exists())

    # --- 정리 ---

    # 체크한 것은 하루 동안 아래에 남아 있어야 방금 무엇을 끝냈는지 보인다.
    def test_purge_keeps_todays_completions(self):
        task = self.add().data
        self.check(task['id'])

        self.assertEqual(purge_done(), 0)
        self.assertEqual(len(self.get()), 1)

    def test_purge_removes_yesterdays_completions(self):
        task = self.add().data
        self.check(task['id'])
        Task.objects.filter(id=task['id']).update(
            done_at=timezone.now() - KEEP_DONE - timedelta(minutes=1)
        )

        self.assertEqual(purge_done(), 1)

    def test_purge_leaves_open_items(self):
        self.add()

        self.assertEqual(purge_done(timezone.now() + timedelta(days=365)), 0)

    # --- 남의 할 일 ---

    def test_another_members_task_is_not_mine(self):
        task = Task.objects.create(
            company=self.company, user=self.other, title='not mine'
        )

        self.assertEqual(self.client.patch(
            f'{self.url}/{task.id}', {'status': 'DONE'}, format='json'
        ).status_code, 404)

    def test_outsider_is_rejected(self):
        outsider = User.objects.create_user(
            email='x@example.com', password='pw', display_name='X'
        )
        self.client.force_authenticate(user=outsider)

        self.assertEqual(self.client.get(self.url).status_code, 403)
