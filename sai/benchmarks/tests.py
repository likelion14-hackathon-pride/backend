import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from .ablation import build_ablation_report
from .cli import main
from .io import BenchmarkFormatError, read_jsonl, write_jsonl
from .promotion_runner import legacy_predictions
from .pipeline_runner import current_predictions
from .schema import validate_dataset, validate_predictions
from .scoring import compare_reports, score
from .synthetic_rag import (
    RULES, UsageCollector, build_dataset, build_repeat_reports,
    relabel_related_rules,
)
from .synthetic_corpus import UNSUPPORTED_CASES


DATASET = [
    {
        'caseId': 'c1',
        'task': 'classification',
        'input': {'text': 'deploy rule'},
        'gold': {'label': 'INSTRUCTION'},
    },
    {
        'caseId': 'd1',
        'task': 'drafting',
        'input': {'companyId': 1, 'scopeId': 1, 'documentIds': [1, 2]},
        'gold': {
            'rules': [{
                'requiredFacts': ['금요일 배포 금지'],
                'scopeId': 1,
                'evidenceIds': ['document:1', 'document:2'],
            }],
            'forbiddenFacts': ['언제든 배포'],
        },
    },
    {
        'caseId': 'p1',
        'task': 'promotion',
        'input': {'entryId': 1},
        'gold': {'promotionType': 'AUTO_PROMOTED', 'critical': False},
    },
    {
        'caseId': 'p2',
        'task': 'promotion',
        'input': {'entryId': 2},
        'gold': {'promotionType': 'MANUAL_REQUIRED', 'critical': True},
    },
    {
        'caseId': 'r1',
        'task': 'retrieval',
        'input': {'query': 'deploy'},
        'gold': {
            'relevantIds': ['h1', 'h2'],
            'relevantDocumentIds': ['document:10'],
        },
    },
    {
        'caseId': 'q1',
        'task': 'qna',
        'input': {'question': 'deploy?'},
        'gold': {
            'shouldEscalate': False,
            'requiredFacts': ['금요일 배포 금지'],
            'forbiddenFacts': ['언제든 배포'],
            'citationIds': ['h1'],
            'citationDocumentIds': ['document:10'],
        },
    },
    {
        'caseId': 'card1',
        'task': 'cards',
        'input': {'documentId': 3},
        'gold': {
            'isInstruction': True,
            'purposeFacts': ['로그 확인'],
            'deliverableFacts': ['결과'],
            'deadlineFacts': ['오늘'],
            'stepFacts': ['로그 확인'],
            'blankFacts': [],
            'forbiddenFacts': ['서버 삭제'],
            'urgency': 'SOON',
            'expectedDeadlineInferred': False,
        },
    },
]

PREDICTIONS = [
    {'caseId': 'c1', 'output': {'label': 'INSTRUCTION'}},
    {
        'caseId': 'd1',
        'output': {'rules': [{
            'title': '배포 규칙',
            'body': '금요일 배포 금지',
            'scopeId': 1,
            'evidenceIds': ['document:1', 'document:2'],
        }]},
    },
    {'caseId': 'p1', 'output': {'promotionType': 'AUTO_PROMOTED'}},
    {'caseId': 'p2', 'output': {'promotionType': 'MANUAL_REQUIRED'}},
    {
        'caseId': 'r1',
        'output': {
            'rankedIds': ['h1', 'other', 'h2'],
            'rankedDocumentIds': ['document:10'],
        },
    },
    {
        'caseId': 'q1',
        'output': {
            'escalated': False,
            'answer': '금요일 배포 금지 규칙입니다.',
            'citationIds': ['h1'],
            'citationDocumentIds': ['document:10'],
        },
        'telemetry': {'latencyMs': 100, 'promptTokens': 10, 'completionTokens': 5},
    },
    {
        'caseId': 'card1',
        'output': {
            'isInstruction': True,
            'card': {
                'purpose': '로그 확인',
                'deliverable': '결과 공유',
                'deadlineText': '오늘',
                'isDeadlineInferred': False,
                'urgency': 'SOON',
                'steps': ['로그 확인'],
                'blanks': [],
            },
        },
    },
]


class BenchmarkTests(unittest.TestCase):
    def test_jsonl_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rows.jsonl'
            write_jsonl(path, DATASET)
            self.assertEqual(read_jsonl(path), DATASET)

    def test_duplicate_case_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rows.jsonl'
            write_jsonl(path, [DATASET[0], DATASET[0]])
            with self.assertRaises(BenchmarkFormatError):
                read_jsonl(path)

    def test_scores_all_supported_tasks(self):
        report = score(
            validate_dataset(DATASET), validate_predictions(PREDICTIONS), retrieval_k=3
        )
        self.assertEqual(report['metrics']['classification']['accuracy'], 1.0)
        self.assertEqual(report['metrics']['drafting']['ruleF1'], 1.0)
        self.assertEqual(report['metrics']['promotion']['autoPrecision'], 1.0)
        self.assertEqual(report['metrics']['promotion']['criticalEscapeCount'], 0)
        self.assertEqual(report['metrics']['retrieval']['recall@3'], 1.0)
        self.assertEqual(report['metrics']['retrieval']['documentRecall@3'], 1.0)
        self.assertEqual(report['metrics']['qna']['requiredFactRecall'], 1.0)
        self.assertEqual(report['metrics']['qna']['documentCitationRecall'], 1.0)
        self.assertEqual(report['metrics']['cards']['instructionAccuracy'], 1.0)
        self.assertEqual(report['metrics']['cards']['purposeFactRecall'], 1.0)
        self.assertEqual(report['operations']['totalTokens'], 15)
        self.assertEqual(report['operationsByTask']['qna']['count'], 1)
        self.assertEqual(report['operationsByTask']['retrieval']['count'], 1)

    def test_legacy_promotion_requires_review(self):
        predictions = legacy_predictions(validate_dataset(DATASET))
        self.assertEqual(len(predictions), 2)
        self.assertTrue(all(
            row['output']['promotionType'] == 'PENDING_REVIEW'
            for row in predictions
        ))

    def test_compare_uses_candidate_minus_baseline(self):
        baseline = {'metrics': {'accuracy': 0.5}, 'operations': {'latency': 10}}
        candidate = {'metrics': {'accuracy': 0.8}, 'operations': {'latency': 8}}
        compared = compare_reports(baseline, candidate)
        self.assertEqual(
            compared['deltaCandidateMinusBaseline']['metrics']['accuracy'], 0.3
        )
        self.assertEqual(
            compared['deltaCandidateMinusBaseline']['operations']['latency'], -2
        )

    def test_pipeline_runner_filters_tasks_and_isolates_case_errors(self):
        dataset = [
            {'caseId': 'ok', 'task': 'classification', 'input': {}, 'gold': {}},
            {'caseId': 'skip', 'task': 'retrieval', 'input': {}, 'gold': {}},
            {'caseId': 'bad', 'task': 'classification', 'input': {}, 'gold': {}},
        ]
        calls = iter([{'label': 'INSTRUCTION'}, RuntimeError('model down')])

        def runner(_inputs):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value

        with patch('benchmarks.pipeline_runner._django_setup'), patch.dict(
            'benchmarks.pipeline_runner.RUNNERS', {'classification': runner}, clear=True
        ):
            rows = current_predictions(dataset, tasks={'classification'})

        self.assertEqual([row['caseId'] for row in rows], ['ok', 'bad'])
        self.assertEqual(rows[0]['output']['label'], 'INSTRUCTION')
        self.assertIn('RuntimeError: model down', rows[1]['error'])
        self.assertIn('latencyMs', rows[1]['telemetry'])

    def test_ablation_report_compares_profiles_in_pipeline_order(self):
        baseline = deepcopy(PREDICTIONS)
        retrieval = next(row for row in baseline if row['caseId'] == 'r1')
        retrieval['output']['rankedIds'] = []
        retrieval['output']['rankedDocumentIds'] = []

        report = build_ablation_report(
            validate_dataset(DATASET),
            {
                'current': validate_predictions(PREDICTIONS),
                'llm-only': validate_predictions(baseline),
            },
            retrieval_k=3,
        )

        self.assertEqual(report['profileOrder'], ['llm-only', 'current'])
        delta = report['comparisons']['llm-only->current']
        self.assertEqual(delta['metrics']['retrieval']['recall@3'], 1.0)
        self.assertEqual(
            delta['metrics']['retrieval']['documentRecall@3'], 1.0
        )

    def test_ablation_requires_two_profiles(self):
        with self.assertRaises(ValueError):
            build_ablation_report(
                validate_dataset(DATASET), {'current': PREDICTIONS}
            )

    def test_ablation_cli_writes_combined_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset_path = root / 'dataset.jsonl'
            baseline_path = root / 'baseline.jsonl'
            current_path = root / 'current.jsonl'
            output_path = root / 'report.json'
            write_jsonl(dataset_path, DATASET)
            write_jsonl(baseline_path, PREDICTIONS)
            write_jsonl(current_path, PREDICTIONS)

            with patch('benchmarks.cli._print'):
                result = main([
                    'ablation',
                    '--dataset', str(dataset_path),
                    '--profile', f'llm-only={baseline_path}',
                    '--profile', f'current={current_path}',
                    '--output', str(output_path),
                    '--retrieval-k', '3',
                ])

            self.assertEqual(result, 0)
            report = json.loads(output_path.read_text(encoding='utf-8'))
            self.assertEqual(report['profileOrder'], ['llm-only', 'current'])
            self.assertIn('llm-only->current', report['comparisons'])

    def test_synthetic_rag_dataset_repeats_each_rule_for_both_tasks(self):
        entries = {
            rule['key']: SimpleNamespace(id=index)
            for index, rule in enumerate(RULES, start=100)
        }
        dataset = build_dataset(1, 2, entries, repeats=3)

        grounded_questions = sum(len(rule['questions']) for rule in RULES)
        self.assertEqual(len(RULES), 45)
        self.assertEqual(grounded_questions + len(UNSUPPORTED_CASES), 150)
        self.assertEqual(
            len(dataset),
            (grounded_questions * 2 + len(UNSUPPORTED_CASES)) * 3,
        )
        self.assertEqual(
            len({row['caseId'] for row in dataset}), len(dataset)
        )
        self.assertEqual(
            {row['task'] for row in dataset}, {'retrieval', 'qna'}
        )
        first = deepcopy(dataset)
        second = relabel_related_rules(deepcopy(first))
        self.assertEqual(second, first)

    def test_usage_collector_includes_generation_and_embedding_cost(self):
        collector = UsageCollector()
        response = SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=1_000,
            completion_tokens=100,
        ))
        collector.record('answer', 'gpt-5.6-terra', response)
        collector.record('embedding', 'text-embedding-3-small', response)

        summary = collector.summary()
        self.assertEqual(summary['apiCalls'], 2)
        self.assertEqual(summary['promptTokens'], 2_000)
        self.assertEqual(summary['completionTokens'], 200)
        self.assertEqual(summary['costUsd'], 0.00322)

    def test_synthetic_rag_repeat_reports_score_each_run_independently(self):
        dataset = [
            {
                'caseId': f'r{repeat}-qna',
                'task': 'qna',
                'input': {'question': 'rule?'},
                'gold': {
                    'shouldEscalate': False,
                    'verdict': 'GROUNDED',
                    'requiredFacts': ['required'],
                    'forbiddenFacts': [],
                    'citationIds': ['handbook:1'],
                },
            }
            for repeat in (1, 2, 3)
        ]
        predictions = [{
            'caseId': row['caseId'],
            'output': {
                'escalated': False,
                'verdict': 'GROUNDED',
                'answer': 'required',
                'citationIds': ['handbook:1'],
            },
        } for row in dataset]

        reports = build_repeat_reports(
            dataset,
            {'current': predictions, 'llm-only': predictions},
        )

        self.assertEqual(list(reports['current']), ['r1', 'r2', 'r3'])
        self.assertTrue(all(
            report['datasetCases'] == 1
            for report in reports['current'].values()
        ))
        self.assertTrue(all(
            report['metrics']['qna']['requiredFactRecall'] == 1.0
            for report in reports['current'].values()
        ))


if __name__ == '__main__':
    unittest.main()
