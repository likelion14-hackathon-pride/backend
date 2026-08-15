from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from cards.generation import GENERATOR_VERSION
from cards.models import InstructionCard
from companies.models import Company

from .classifier import CLASSIFIER_VERSION
from .ingestion import has_pending_work
from .models import Connection, IngestionJob, Item, RawDocument
from .scheduling import COLLECT_EVERY, PROCESS_EVERY, RETRY_AFTER, enqueue_due_jobs


class SchedulingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE1')
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.SLACK, bot_token='xoxb-test'
        )
        self.item = Item.objects.create(
            company=self.company, connection=self.connection,
            external_id='C001', label='#dev',
        )
        self.now = timezone.now()

    def document(self, ref='1.1', **extra):
        return RawDocument.objects.create(
            company=self.company, item=self.item, external_ref=ref,
            raw_text='결제 로그 좀 봐주세요', content_hash=ref.ljust(64, '0'),
            occurred_at=self.now, **extra,
        )

    # 처리가 끝난 원문. 분류도 됐고 카드 판정도 끝났다.
    def settled_document(self, ref='1.1'):
        return self.document(
            ref,
            classified_as=RawDocument.ClassifiedAs.CONTEXT,
            classifier_version=CLASSIFIER_VERSION,
            card_version=GENERATOR_VERSION,
        )

    def job(self, kind, created_ago, status=IngestionJob.Status.SUCCEEDED):
        job = IngestionJob.objects.create(
            company=self.company, kind=kind, item_ids=[self.item.id], status=status
        )
        IngestionJob.objects.filter(id=job.id).update(created_at=self.now - created_ago)

        return job

    # --- 처리할 것이 남았는가 ---

    def test_no_documents_means_nothing_pending(self):
        self.assertFalse(has_pending_work(self.company))

    def test_unclassified_document_is_pending(self):
        self.document()

        self.assertTrue(has_pending_work(self.company))

    def test_classified_but_unjudged_document_is_pending(self):
        self.document(
            classified_as=RawDocument.ClassifiedAs.CONTEXT,
            classifier_version=CLASSIFIER_VERSION,
        )

        self.assertTrue(has_pending_work(self.company))

    def test_settled_document_is_not_pending(self):
        self.settled_document()

        self.assertFalse(has_pending_work(self.company))

    # 규칙으로 분류된 원문은 카드 후보가 아니다. 카드 판정을 기다리지 않는다.
    def test_rule_document_is_not_pending(self):
        self.document(
            classified_as=RawDocument.ClassifiedAs.INSTRUCTION,
            classifier_version=CLASSIFIER_VERSION,
        )

        self.assertFalse(has_pending_work(self.company))

    def test_document_with_a_card_is_not_pending(self):
        document = self.document(
            classified_as=RawDocument.ClassifiedAs.CONTEXT,
            classifier_version=CLASSIFIER_VERSION,
        )
        InstructionCard.objects.create(
            company=self.company, document=document, purpose='결제 로그 확인'
        )

        self.assertFalse(has_pending_work(self.company))

    # --- 큐에 넣기 ---

    # 처음에는 수집부터 한다. 슬랙에 이미 쌓여 있는 과거 대화를 가져와야 한다.
    def test_first_run_collects(self):
        jobs = enqueue_due_jobs(self.now)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, IngestionJob.Kind.COLLECT)
        self.assertEqual(jobs[0].item_ids, [self.item.id])

    def test_collect_is_repeated_after_the_interval(self):
        self.job(IngestionJob.Kind.COLLECT, COLLECT_EVERY + timedelta(minutes=1))

        self.assertEqual(enqueue_due_jobs(self.now)[0].kind, IngestionJob.Kind.COLLECT)

    def test_recent_collect_is_not_repeated(self):
        self.job(IngestionJob.Kind.COLLECT, timedelta(minutes=5))

        self.assertEqual(enqueue_due_jobs(self.now), [])

    # 웹훅으로 들어온 메시지는 슬랙을 다시 읽지 않고 처리만 한다.
    def test_pending_work_between_collects_is_processed(self):
        self.job(IngestionJob.Kind.COLLECT, timedelta(hours=1))
        self.document()

        jobs = enqueue_due_jobs(self.now)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, IngestionJob.Kind.PROCESS)

    # 할 일이 없으면 빈 작업을 만들지 않는다.
    def test_nothing_pending_means_no_job(self):
        self.job(IngestionJob.Kind.COLLECT, timedelta(hours=1))
        self.settled_document()

        self.assertEqual(enqueue_due_jobs(self.now), [])

    def test_recent_process_is_not_repeated(self):
        self.job(IngestionJob.Kind.COLLECT, timedelta(hours=1))
        self.job(IngestionJob.Kind.PROCESS, PROCESS_EVERY - timedelta(minutes=1))
        self.document()

        self.assertEqual(enqueue_due_jobs(self.now), [])

    # OpenAI 한도에 걸린 상태에서 10분마다 재시도하면 한도만 계속 태운다.
    def test_failed_run_waits_longer(self):
        self.job(IngestionJob.Kind.COLLECT, timedelta(hours=1))
        self.job(
            IngestionJob.Kind.PROCESS,
            PROCESS_EVERY + timedelta(minutes=1),
            status=IngestionJob.Status.PARTIAL,
        )
        self.document()

        self.assertEqual(enqueue_due_jobs(self.now), [])

        later = self.now + RETRY_AFTER
        self.assertEqual(len(enqueue_due_jobs(later)), 1)

    # 이미 큐에 있는데 또 넣으면 같은 일을 두 번 한다.
    def test_queued_job_blocks_a_new_one(self):
        IngestionJob.objects.create(
            company=self.company, kind=IngestionJob.Kind.COLLECT, item_ids=[self.item.id]
        )

        self.assertEqual(enqueue_due_jobs(self.now), [])

    def test_running_job_blocks_a_new_one(self):
        IngestionJob.objects.create(
            company=self.company, kind=IngestionJob.Kind.COLLECT,
            item_ids=[self.item.id], status=IngestionJob.Status.RUNNING,
        )

        self.assertEqual(enqueue_due_jobs(self.now), [])

    # --- 대상에서 빠지는 회사 ---

    def test_company_without_slack_is_skipped(self):
        Company.objects.create(name='다른회사', code='TESTCODE2')

        self.assertEqual(len(enqueue_due_jobs(self.now)), 1)

    def test_disconnected_slack_is_skipped(self):
        self.connection.disconnected_at = self.now
        self.connection.save()

        self.assertEqual(enqueue_due_jobs(self.now), [])

    def test_company_without_channels_is_skipped(self):
        self.item.removed_at = self.now
        self.item.save()

        self.assertEqual(enqueue_due_jobs(self.now), [])
