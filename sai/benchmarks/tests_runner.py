from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from companies.models import Company
from handbook.models import CompanyScope, HandbookEntry
from sources.models import Connection, Item, RawDocument

from .pipeline_runner import (
    _cards, _classification, _drafting, _qna, _retrieval,
)


class PipelineRunnerTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name='Benchmark Co', code='BENCH001')
        self.scope = CompanyScope.objects.create(
            company=self.company,
            kind=CompanyScope.Kind.PROJECT,
            name='benchmark-project',
        )
        self.connection = Connection.objects.create(
            company=self.company,
            kind=Connection.Kind.LOCAL,
        )
        self.item = Item.objects.create(
            company=self.company,
            connection=self.connection,
            external_id='benchmark-file',
            label='benchmark.txt',
            scope=self.scope,
            is_scope_confirmed=True,
        )
        self.document = RawDocument.objects.create(
            company=self.company,
            item=self.item,
            external_ref='benchmark-document',
            raw_text='금요일 오후에는 배포하지 않습니다.',
            content_hash='benchmark-hash',
        )

    @patch('sources.classifier._get_client', return_value=object())
    @patch('sources.classifier._classify_batch', return_value={0: 'INSTRUCTION'})
    def test_classification_serializes_the_production_label(self, _classify, _client):
        output = _classification({'documentId': self.document.id})

        self.assertEqual(output, {'label': 'INSTRUCTION'})

    @patch('handbook.drafting._get_client', return_value=object())
    def test_drafting_rolls_back_created_entries(self, _client):
        def draft(_client, company, scope, _documents, _channels, _users, _parents):
            entry = HandbookEntry.objects.create(
                company=company,
                scope=scope,
                title='금요일 배포 금지',
                body_ko='금요일 오후에는 배포하지 않습니다.',
                confidence=HandbookEntry.Confidence.HIGH,
            )
            return [entry], []

        with patch('handbook.drafting._draft_batch', side_effect=draft):
            output = _drafting({
                'companyId': self.company.id,
                'scopeId': self.scope.id,
                'documentIds': [self.document.id],
            })

        self.assertEqual(output['rules'][0]['title'], '금요일 배포 금지')
        self.assertFalse(HandbookEntry.objects.exists())

    @patch('qna.answering._get_client', return_value=object())
    @patch('qna.answering.embed_question', return_value=[0.0] * 1536)
    def test_retrieval_serializes_ranked_model_ids(self, _embed, _client):
        entry = SimpleNamespace(id=11)
        chunk = SimpleNamespace(id=22, document_id=self.document.id)
        with patch('qna.answering.retrieve', return_value=([entry], [chunk])) as retrieve:
            output = _retrieval({
                'companyId': self.company.id,
                'scopeId': self.scope.id,
                'query': '배포 정책',
            })

        self.assertEqual(output['rankedIds'], ['handbook:11', 'chunk:22'])
        self.assertEqual(
            output['rankedDocumentIds'], [f'document:{self.document.id}']
        )
        self.assertEqual(retrieve.call_args.kwargs['query'], '배포 정책')

    @patch('qna.answering._get_client')
    @patch('qna.answering.embed_question')
    def test_llm_only_retrieval_skips_embedding(self, embed, client):
        output = _retrieval({
            'companyId': self.company.id,
            'query': '배포 정책',
        }, profile='llm-only')

        self.assertEqual(output, {'rankedIds': [], 'rankedDocumentIds': []})
        embed.assert_not_called()
        client.assert_not_called()

    @patch('qna.answering._get_client', return_value=object())
    @patch('qna.answering.embed_question', return_value=[0.0] * 1536)
    def test_dense_profile_disables_hybrid_ranking(self, _embed, _client):
        from django.conf import settings

        observed = []

        def retrieve(_vector, _company, _scope, **_kwargs):
            observed.append(settings.HANDBOOK_HYBRID_RETRIEVAL_ENABLED)
            return [], []

        with patch('qna.answering.retrieve', side_effect=retrieve):
            _retrieval({
                'companyId': self.company.id,
                'query': '배포 정책',
            }, profile='dense-only')

        self.assertEqual(observed, [False])

    def test_qna_serializes_verdict_citations_and_usage(self):
        source = SimpleNamespace(entry=SimpleNamespace(id=11), chunk=None)
        result = SimpleNamespace(
            verdict='GROUNDED', answer='금요일 오후에는 배포하지 않습니다.'
        )
        usage = {
            'model': 'answer-model',
            'latencyMs': 123,
            'promptTokens': 20,
            'completionTokens': 5,
        }
        with patch(
            'qna.answering.answer_question',
            return_value=(result, [source], [], usage),
        ):
            output, telemetry = _qna({
                'companyId': self.company.id,
                'scopeId': self.scope.id,
                'question': '배포 정책',
            })

        self.assertFalse(output['escalated'])
        self.assertEqual(output['citationIds'], ['handbook:11'])
        self.assertEqual(telemetry['promptTokens'], 20)
        self.assertEqual(telemetry['completionTokens'], 5)
        self.assertEqual(telemetry['model'], 'answer-model')

    def test_llm_only_qna_uses_no_retrieved_context(self):
        result = SimpleNamespace(verdict='NO_SOURCE', answer='')
        usage = {
            'model': 'answer-model',
            'latencyMs': 50,
            'promptTokens': 10,
            'completionTokens': 2,
        }
        with patch(
            'benchmarks.pipeline_runner._llm_only_qna',
            return_value=(result, [], [], usage),
        ) as llm_only:
            output, telemetry = _qna({
                'companyId': self.company.id,
                'scopeId': self.scope.id,
                'question': '배포 정책',
            }, profile='llm-only')

        llm_only.assert_called_once()
        self.assertTrue(output['escalated'])
        self.assertEqual(output['citationIds'], [])
        self.assertEqual(output['citationDocumentIds'], [])
        self.assertEqual(telemetry['promptTokens'], 10)

    @patch('cards.generation._get_client', return_value=object())
    @patch('cards.generation._judge_batch', return_value={0: True})
    def test_cards_roll_back_created_cards(self, _judge, _client):
        from cards.models import InstructionCard

        def build(_client, company, document, _channels, _users):
            return InstructionCard.objects.create(
                company=company,
                scope=self.scope,
                document=document,
                purpose='배포 로그 확인',
                urgency=InstructionCard.Urgency.SOON,
            )

        with patch('cards.generation._build_card', side_effect=build):
            output = _cards({'documentId': self.document.id})

        self.assertTrue(output['isInstruction'])
        self.assertEqual(output['card']['purpose'], '배포 로그 확인')
        self.assertFalse(InstructionCard.objects.exists())
