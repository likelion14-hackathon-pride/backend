from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from cards.generation import GENERATOR_VERSION
from cards.models import InstructionCard
from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry

from .classifier import CLASSIFIER_VERSION
from .ingestion import has_pending_work
from .local_files import LocalFileStorageError
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

    def job(self, kind, created_ago, status=IngestionJob.Status.SUCCEEDED, connection=None):
        job = IngestionJob.objects.create(
            company=self.company, connection=connection or self.connection,
            kind=kind, item_ids=[self.item.id], status=status,
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

    # 확정 시점에 번역·임베딩이 실패했거나 Day 0 완료를 누르지 않으면 여기 걸린다.
    # 아무도 챙기지 않으면 외국인 직원은 영어를 못 읽고 Ask SAI 는 찾지 못한다.
    def test_confirmed_but_unembedded_entry_is_pending(self):
        self.settled_document()
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY, name='Company',
        )
        entry = HandbookEntry.objects.create(
            company=self.company, scope=scope, title='머지 승인 조건',
            body_ko='책임자 1인 이상의 승인이 필요합니다.',
            status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.ONBOARDING,
        )

        self.assertTrue(has_pending_work(self.company))

        entry.embedded_at = self.now
        entry.title_en = 'Merge approval requirements'
        entry.save(update_fields=['embedded_at', 'title_en'])

        self.assertFalse(has_pending_work(self.company))

    def test_confirmed_but_missing_english_title_is_pending(self):
        self.settled_document()
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY, name='Company',
        )
        entry = HandbookEntry.objects.create(
            company=self.company, scope=scope, title='머지 승인 조건',
            body_ko='책임자 1인 이상의 승인이 필요합니다.',
            status=HandbookEntry.Status.CONFIRMED,
            origin=HandbookEntry.Origin.ONBOARDING,
            embedded_at=self.now,
        )

        self.assertTrue(has_pending_work(self.company))

        entry.title_en = 'Merge approval requirements'
        entry.save(update_fields=['title_en'])

        self.assertFalse(has_pending_work(self.company))

    # 아직 확정하지 않은 초안은 검색 대상이 아니다. 임베딩할 이유가 없다.
    def test_draft_entry_is_not_pending(self):
        self.settled_document()
        scope = CompanyScope.objects.create(
            company=self.company, kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.COMPANY, name='Company',
        )
        HandbookEntry.objects.create(
            company=self.company, scope=scope, title='초안',
            body_ko='아직 확정 전입니다.', status=HandbookEntry.Status.DRAFT,
            origin=HandbookEntry.Origin.SLACK,
        )

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

    # 깃헙 웹훅도 COLLECT 를 만든다. 소스를 구분하지 않으면 푸시 한 번에
    # 슬랙 수집이 12시간 밀린다.
    def test_github_collect_does_not_delay_slack_collect(self):
        github = Connection.objects.create(
            company=self.company, kind=Connection.Kind.GITHUB
        )
        self.job(IngestionJob.Kind.COLLECT, timedelta(minutes=5), connection=github)

        jobs = enqueue_due_jobs(self.now)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, IngestionJob.Kind.COLLECT)
        self.assertEqual(jobs[0].connection_id, self.connection.id)

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


# 깃헙 웹훅은 등록 이후의 변경만 보낸다. 레포에 이미 쌓여 있는 README·이슈·PR 은
# 아무도 읽지 않아 규칙도 카드도 생기지 않았다.
class GithubSchedulingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE4')
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.GITHUB,
            github_app_id='1', github_private_key='pem',
        )
        self.now = timezone.now()

    def repository(self, external_id='9001', **extra):
        return Item.objects.create(
            company=self.company, connection=self.connection,
            external_id=external_id, label=f'pride/{external_id}', **extra,
        )

    def job(self, kind, created_ago, status=IngestionJob.Status.SUCCEEDED, item_ids=None):
        job = IngestionJob.objects.create(
            company=self.company, connection=self.connection, kind=kind,
            item_ids=item_ids if item_ids is not None else [], status=status,
        )
        IngestionJob.objects.filter(id=job.id).update(created_at=self.now - created_ago)

        return job

    def test_newly_added_repository_is_collected(self):
        item = self.repository()

        jobs = enqueue_due_jobs(self.now)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, IngestionJob.Kind.COLLECT)
        self.assertEqual(jobs[0].item_ids, [item.id])
        self.assertEqual(jobs[0].connection_id, self.connection.id)

    # 최초 수집은 주기를 기다리지 않는다. 방금 수집을 돌린 직후 레포를 담아도 바로 읽는다.
    def test_new_repository_does_not_wait_for_the_interval(self):
        synced = self.repository('9001', last_synced_at=self.now)
        self.job(IngestionJob.Kind.COLLECT, timedelta(minutes=5), item_ids=[synced.id])
        fresh = self.repository('9002')

        jobs = enqueue_due_jobs(self.now)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].item_ids, [fresh.id])

    def test_collected_repository_is_not_repeated(self):
        item = self.repository(last_synced_at=self.now)
        self.job(IngestionJob.Kind.COLLECT, timedelta(minutes=5), item_ids=[item.id])

        self.assertEqual(enqueue_due_jobs(self.now), [])

    # 최초 수집이 실패한 레포가 60초마다 되살아나면 깃헙 한도만 태운다.
    def test_failed_first_collect_waits_before_retry(self):
        item = self.repository()
        self.job(
            IngestionJob.Kind.COLLECT, timedelta(minutes=5),
            status=IngestionJob.Status.FAILED, item_ids=[item.id],
        )

        self.assertEqual(enqueue_due_jobs(self.now), [])
        self.assertEqual(len(enqueue_due_jobs(self.now + RETRY_AFTER + timedelta(minutes=1))), 1)

    # 웹훅 설정을 건너뛰면 이 주기 말고는 갱신될 길이 없다.
    def test_collect_is_repeated_after_the_interval(self):
        item = self.repository(last_synced_at=self.now)
        self.job(IngestionJob.Kind.COLLECT, COLLECT_EVERY + timedelta(minutes=1), item_ids=[item.id])

        jobs = enqueue_due_jobs(self.now)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, IngestionJob.Kind.COLLECT)
        self.assertEqual(jobs[0].item_ids, [item.id])

    # 슬랙 없이 깃헙만 쓰는 회사는 처리 작업이 도는 경로가 아예 없었다.
    def test_pending_work_is_processed_without_slack(self):
        item = self.repository(last_synced_at=self.now)
        self.job(IngestionJob.Kind.COLLECT, timedelta(hours=1), item_ids=[item.id])
        RawDocument.objects.create(
            company=self.company, item=item, external_ref='readme',
            raw_text='배포 전 리뷰를 받습니다', content_hash='a' * 64,
            occurred_at=self.now,
        )

        jobs = enqueue_due_jobs(self.now)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, IngestionJob.Kind.PROCESS)

    def test_nothing_pending_means_no_job(self):
        item = self.repository(last_synced_at=self.now)
        self.job(IngestionJob.Kind.COLLECT, timedelta(hours=1), item_ids=[item.id])

        self.assertEqual(enqueue_due_jobs(self.now), [])

    def test_company_without_repositories_is_skipped(self):
        self.assertEqual(enqueue_due_jobs(self.now), [])

    def test_removed_repository_is_skipped(self):
        self.repository(removed_at=self.now)

        self.assertEqual(enqueue_due_jobs(self.now), [])

    def test_disconnected_github_is_skipped(self):
        self.repository()
        self.connection.disconnected_at = self.now
        self.connection.save()

        self.assertEqual(enqueue_due_jobs(self.now), [])


# 업로드는 브라우저가 S3로 직접 한다. 서버는 완료 통보를 받지 못하므로
# 올라온 파일을 찾아 큐에 넣는 것은 이 스윕뿐이다.
class LocalFileSchedulingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='에코랩', code='TESTCODE3')
        self.connection = Connection.objects.create(
            company=self.company, kind=Connection.Kind.LOCAL
        )
        self.now = timezone.now()

    def file_item(self, external_id='file-1', **extra):
        return Item.objects.create(
            company=self.company, connection=self.connection,
            external_id=external_id, label='개발규칙.txt',
            storage_key=f'companies/{self.company.id}/local/{external_id}.txt',
            mime_type='text/plain', byte_size=12,
            **extra,
        )

    def enqueue(self, now=None, exists=True):
        with patch('sources.scheduling.object_exists', return_value=exists) as check:
            return enqueue_due_jobs(now or self.now), check

    def test_uploaded_file_is_queued(self):
        item = self.file_item()

        jobs, _ = self.enqueue()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].kind, IngestionJob.Kind.COLLECT)
        self.assertEqual(jobs[0].item_ids, [item.id])
        self.assertEqual(jobs[0].connection_id, self.connection.id)

    # 메타만 만들고 S3 PUT 이 아직 끝나지 않은 사이에 큐에 넣으면
    # 워커가 file_not_uploaded 로 실패시킨다.
    def test_file_not_uploaded_yet_is_not_queued(self):
        self.file_item()

        jobs, _ = self.enqueue(exists=False)

        self.assertEqual(jobs, [])

    def test_already_ingested_file_is_not_queued(self):
        self.file_item(last_synced_at=self.now)

        jobs, check = self.enqueue()

        self.assertEqual(jobs, [])
        check.assert_not_called()

    def test_removed_file_is_not_queued(self):
        self.file_item(removed_at=self.now)

        jobs, _ = self.enqueue()

        self.assertEqual(jobs, [])

    def test_failed_file_waits_before_retry(self):
        item = self.file_item()
        IngestionJob.objects.create(
            company=self.company, connection=self.connection,
            kind=IngestionJob.Kind.COLLECT, item_ids=[item.id],
            status=IngestionJob.Status.FAILED,
        )

        jobs, _ = self.enqueue()
        self.assertEqual(jobs, [])

        later, _ = self.enqueue(now=self.now + RETRY_AFTER + timedelta(minutes=1))
        self.assertEqual(len(later), 1)

    # S3가 답하지 않는 것은 파일이 없다는 뜻이 아니다. 실패로 굳히지 않고 다음 주기에 다시 본다.
    def test_storage_error_is_skipped(self):
        self.file_item()

        with patch(
            'sources.scheduling.object_exists',
            side_effect=LocalFileStorageError('storage_unavailable'),
        ):
            jobs = enqueue_due_jobs(self.now)

        self.assertEqual(jobs, [])
        self.assertEqual(IngestionJob.objects.count(), 0)

    def test_several_files_share_one_job(self):
        first = self.file_item('file-1')
        second = self.file_item('file-2')

        jobs, _ = self.enqueue()

        self.assertEqual(len(jobs), 1)
        self.assertCountEqual(jobs[0].item_ids, [first.id, second.id])

    def test_running_job_blocks_the_sweep(self):
        self.file_item()
        IngestionJob.objects.create(
            company=self.company, connection=self.connection,
            kind=IngestionJob.Kind.COLLECT, item_ids=[],
            status=IngestionJob.Status.RUNNING,
        )

        jobs, _ = self.enqueue()

        self.assertEqual(jobs, [])

    def test_disconnected_local_source_is_skipped(self):
        self.file_item()
        self.connection.disconnected_at = self.now
        self.connection.save()

        jobs, _ = self.enqueue()

        self.assertEqual(jobs, [])
