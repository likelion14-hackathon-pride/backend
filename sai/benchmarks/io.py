import json
from pathlib import Path


class BenchmarkFormatError(ValueError):
    pass


def read_jsonl(path):
    path = Path(path)
    rows = []
    seen = set()
    with path.open(encoding='utf-8') as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise BenchmarkFormatError(
                    f'{path}:{line_number}: invalid JSON: {exc.msg}'
                ) from exc
            if not isinstance(row, dict):
                raise BenchmarkFormatError(
                    f'{path}:{line_number}: each row must be a JSON object'
                )
            case_id = row.get('caseId')
            if not isinstance(case_id, str) or not case_id.strip():
                raise BenchmarkFormatError(
                    f'{path}:{line_number}: caseId must be a non-empty string'
                )
            if case_id in seen:
                raise BenchmarkFormatError(
                    f'{path}:{line_number}: duplicate caseId {case_id!r}'
                )
            seen.add(case_id)
            rows.append(row)
    return rows


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            stream.write('\n')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')
