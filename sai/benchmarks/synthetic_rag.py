import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from unittest.mock import patch

from django.db import transaction
from django.utils import timezone

from .ablation import build_ablation_report
from .io import read_jsonl, write_json, write_jsonl
from .pipeline_runner import current_predictions, environment_manifest
from .pricing import EMBEDDING_PRICES_PER_MILLION, PRICES_PER_MILLION
from .schema import validate_dataset
from .scoring import score
from .synthetic_corpus import RULES, UNSUPPORTED_CASES

PROFILE_RUNS = (
    ('llm-only', 'llm-only'),
    ('structure-dense', 'dense-only'),
    ('hybrid', 'hybrid'),
    ('current', 'current'),
)

_REPEAT_CASE_ID = re.compile(r'^r(?P<repeat>\d+)-')


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

class UsageCollector:
    def __init__(self):
        self.rows = []

    def record(self, operation, model, response, inputs=1):
        usage = getattr(response, 'usage', None)
        prompt = int(getattr(usage, 'prompt_tokens', 0) or 0)
        completion = int(getattr(usage, 'completion_tokens', 0) or 0)
        if model in PRICES_PER_MILLION:
            price = PRICES_PER_MILLION[model]
            cost = (
                prompt * price['input'] + completion * price['output']
            ) / 1_000_000
        else:
            cost = (
                prompt * EMBEDDING_PRICES_PER_MILLION.get(model, 0.0)
            ) / 1_000_000
        self.rows.append({
            'operation': operation,
            'model': model,
            'inputs': inputs,
            'promptTokens': prompt,
            'completionTokens': completion,
            'costUsd': cost,
        })

    def summary(self):
        return {
            'apiCalls': len(self.rows),
            'logicalInputs': sum(row['inputs'] for row in self.rows),
            'promptTokens': sum(row['promptTokens'] for row in self.rows),
            'completionTokens': sum(row['completionTokens'] for row in self.rows),
            'totalTokens': sum(
                row['promptTokens'] + row['completionTokens'] for row in self.rows
            ),
            'costUsd': round(sum(row['costUsd'] for row in self.rows), 8),
            'byOperation': self._by_operation(),
        }

    def _by_operation(self):
        values = {}
        for row in self.rows:
            value = values.setdefault(row['operation'], {
                'apiCalls': 0,
                'logicalInputs': 0,
                'promptTokens': 0,
                'completionTokens': 0,
                'costUsd': 0.0,
            })
            value['apiCalls'] += 1
            value['logicalInputs'] += row['inputs']
            value['promptTokens'] += row['promptTokens']
            value['completionTokens'] += row['completionTokens']
            value['costUsd'] += row['costUsd']
        for value in values.values():
            value['costUsd'] = round(value['costUsd'], 8)
        return dict(sorted(values.items()))


def build_dataset(company_id, scope_id, entries_by_key, repeats=3):
    rows = []
    for repeat in range(1, repeats + 1):
        for rule in RULES:
            entry = entries_by_key[rule['key']]
            relevant_ids = [
                f'handbook:{entries_by_key[key].id}'
                for key in rule.get('related', [rule['key']])
            ]
            for question_index, question in enumerate(rule['questions'], start=1):
                suffix = f'r{repeat}-{rule["key"]}-q{question_index}'
                inputs = {
                    'companyId': company_id,
                    'scopeId': scope_id,
                    'query': question,
                    'targetEntryId': entry.id,
                    'ruleKey': rule['key'],
                }
                rows.append({
                    'caseId': f'{suffix}-retrieval',
                    'task': 'retrieval',
                    'input': inputs,
                    'gold': {'relevantIds': relevant_ids},
                })
                rows.append({
                    'caseId': f'{suffix}-qna',
                    'task': 'qna',
                    'input': {
                        'companyId': company_id,
                        'scopeId': scope_id,
                        'lang': 'en',
                        'question': question,
                        'targetEntryId': entry.id,
                        'ruleKey': rule['key'],
                    },
                    'gold': {
                        'shouldEscalate': False,
                        'verdict': 'GROUNDED',
                        'requiredFacts': rule['facts'],
                        'forbiddenFacts': [],
                        'citationIds': relevant_ids,
                    },
                })
        for key, question, verdict, should_escalate in UNSUPPORTED_CASES:
            rows.append({
                'caseId': f'r{repeat}-unsupported-{key}-qna',
                'task': 'qna',
                'input': {
                    'companyId': company_id,
                    'scopeId': scope_id,
                    'lang': 'en',
                    'question': question,
                },
                'gold': {
                    'shouldEscalate': should_escalate,
                    'verdict': verdict,
                    'requiredFacts': [],
                    'forbiddenFacts': [],
                    'citationIds': [],
                },
            })
    return validate_dataset(rows)


def build_repeat_reports(dataset, predictions_by_profile, *, retrieval_k=5):
    """Score each paid repetition independently without regenerating outputs."""
    repeat_numbers = sorted({
        int(match.group('repeat'))
        for row in dataset
        if (match := _REPEAT_CASE_ID.match(row['caseId']))
    })
    reports = {}
    for profile, predictions in predictions_by_profile.items():
        predictions_by_id = {row['caseId']: row for row in predictions}
        profile_reports = {}
        for repeat in repeat_numbers:
            prefix = f'r{repeat}-'
            repeat_dataset = [
                row for row in dataset if row['caseId'].startswith(prefix)
            ]
            repeat_predictions = [
                predictions_by_id[row['caseId']]
                for row in repeat_dataset
                if row['caseId'] in predictions_by_id
            ]
            profile_reports[f'r{repeat}'] = score(
                repeat_dataset,
                repeat_predictions,
                retrieval_k=retrieval_k,
            )
        reports[profile] = profile_reports
    return reports


def _case_key(row):
    return row['input'].get('ruleKey')


def relabel_related_rules(dataset, candidate_entry_ids=None):
    entry_ids = {
        _case_key(row): f"handbook:{row['input']['targetEntryId']}"
        for row in dataset
        if _case_key(row) and row['input'].get('targetEntryId') is not None
    }
    if len(entry_ids) != len(RULES):
        observed_ids = sorted(
            set(candidate_entry_ids or ()) or {
                value
                for row in dataset
                for value in (
                    row['gold']['relevantIds']
                    if row['task'] == 'retrieval'
                    else row['gold']['citationIds']
                )
            },
            key=lambda value: int(value.split(':', 1)[1]),
        )
        if len(observed_ids) != len(RULES):
            raise ValueError('cannot recover synthetic target entry IDs')
        entry_ids = {
            rule['key']: value for rule, value in zip(RULES, observed_ids)
        }

    for row in dataset:
        key = _case_key(row)
        if not key:
            continue
        row['input']['targetEntryId'] = int(entry_ids[key].split(':', 1)[1])

    rules_by_key = {rule['key']: rule for rule in RULES}
    for row in dataset:
        key = _case_key(row)
        if not key:
            continue
        rule = rules_by_key[key]
        relevant_ids = [
            entry_ids[related]
            for related in rule.get('related', [key])
        ]
        if row['task'] == 'retrieval':
            row['gold']['relevantIds'] = relevant_ids
        else:
            row['gold']['citationIds'] = relevant_ids
    return validate_dataset(dataset)


def rescore(output_dir):
    output_dir = Path(output_dir)
    dataset_path = output_dir / 'synthetic-rag-dataset.jsonl'
    predictions_by_profile = {
        report_name: read_jsonl(output_dir / f'{report_name}.jsonl')
        for report_name, _runner_profile in PROFILE_RUNS
    }
    candidate_entry_ids = {
        value
        for predictions in predictions_by_profile.values()
        for prediction in predictions
        for value in (prediction.get('output') or {}).get('rankedIds', [])
        if value.startswith('handbook:')
    }
    dataset = relabel_related_rules(
        read_jsonl(dataset_path), candidate_entry_ids=candidate_entry_ids
    )
    write_jsonl(dataset_path, dataset)
    dataset_sha256 = _sha256(dataset_path)
    for report_name, _runner_profile in PROFILE_RUNS:
        manifest_path = output_dir / f'{report_name}-manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['datasetSha256'] = dataset_sha256
        write_json(manifest_path, manifest)
    report_path = output_dir / 'synthetic-rag-ablation.json'
    previous = json.loads(report_path.read_text(encoding='utf-8'))
    report = build_ablation_report(
        dataset, predictions_by_profile, retrieval_k=5
    )
    report['repeatReports'] = build_repeat_reports(
        dataset, predictions_by_profile, retrieval_k=5
    )
    for key in (
        'measurementType', 'dataset', 'fixtureSetupUsage', 'apiUsageByProfile',
        'models', 'generationConfiguration', 'elapsedMs', 'totalApiCostUsd',
    ):
        if key in previous:
            report[key] = previous[key]
    report['datasetSha256'] = dataset_sha256
    report['goldAdjudication'] = {
        'relatedRulesAdjudicated': True,
        'performedAfterPrediction': True,
        'modelOutputsRegenerated': False,
        'reason': (
            'Semantically overlapping confirmed rules were accepted as relevant '
            'citations; questions, inputs, and model outputs were unchanged.'
        ),
    }
    write_json(report_path, report)
    return report


def run(output_dir, repeats=3):
    from companies.models import Company
    from handbook.finalizing import _embed, _get_client
    from handbook.models import CompanyScope, HandbookEntry

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    with transaction.atomic():
        company = Company.objects.create(
            name='SAi Synthetic RAG Benchmark',
            code=f'BR{uuid.uuid4().hex[:12].upper()}',
        )
        scope = CompanyScope.objects.create(
            company=company,
            kind=CompanyScope.Kind.COMPANY,
            area_key=CompanyScope.AreaKey.PRODUCT_ENG,
            name='Synthetic Engineering Rules',
        )
        now = timezone.now()
        entries = [
            HandbookEntry.objects.create(
                company=company,
                scope=scope,
                title=rule['title'],
                title_en=rule['title'],
                body_en=rule['body'],
                original_lang='en',
                status=HandbookEntry.Status.CONFIRMED,
                origin=HandbookEntry.Origin.DIRECT_ENTRY,
                confirmed_at=now,
                translated_at=now,
            )
            for rule in RULES
        ]
        entries_by_key = {
            rule['key']: entry for rule, entry in zip(RULES, entries)
        }

        setup_usage = UsageCollector()
        with patch('handbook.finalizing.record_usage', setup_usage.record):
            embedded = _embed(_get_client(), entries)
        if len(embedded) != len(entries):
            raise RuntimeError('synthetic fixture embedding was incomplete')

        dataset = build_dataset(
            company.id, scope.id, entries_by_key, repeats=repeats
        )
        dataset_path = output_dir / 'synthetic-rag-dataset.jsonl'
        write_jsonl(dataset_path, dataset)

        predictions_by_profile = {}
        usage_by_profile = {}
        manifests = {}
        for report_name, runner_profile in PROFILE_RUNS:
            collector = UsageCollector()
            with patch('qna.answering.record_usage', collector.record):
                predictions = current_predictions(
                    dataset,
                    tasks=('retrieval', 'qna'),
                    profile=runner_profile,
                )
            predictions_by_profile[report_name] = predictions
            usage_by_profile[report_name] = collector.summary()
            write_jsonl(output_dir / f'{report_name}.jsonl', predictions)
            manifest = environment_manifest(dataset_path, profile=runner_profile)
            manifests[report_name] = manifest
            write_json(output_dir / f'{report_name}-manifest.json', manifest)

        report = build_ablation_report(
            dataset, predictions_by_profile, retrieval_k=5
        )
        report['repeatReports'] = build_repeat_reports(
            dataset, predictions_by_profile, retrieval_k=5
        )
        report.update({
            'measurementType': 'measured-synthetic-ablation',
            'dataset': {
                'rules': len(RULES),
                'uniqueQuestions': (
                    sum(len(rule['questions']) for rule in RULES)
                    + len(UNSUPPORTED_CASES)
                ),
                'unsupportedQuestions': len(UNSUPPORTED_CASES),
                'repeats': repeats,
                'retrievalCases': sum(
                    len(rule['questions']) for rule in RULES
                ) * repeats,
                'qnaCases': (
                    sum(len(rule['questions']) for rule in RULES)
                    + len(UNSUPPORTED_CASES)
                ) * repeats,
            },
            'fixtureSetupUsage': setup_usage.summary(),
            'apiUsageByProfile': usage_by_profile,
            'models': manifests['current']['models'],
            'generationConfiguration': (
                manifests['current']['generationConfiguration']
            ),
            'elapsedMs': round((time.perf_counter() - started) * 1000, 3),
            'datasetSha256': _sha256(dataset_path),
            'goldAdjudication': {
                'relatedRulesAdjudicated': True,
                'performedAfterPrediction': False,
                'modelOutputsRegenerated': True,
            },
        })
        report['totalApiCostUsd'] = round(
            report['fixtureSetupUsage']['costUsd']
            + sum(value['costUsd'] for value in usage_by_profile.values()),
            8,
        )
        write_json(output_dir / 'synthetic-rag-ablation.json', report)
        transaction.set_rollback(True)

    return report
