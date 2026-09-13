from .io import BenchmarkFormatError


TASKS = {'classification', 'drafting', 'promotion', 'retrieval', 'qna', 'cards'}
PROMOTION_TYPES = {'AUTO_PROMOTED', 'PENDING_REVIEW', 'MANUAL_REQUIRED'}


def _require_mapping(row, key, case_id):
    value = row.get(key)
    if not isinstance(value, dict):
        raise BenchmarkFormatError(f'{case_id}: {key} must be an object')
    return value


def _require_string(value, name, case_id):
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkFormatError(f'{case_id}: {name} must be a non-empty string')


def _require_string_list(value, name, case_id):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise BenchmarkFormatError(f'{case_id}: {name} must be a string array')


def validate_dataset(rows):
    for row in rows:
        case_id = row['caseId']
        task = row.get('task')
        if task not in TASKS:
            raise BenchmarkFormatError(
                f'{case_id}: task must be one of {sorted(TASKS)}'
            )
        _require_mapping(row, 'input', case_id)
        gold = _require_mapping(row, 'gold', case_id)

        if task == 'classification':
            _require_string(gold.get('label'), 'gold.label', case_id)
        elif task == 'drafting':
            rules = gold.get('rules')
            if not isinstance(rules, list):
                raise BenchmarkFormatError(f'{case_id}: gold.rules must be an array')
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    raise BenchmarkFormatError(
                        f'{case_id}: gold.rules[{index}] must be an object'
                    )
                facts = rule.get('requiredFacts')
                _require_string_list(
                    facts, f'gold.rules[{index}].requiredFacts', case_id
                )
                if not facts:
                    raise BenchmarkFormatError(
                        f'{case_id}: gold.rules[{index}].requiredFacts must not be empty'
                    )
                _require_string_list(
                    rule.get('evidenceIds', []),
                    f'gold.rules[{index}].evidenceIds',
                    case_id,
                )
            _require_string_list(
                gold.get('forbiddenFacts', []), 'gold.forbiddenFacts', case_id
            )
        elif task == 'promotion':
            if gold.get('promotionType') not in PROMOTION_TYPES:
                raise BenchmarkFormatError(
                    f'{case_id}: gold.promotionType must be one of '
                    f'{sorted(PROMOTION_TYPES)}'
                )
            if not isinstance(gold.get('critical', False), bool):
                raise BenchmarkFormatError(
                    f'{case_id}: gold.critical must be a boolean'
                )
        elif task == 'retrieval':
            _require_string_list(
                gold.get('relevantIds'), 'gold.relevantIds', case_id
            )
            if not gold['relevantIds']:
                raise BenchmarkFormatError(
                    f'{case_id}: gold.relevantIds must not be empty'
                )
            if 'relevantDocumentIds' in gold:
                _require_string_list(
                    gold['relevantDocumentIds'],
                    'gold.relevantDocumentIds',
                    case_id,
                )
                if not gold['relevantDocumentIds']:
                    raise BenchmarkFormatError(
                        f'{case_id}: gold.relevantDocumentIds must not be empty'
                    )
        elif task == 'qna':
            if not isinstance(gold.get('shouldEscalate'), bool):
                raise BenchmarkFormatError(
                    f'{case_id}: gold.shouldEscalate must be a boolean'
                )
            for key in ('requiredFacts', 'forbiddenFacts', 'citationIds'):
                _require_string_list(gold.get(key, []), f'gold.{key}', case_id)
            if 'citationDocumentIds' in gold:
                _require_string_list(
                    gold['citationDocumentIds'],
                    'gold.citationDocumentIds',
                    case_id,
                )
        elif task == 'cards':
            if not isinstance(gold.get('isInstruction'), bool):
                raise BenchmarkFormatError(
                    f'{case_id}: gold.isInstruction must be a boolean'
                )
            for key in (
                'purposeFacts', 'deliverableFacts', 'deadlineFacts',
                'stepFacts', 'blankFacts', 'forbiddenFacts',
            ):
                _require_string_list(gold.get(key, []), f'gold.{key}', case_id)
            if (
                gold.get('expectedDeadlineInferred') is not None
                and not isinstance(gold['expectedDeadlineInferred'], bool)
            ):
                raise BenchmarkFormatError(
                    f'{case_id}: gold.expectedDeadlineInferred must be a boolean'
                )
    return rows


def validate_predictions(rows):
    for row in rows:
        case_id = row['caseId']
        if 'error' in row and row['error'] is not None:
            _require_string(row['error'], 'error', case_id)
            continue
        _require_mapping(row, 'output', case_id)
        telemetry = row.get('telemetry', {})
        if not isinstance(telemetry, dict):
            raise BenchmarkFormatError(f'{case_id}: telemetry must be an object')
        for key in ('latencyMs', 'promptTokens', 'completionTokens', 'costUsd'):
            value = telemetry.get(key)
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                raise BenchmarkFormatError(
                    f'{case_id}: telemetry.{key} must be a non-negative number'
                )
    return rows
