import math
import re
from collections import Counter, defaultdict
from statistics import median

from .io import BenchmarkFormatError


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _round(value):
    return round(value, 6) if isinstance(value, float) else value


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _normalise_text(value):
    return re.sub(r'[^0-9a-zA-Z가-힣]+', ' ', value or '').strip().lower()


def _classification(cases):
    labels = sorted({gold['label'] for gold, _ in cases})
    correct = sum(output.get('label') == gold['label'] for gold, output in cases)
    per_label = {}
    for label in labels:
        tp = fp = fn = 0
        for gold, output in cases:
            expected = gold['label']
            predicted = output.get('label')
            tp += int(expected == label and predicted == label)
            fp += int(expected != label and predicted == label)
            fn += int(expected == label and predicted != label)
        precision = _ratio(tp, tp + fp) or 0.0
        recall = _ratio(tp, tp + fn) or 0.0
        f1 = _ratio(2 * precision * recall, precision + recall) or 0.0
        per_label[label] = {
            'precision': _round(precision),
            'recall': _round(recall),
            'f1': _round(f1),
            'support': sum(gold['label'] == label for gold, _ in cases),
        }
    return {
        'count': len(cases),
        'accuracy': _round(_ratio(correct, len(cases))),
        'macroF1': _round(
            sum(value['f1'] for value in per_label.values()) / len(per_label)
        ),
        'byLabel': per_label,
    }


def _fact_recall(text, facts):
    if not facts:
        return None
    text = _normalise_text(text)
    normalised = [_normalise_text(fact) for fact in facts]
    return sum(bool(fact) and fact in text for fact in normalised) / len(normalised)


def _drafting(cases):
    gold_count = predicted_count = matched_gold = matched_predictions = 0
    fact_scores = []
    scope_correct = scope_compared = 0
    evidence_scores = []
    forbidden_cases = forbidden_violations = 0

    for gold, output in cases:
        expected_rules = gold['rules']
        predicted_rules = output.get('rules', [])
        gold_count += len(expected_rules)
        predicted_count += len(predicted_rules)
        available = set(range(len(predicted_rules)))

        for expected in expected_rules:
            ranked = sorted(
                (
                    (_fact_recall(
                        ' '.join(filter(None, [rule.get('title'), rule.get('body')])),
                        expected['requiredFacts'],
                    ) or 0.0, index)
                    for index, rule in enumerate(predicted_rules)
                    if index in available
                ),
                reverse=True,
            )
            best_score, best_index = ranked[0] if ranked else (0.0, None)
            fact_scores.append(best_score)
            # A rule is a deterministic match only when all required facts are present.
            if best_index is None or best_score < 1.0:
                continue
            matched_gold += 1
            matched_predictions += 1
            available.remove(best_index)
            predicted = predicted_rules[best_index]

            if expected.get('scopeId') is not None:
                scope_compared += 1
                scope_correct += int(predicted.get('scopeId') == expected['scopeId'])

            expected_evidence = set(expected.get('evidenceIds', []))
            if expected_evidence:
                actual_evidence = set(predicted.get('evidenceIds', []))
                evidence_scores.append(
                    len(expected_evidence & actual_evidence) / len(expected_evidence)
                )

        forbidden = gold.get('forbiddenFacts', [])
        if forbidden:
            forbidden_cases += 1
            rendered = ' '.join(
                ' '.join(filter(None, [rule.get('title'), rule.get('body')]))
                for rule in predicted_rules
            )
            forbidden_violations += int((_fact_recall(rendered, forbidden) or 0) > 0)

    precision = _ratio(matched_predictions, predicted_count)
    recall = _ratio(matched_gold, gold_count)
    f1 = None
    if precision is not None and recall is not None:
        f1 = _ratio(2 * precision * recall, precision + recall) or 0.0
    return {
        'count': len(cases),
        'goldRuleCount': gold_count,
        'predictedRuleCount': predicted_count,
        'rulePrecision': _round(precision),
        'ruleRecall': _round(recall),
        'ruleF1': _round(f1),
        'requiredFactRecall': _round(
            sum(fact_scores) / len(fact_scores) if fact_scores else None
        ),
        'scopeAccuracy': _round(_ratio(scope_correct, scope_compared)),
        'evidenceRecall': _round(
            sum(evidence_scores) / len(evidence_scores) if evidence_scores else None
        ),
        'forbiddenFactViolationRate': _round(
            _ratio(forbidden_violations, forbidden_cases)
        ),
    }


def _promotion(cases):
    auto = correct_auto = safe_gold = critical = critical_escapes = correct = 0
    predicted_counts = Counter()
    for gold, output in cases:
        expected = gold['promotionType']
        predicted = output.get('promotionType')
        is_critical = gold.get('critical', False)
        predicted_counts[predicted or 'MISSING'] += 1
        correct += int(expected == predicted)
        safe_gold += int(expected == 'AUTO_PROMOTED')
        critical += int(is_critical)
        if predicted == 'AUTO_PROMOTED':
            auto += 1
            correct_auto += int(expected == 'AUTO_PROMOTED')
            critical_escapes += int(is_critical)
    count = len(cases)
    return {
        'count': count,
        'accuracy': _round(_ratio(correct, count)),
        'autoCoverage': _round(_ratio(auto, count)),
        'autoPrecision': _round(_ratio(correct_auto, auto)),
        'safeAutoRecall': _round(_ratio(correct_auto, safe_gold)),
        'manualReviewRate': _round(_ratio(count - auto, count)),
        'criticalEscapeCount': critical_escapes,
        'criticalEscapeRate': _round(_ratio(critical_escapes, critical)),
        'predictedCounts': dict(sorted(predicted_counts.items())),
    }


def _ranking_metrics(cases, k, gold_key, output_key):
    recalls = []
    reciprocal_ranks = []
    ndcgs = []
    hits = 0
    for gold, output in cases:
        relevant = set(gold[gold_key])
        ranked = output.get(output_key, [])[:k]
        hits += int(any(item in relevant for item in ranked))
        recalls.append(len(relevant.intersection(ranked)) / len(relevant))
        first_rank = next(
            (index for index, item in enumerate(ranked, start=1) if item in relevant),
            None,
        )
        reciprocal_ranks.append(1 / first_rank if first_rank else 0.0)
        dcg = sum(
            1 / math.log2(index + 1)
            for index, item in enumerate(ranked, start=1)
            if item in relevant
        )
        ideal_count = min(len(relevant), k)
        idcg = sum(1 / math.log2(index + 1) for index in range(1, ideal_count + 1))
        ndcgs.append(dcg / idcg if idcg else 0.0)
    count = len(cases)
    return {
        'count': count,
        f'hitRate@{k}': _round(_ratio(hits, count)),
        f'recall@{k}': _round(sum(recalls) / count),
        f'mrr@{k}': _round(sum(reciprocal_ranks) / count),
        f'ndcg@{k}': _round(sum(ndcgs) / count),
    }


def _retrieval(cases, k):
    result = _ranking_metrics(cases, k, 'relevantIds', 'rankedIds')
    document_cases = [
        (gold, output)
        for gold, output in cases
        if gold.get('relevantDocumentIds')
    ]
    if document_cases:
        document_metrics = _ranking_metrics(
            document_cases, k, 'relevantDocumentIds', 'rankedDocumentIds'
        )
        for key, value in document_metrics.items():
            result[f'document{key[0].upper()}{key[1:]}'] = value

    return result


def _qna(cases):
    escalation_correct = 0
    fact_scores = []
    forbidden_violations = 0
    forbidden_cases = 0
    citation_tp = citation_fp = citation_fn = 0
    document_citation_tp = document_citation_fp = document_citation_fn = 0
    document_citation_cases = 0
    verdict_correct = verdict_compared = 0
    for gold, output in cases:
        escalation_correct += int(
            bool(output.get('escalated')) == gold['shouldEscalate']
        )
        if gold.get('verdict') is not None:
            verdict_compared += 1
            verdict_correct += int(output.get('verdict') == gold['verdict'])
        answer = _normalise_text(output.get('answer', ''))
        required = [_normalise_text(item) for item in gold.get('requiredFacts', [])]
        if required:
            fact_scores.append(sum(item in answer for item in required) / len(required))
        forbidden = [_normalise_text(item) for item in gold.get('forbiddenFacts', [])]
        if forbidden:
            forbidden_cases += 1
            forbidden_violations += int(any(item in answer for item in forbidden))
        expected_citations = set(gold.get('citationIds', []))
        predicted_citations = set(output.get('citationIds', []))
        citation_tp += len(expected_citations & predicted_citations)
        citation_fp += len(predicted_citations - expected_citations)
        citation_fn += len(expected_citations - predicted_citations)
        if 'citationDocumentIds' in gold:
            document_citation_cases += 1
            expected_documents = set(gold.get('citationDocumentIds', []))
            predicted_documents = set(output.get('citationDocumentIds', []))
            document_citation_tp += len(expected_documents & predicted_documents)
            document_citation_fp += len(predicted_documents - expected_documents)
            document_citation_fn += len(expected_documents - predicted_documents)
    citation_precision = _ratio(citation_tp, citation_tp + citation_fp)
    citation_recall = _ratio(citation_tp, citation_tp + citation_fn)
    citation_f1 = None
    if citation_precision is not None and citation_recall is not None:
        citation_f1 = _ratio(
            2 * citation_precision * citation_recall,
            citation_precision + citation_recall,
        ) or 0.0
    result = {
        'count': len(cases),
        'escalationAccuracy': _round(_ratio(escalation_correct, len(cases))),
        'verdictAccuracy': _round(_ratio(verdict_correct, verdict_compared)),
        'requiredFactRecall': _round(
            sum(fact_scores) / len(fact_scores) if fact_scores else None
        ),
        'forbiddenFactViolationRate': _round(
            _ratio(forbidden_violations, forbidden_cases)
        ),
        'citationPrecision': _round(citation_precision),
        'citationRecall': _round(citation_recall),
        'citationF1': _round(citation_f1),
    }
    if document_citation_cases:
        document_precision = _ratio(
            document_citation_tp,
            document_citation_tp + document_citation_fp,
        )
        document_recall = _ratio(
            document_citation_tp,
            document_citation_tp + document_citation_fn,
        )
        document_f1 = None
        if document_precision is not None and document_recall is not None:
            document_f1 = _ratio(
                2 * document_precision * document_recall,
                document_precision + document_recall,
            ) or 0.0
        result.update({
            'documentCitationCount': document_citation_cases,
            'documentCitationPrecision': _round(document_precision),
            'documentCitationRecall': _round(document_recall),
            'documentCitationF1': _round(document_f1),
        })

    return result


def _cards(cases):
    instruction_correct = 0
    positive = generated = 0
    purpose = []
    deliverable = []
    deadline = []
    steps = []
    blanks = []
    urgency_correct = urgency_compared = 0
    inferred_correct = inferred_compared = 0
    forbidden_cases = forbidden_violations = 0

    for gold, output in cases:
        predicted_instruction = bool(output.get('isInstruction'))
        instruction_correct += int(predicted_instruction == gold['isInstruction'])
        if not gold['isInstruction']:
            continue
        positive += 1
        card = output.get('card')
        if not isinstance(card, dict):
            continue
        generated += 1
        purpose.append(_fact_recall(card.get('purpose', ''), gold.get('purposeFacts', [])))
        deliverable.append(
            _fact_recall(card.get('deliverable', ''), gold.get('deliverableFacts', []))
        )
        deadline.append(
            _fact_recall(card.get('deadlineText', ''), gold.get('deadlineFacts', []))
        )
        steps.append(
            _fact_recall(
                ' '.join(card.get('steps', [])), gold.get('stepFacts', [])
            )
        )
        blanks.append(
            _fact_recall(
                ' '.join(card.get('blanks', [])), gold.get('blankFacts', [])
            )
        )
        if gold.get('urgency') is not None:
            urgency_compared += 1
            urgency_correct += int(card.get('urgency') == gold['urgency'])
        if gold.get('expectedDeadlineInferred') is not None:
            inferred_compared += 1
            inferred_correct += int(
                bool(card.get('isDeadlineInferred'))
                == gold['expectedDeadlineInferred']
            )
        forbidden = gold.get('forbiddenFacts', [])
        if forbidden:
            forbidden_cases += 1
            rendered = ' '.join(filter(None, [
                card.get('purpose'), card.get('deliverable'), card.get('deadlineText'),
                ' '.join(card.get('steps', [])),
            ]))
            forbidden_violations += int((_fact_recall(rendered, forbidden) or 0) > 0)

    def average_present(values):
        present = [value for value in values if value is not None]
        return _round(sum(present) / len(present)) if present else None

    return {
        'count': len(cases),
        'instructionAccuracy': _round(_ratio(instruction_correct, len(cases))),
        'cardGenerationRate': _round(_ratio(generated, positive)),
        'purposeFactRecall': average_present(purpose),
        'deliverableFactRecall': average_present(deliverable),
        'deadlineFactRecall': average_present(deadline),
        'stepFactRecall': average_present(steps),
        'blankFactRecall': average_present(blanks),
        'urgencyAccuracy': _round(_ratio(urgency_correct, urgency_compared)),
        'deadlineInferenceAccuracy': _round(
            _ratio(inferred_correct, inferred_compared)
        ),
        'forbiddenFactViolationRate': _round(
            _ratio(forbidden_violations, forbidden_cases)
        ),
    }


def _operations(predictions):
    latencies = []
    prompt_tokens = completion_tokens = 0
    cost = 0.0
    failures = 0
    for prediction in predictions:
        failures += int(bool(prediction.get('error')))
        telemetry = prediction.get('telemetry') or {}
        if telemetry.get('latencyMs') is not None:
            latencies.append(float(telemetry['latencyMs']))
        prompt_tokens += int(telemetry.get('promptTokens') or 0)
        completion_tokens += int(telemetry.get('completionTokens') or 0)
        cost += float(telemetry.get('costUsd') or 0)
    count = len(predictions)
    return {
        'count': count,
        'failureCount': failures,
        'failureRate': _round(_ratio(failures, count)),
        'latencyP50Ms': _round(float(median(latencies))) if latencies else None,
        'latencyP95Ms': _round(_percentile(latencies, 0.95)),
        'promptTokens': prompt_tokens,
        'completionTokens': completion_tokens,
        'totalTokens': prompt_tokens + completion_tokens,
        'costUsd': _round(cost),
    }


def score(dataset, predictions, *, retrieval_k=10):
    prediction_by_id = {row['caseId']: row for row in predictions}
    dataset_ids = {row['caseId'] for row in dataset}
    unknown = sorted(set(prediction_by_id) - dataset_ids)
    if unknown:
        raise BenchmarkFormatError(f'predictions contain unknown caseIds: {unknown}')

    missing = sorted(dataset_ids - set(prediction_by_id))
    successful = defaultdict(list)
    for case in dataset:
        prediction = prediction_by_id.get(case['caseId'])
        if prediction is None or prediction.get('error'):
            continue
        successful[case['task']].append((case['gold'], prediction['output']))

    metrics = {}
    if successful['classification']:
        metrics['classification'] = _classification(successful['classification'])
    if successful['drafting']:
        metrics['drafting'] = _drafting(successful['drafting'])
    if successful['promotion']:
        metrics['promotion'] = _promotion(successful['promotion'])
    if successful['retrieval']:
        metrics['retrieval'] = _retrieval(successful['retrieval'], retrieval_k)
    if successful['qna']:
        metrics['qna'] = _qna(successful['qna'])
    if successful['cards']:
        metrics['cards'] = _cards(successful['cards'])

    task_by_id = {row['caseId']: row['task'] for row in dataset}
    predictions_by_task = defaultdict(list)
    for prediction in predictions:
        task = task_by_id.get(prediction['caseId'])
        if task is not None:
            predictions_by_task[task].append(prediction)

    return {
        'schemaVersion': 1,
        'datasetCases': len(dataset),
        'scoredCases': sum(len(rows) for rows in successful.values()),
        'missingCaseIds': missing,
        'metrics': metrics,
        'operations': _operations(predictions),
        'operationsByTask': {
            task: _operations(rows)
            for task, rows in sorted(predictions_by_task.items())
        },
    }


def compare_reports(baseline, candidate):
    def numeric_delta(left, right):
        if isinstance(left, dict) and isinstance(right, dict):
            return {
                key: numeric_delta(left[key], right[key])
                for key in sorted(left.keys() & right.keys())
                if numeric_delta(left[key], right[key]) is not None
            }
        if (
            isinstance(left, (int, float)) and not isinstance(left, bool)
            and isinstance(right, (int, float)) and not isinstance(right, bool)
        ):
            return _round(right - left)
        return None

    return {
        'schemaVersion': 1,
        'baseline': baseline,
        'candidate': candidate,
        'deltaCandidateMinusBaseline': numeric_delta(baseline, candidate),
    }
