import hashlib
import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.test.utils import override_settings
from openai import OpenAI

from cards.generation import CardDraft, JudgementResult
from cards.prompts import CARD_PROMPT, JUDGE_PROMPT
from config.ai import client_options, generation_options
from handbook.drafting import DraftResult, SYSTEM_PROMPT as DRAFT_PROMPT
from handbook.finalizing import TRANSLATE_PROMPT, TranslationResult
from qna.answering import SYSTEM_PROMPT as ANSWER_PROMPT
from qna.answering import AnswerResult
from qna.answering import _ask as ask_with_context
from sources.classifier import ClassificationResult
from sources.classifier import SYSTEM_PROMPT as CLASSIFIER_PROMPT

from .pricing import PRICES_PER_MILLION


PROFILES = {
    'current': {
        'classifier': ('gpt-5.6-luna', 'medium', 'low'),
        'drafter': ('gpt-5.6-sol', 'high', 'medium'),
        'answer': ('gpt-5.6-sol', 'high', 'medium'),
        'translator': ('gpt-5.6-terra', 'low', 'low'),
    },
    'cost_optimized': {
        'classifier': ('gpt-5.6-luna', 'medium', 'low'),
        'drafter': ('gpt-5.6-terra', 'medium', 'medium'),
        'answer': ('gpt-5.6-terra', 'medium', 'medium'),
        'translator': ('gpt-5.6-luna', 'low', 'low'),
    },
}


def _normalise(value):
    return re.sub(r'[^0-9a-zA-Z가-힣#]+', ' ', value or '').strip().lower()


def _groups_pass(text, groups):
    text = _normalise(text)
    return all(any(_normalise(term) in text for term in group) for group in groups)


def _usage(completion, model, latency_ms):
    prompt = getattr(completion.usage, 'prompt_tokens', 0) if completion.usage else 0
    output = getattr(completion.usage, 'completion_tokens', 0) if completion.usage else 0
    prices = PRICES_PER_MILLION[model]
    return {
        'model': model,
        'promptTokens': prompt,
        'completionTokens': output,
        'latencyMs': round(latency_ms, 3),
        'costUsd': round(
            prompt / 1_000_000 * prices['input']
            + output / 1_000_000 * prices['output'],
            8,
        ),
    }


def _parse(client, *, model, effort, verbosity, system, user, schema):
    started = time.perf_counter()
    completion = client.chat.completions.parse(
        model=model,
        messages=[
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        response_format=schema,
        **generation_options(model, reasoning_effort=effort, verbosity=verbosity),
    )
    elapsed = (time.perf_counter() - started) * 1000
    return completion.choices[0].message.parsed, _usage(completion, model, elapsed)


def _classification(client, cases, profile):
    model, effort, verbosity = profile['classifier']
    user = '\n'.join(f'[{i}] {case["text"]}' for i, case in enumerate(cases))
    parsed, usage = _parse(
        client, model=model, effort=effort, verbosity=verbosity,
        system=CLASSIFIER_PROMPT, user=user, schema=ClassificationResult,
    )
    labels = {item.index: item.label for item in parsed.labels}
    outcomes = [
        {
            'id': case['id'],
            'expected': case['label'],
            'actual': labels.get(index),
            'passed': labels.get(index) == case['label'],
        }
        for index, case in enumerate(cases)
    ]
    return {
        'cases': len(cases),
        'correct': sum(row['passed'] for row in outcomes),
        'accuracy': round(sum(row['passed'] for row in outcomes) / len(cases), 6),
        'outcomes': outcomes,
        'usage': [usage],
    }


def _card_judge(client, cases, profile):
    model, effort, verbosity = profile['classifier']
    user = '\n'.join(
        f'[{i}] source=SLACK type=message {case["text"]}'
        for i, case in enumerate(cases)
    )
    parsed, usage = _parse(
        client, model=model, effort=effort, verbosity=verbosity,
        system=JUDGE_PROMPT, user=user, schema=JudgementResult,
    )
    decisions = {item.index: item.is_instruction for item in parsed.judgements}
    outcomes = [
        {
            'id': case['id'],
            'expected': case['isInstruction'],
            'actual': decisions.get(index),
            'passed': decisions.get(index) == case['isInstruction'],
        }
        for index, case in enumerate(cases)
    ]
    return {
        'cases': len(cases),
        'correct': sum(row['passed'] for row in outcomes),
        'accuracy': round(sum(row['passed'] for row in outcomes) / len(cases), 6),
        'outcomes': outcomes,
        'usage': [usage],
    }


def _drafting(client, data, profile):
    model, effort, verbosity = profile['drafter']
    rendered = '\n'.join(
        f'[{row["id"]}] source=SLACK location=#dev author=owner at=2026-09-01\n'
        f'    text: {row["text"]}'
        for row in data['documents']
    )
    parsed, usage = _parse(
        client, model=model, effort=effort, verbosity=verbosity,
        system=DRAFT_PROMPT,
        user=f'{data["scopePrompt"]}\n\n{rendered}',
        schema=DraftResult,
    )
    predictions = [
        {
            'title': rule.title,
            'body': rule.body,
            'confidence': rule.confidence,
            'citationIndexes': [citation.index for citation in rule.citations],
        }
        for rule in parsed.rules
    ]
    available = set(range(len(predictions)))
    matches = []
    for expected in data['goldRules']:
        matched = next((
            index for index in available
            if _groups_pass(
                f'{predictions[index]["title"]} {predictions[index]["body"]}',
                expected['requiredTermGroups'],
            )
        ), None)
        evidence_recall = 0.0
        if matched is not None:
            available.remove(matched)
            cited = set(predictions[matched]['citationIndexes'])
            gold = set(expected['evidenceIndexes'])
            evidence_recall = len(cited & gold) / len(gold)
        matches.append({
            'id': expected['id'],
            'matched': matched is not None,
            'evidenceRecall': round(evidence_recall, 6),
        })
    matched_count = sum(row['matched'] for row in matches)
    precision = matched_count / len(predictions) if predictions else 0.0
    recall = matched_count / len(data['goldRules'])
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    rendered_predictions = ' '.join(
        f'{row["title"]} {row["body"]}' for row in predictions
    )
    forbidden = any(
        _groups_pass(rendered_predictions, [group])
        for group in data['forbiddenTermGroups']
    )
    return {
        'goldRules': len(data['goldRules']),
        'predictedRules': len(predictions),
        'rulePrecision': round(precision, 6),
        'ruleRecall': round(recall, 6),
        'ruleF1': round(f1, 6),
        'evidenceRecall': round(
            sum(row['evidenceRecall'] for row in matches) / len(matches), 6
        ),
        'forbiddenRuleViolation': forbidden,
        'matches': matches,
        'predictions': predictions,
        'usage': [usage],
    }


def _entry(row):
    scope = SimpleNamespace(name=row['scopeName'], kind=row['scopeKind'])
    return SimpleNamespace(
        title=row['title'], body_en=row['body'], body_ko=None, scope=scope
    )


def _qna(client, cases, profile):
    model, effort, verbosity = profile['answer']
    outcomes = []
    usages = []
    for case in cases:
        scope = SimpleNamespace(**case['scope']) if case.get('scope') else None
        entries = [_entry(row) for row in case['rules']]
        with override_settings(
            OPENAI_ANSWER_MODEL=model,
            OPENAI_ANSWER_REASONING_EFFORT=effort,
            OPENAI_ANSWER_VERBOSITY=verbosity,
        ):
            started = time.perf_counter()
            completion = ask_with_context(
                client, case['question'], 'en', entries, [], scope
            )
            elapsed = (time.perf_counter() - started) * 1000
        parsed = completion.choices[0].message.parsed
        usage = _usage(completion, model, elapsed)
        usages.append(usage)
        cited = set(parsed.cited_indexes)
        facts_passed = _groups_pass(parsed.answer, case['requiredTermGroups'])
        citations_passed = all(
            index in cited for index in case['requiredCitationIndexes']
        )
        if not case['requiredCitationIndexes'] and parsed.verdict in {
            'NO_SOURCE', 'OUT_OF_SCOPE'
        }:
            citations_passed = not cited
        verdict_passed = parsed.verdict == case['expectedVerdict']
        outcomes.append({
            'id': case['id'],
            'expectedVerdict': case['expectedVerdict'],
            'actualVerdict': parsed.verdict,
            'verdictPassed': verdict_passed,
            'factsPassed': facts_passed,
            'citationsPassed': citations_passed,
            'passed': verdict_passed and facts_passed and citations_passed,
            'answer': parsed.answer,
            'citedIndexes': parsed.cited_indexes,
        })
    count = len(outcomes)
    return {
        'cases': count,
        'casePassRate': round(sum(row['passed'] for row in outcomes) / count, 6),
        'verdictAccuracy': round(
            sum(row['verdictPassed'] for row in outcomes) / count, 6
        ),
        'factPassRate': round(sum(row['factsPassed'] for row in outcomes) / count, 6),
        'citationPassRate': round(
            sum(row['citationsPassed'] for row in outcomes) / count, 6
        ),
        'outcomes': outcomes,
        'usage': usages,
    }


def _cards(client, cases, profile):
    model, effort, verbosity = profile['drafter']
    outcomes = []
    usages = []
    for case in cases:
        user = (
            'Company timezone: Asia/Seoul\n'
            'Current time: 2026-09-13 10:00 (Sunday)\n'
            'Working hours end: 18:00\n\n'
            f'Source: SLACK / #dev\nContent from owner:\n{case["text"]}\n\n'
            'Company rules that may apply:\n(no rules)\n\n'
            'Past messages with similar phrasing (for tone_note):\n(no past cases)'
        )
        parsed, usage = _parse(
            client, model=model, effort=effort, verbosity=verbosity,
            system=CARD_PROMPT, user=user, schema=CardDraft,
        )
        usages.append(usage)
        content = ' '.join([
            parsed.purpose, parsed.deliverable,
            ' '.join(step.text for step in parsed.steps),
        ])
        content_passed = _groups_pass(content, case['requiredTermGroups'])
        deadline_passed = _groups_pass(
            parsed.deadline_text, case['deadlineTermGroups']
        )
        urgency_passed = parsed.urgency == case['urgency']
        outcomes.append({
            'id': case['id'],
            'expectedUrgency': case['urgency'],
            'actualUrgency': parsed.urgency,
            'contentPassed': content_passed,
            'deadlinePassed': deadline_passed,
            'urgencyPassed': urgency_passed,
            'passed': content_passed and deadline_passed and urgency_passed,
            'purpose': parsed.purpose,
            'deliverable': parsed.deliverable,
            'deadlineText': parsed.deadline_text,
        })
    count = len(outcomes)
    return {
        'cases': count,
        'casePassRate': round(sum(row['passed'] for row in outcomes) / count, 6),
        'contentPassRate': round(
            sum(row['contentPassed'] for row in outcomes) / count, 6
        ),
        'deadlinePassRate': round(
            sum(row['deadlinePassed'] for row in outcomes) / count, 6
        ),
        'urgencyAccuracy': round(
            sum(row['urgencyPassed'] for row in outcomes) / count, 6
        ),
        'outcomes': outcomes,
        'usage': usages,
    }


def _translations(client, cases, profile):
    model, effort, verbosity = profile['translator']
    user = '\n\n'.join(
        f'[{index}] from=ko\ntitle: {case["title"]}\nbody: {case["body"]}'
        for index, case in enumerate(cases)
    )
    parsed, usage = _parse(
        client, model=model, effort=effort, verbosity=verbosity,
        system=TRANSLATE_PROMPT, user=user, schema=TranslationResult,
    )
    translated = {row.index: row for row in parsed.translations}
    outcomes = []
    for index, case in enumerate(cases):
        row = translated.get(index)
        text = f'{row.title} {row.text}' if row else ''
        passed = bool(row) and _groups_pass(text, case['requiredTermGroups'])
        outcomes.append({
            'id': case['id'],
            'passed': passed,
            'title': row.title if row else None,
            'text': row.text if row else None,
        })
    return {
        'cases': len(cases),
        'passRate': round(sum(row['passed'] for row in outcomes) / len(cases), 6),
        'outcomes': outcomes,
        'usage': [usage],
    }


def _usage_total(tasks):
    rows = [usage for task in tasks.values() for usage in task.get('usage', [])]
    return {
        'apiCalls': len(rows),
        'promptTokens': sum(row['promptTokens'] for row in rows),
        'completionTokens': sum(row['completionTokens'] for row in rows),
        'costUsd': round(sum(row['costUsd'] for row in rows), 6),
        'latencyMs': round(sum(row['latencyMs'] for row in rows), 3),
    }


def run(dataset_path, output_path):
    dataset_path = Path(dataset_path)
    dataset_bytes = dataset_path.read_bytes()
    with dataset_path.open(encoding='utf-8') as stream:
        data = json.load(stream)
    client = OpenAI(api_key=settings.OPENAI_API_KEY, **client_options())

    # These use the same Luna configuration in both profiles, so measure once and
    # reuse the identical result instead of paying twice.
    shared = {
        'classification': _classification(
            client, data['classification'], PROFILES['current']
        ),
        'cardJudge': _card_judge(client, data['cardJudge'], PROFILES['current']),
    }
    profiles = {}
    for name, profile in PROFILES.items():
        tasks = {
            **shared,
            'drafting': _drafting(client, data['drafting'], profile),
            'qna': _qna(client, data['qna'], profile),
            'cards': _cards(client, data['cards'], profile),
            'translation': _translations(client, data['translations'], profile),
        }
        profiles[name] = {
            'models': {
                key: {'model': value[0], 'effort': value[1], 'verbosity': value[2]}
                for key, value in profile.items()
            },
            'tasks': tasks,
            'usage': _usage_total(tasks),
        }

    shared_api_calls = sum(
        len(task.get('usage', [])) for task in shared.values()
    )
    report = {
        'schemaVersion': 1,
        'benchmark': data['name'],
        'dataset': str(dataset_path),
        'datasetSha256': hashlib.sha256(dataset_bytes).hexdigest(),
        'pricingAsOf': '2026-09-13',
        'measurementType': 'live-openai-api',
        'physicalApiCalls': (
            sum(profile['usage']['apiCalls'] for profile in profiles.values())
            - shared_api_calls
        ),
        'sharedMeasurementsReused': ['classification', 'cardJudge'],
        'profiles': profiles,
    }
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return report
