import argparse
import json
from pathlib import Path

from .io import read_jsonl, write_json, write_jsonl
from .promotion_runner import current_predictions, legacy_predictions
from .schema import validate_dataset, validate_predictions
from .scoring import compare_reports, score


def _dataset(path):
    return validate_dataset(read_jsonl(path))


def _predictions(path):
    return validate_predictions(read_jsonl(path))


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _profile_predictions(values):
    profiles = {}
    for value in values:
        name, separator, path = value.partition('=')
        name = name.strip()
        path = path.strip()
        if not separator or not name or not path:
            raise ValueError('--profile must use NAME=PREDICTIONS_JSONL')
        if name in profiles:
            raise ValueError(f'duplicate profile: {name}')
        profiles[name] = _predictions(path)
    return profiles


def build_parser():
    parser = argparse.ArgumentParser(
        prog='python -m benchmarks',
        description='Reproducible offline evaluation for the SAi AI pipeline.',
    )
    subparsers = parser.add_subparsers(dest='command', required=True)

    validate = subparsers.add_parser('validate', help='validate JSONL contracts')
    validate.add_argument('--dataset', required=True)
    validate.add_argument('--predictions')

    score_parser = subparsers.add_parser('score', help='score one prediction file')
    score_parser.add_argument('--dataset', required=True)
    score_parser.add_argument('--predictions', required=True)
    score_parser.add_argument('--output')
    score_parser.add_argument('--retrieval-k', type=int, default=10)

    compare = subparsers.add_parser('compare', help='compare baseline and candidate')
    compare.add_argument('--dataset', required=True)
    compare.add_argument('--baseline', required=True)
    compare.add_argument('--candidate', required=True)
    compare.add_argument('--output')
    compare.add_argument('--retrieval-k', type=int, default=10)

    promotion = subparsers.add_parser(
        'run-promotion', help='generate read-only promotion predictions'
    )
    promotion.add_argument('--dataset', required=True)
    promotion.add_argument('--output', required=True)
    promotion.add_argument('--mode', choices=('legacy', 'current'), required=True)

    pipeline = subparsers.add_parser(
        'run-current', help='run current production AI paths against saved DB fixtures'
    )
    pipeline.add_argument('--dataset', required=True)
    pipeline.add_argument('--output', required=True)
    pipeline.add_argument('--manifest')
    pipeline.add_argument(
        '--profile',
        choices=('llm-only', 'dense-only', 'hybrid', 'current'),
        default='current',
        help='retrieval/Q&A pipeline profile; other tasks keep production behavior',
    )
    pipeline.add_argument(
        '--task', action='append',
        choices=('classification', 'drafting', 'promotion', 'retrieval', 'qna', 'cards'),
        help='task to run; repeat the option to select multiple tasks',
    )

    model_pilot = subparsers.add_parser(
        'model-pilot', help='run the fixed low-cost model-selection pilot'
    )
    model_pilot.add_argument(
        '--dataset', default='benchmarks/datasets/model_selection_pilot.json'
    )
    model_pilot.add_argument('--output', required=True)

    ablation = subparsers.add_parser(
        'ablation', help='score and compare prediction files from pipeline profiles'
    )
    ablation.add_argument('--dataset', required=True)
    ablation.add_argument(
        '--profile', action='append', required=True,
        metavar='NAME=PREDICTIONS_JSONL',
        help='repeat for at least two profiles',
    )
    ablation.add_argument('--output', required=True)
    ablation.add_argument('--retrieval-k', type=int, default=5)

    synthetic_rag = subparsers.add_parser(
        'synthetic-rag',
        help='run a rollback-only RAG ablation using non-sensitive synthetic rules',
    )
    synthetic_rag.add_argument('--output-dir', required=True)
    synthetic_rag.add_argument('--repeats', type=int, default=3)

    rescore_rag = subparsers.add_parser(
        'rescore-synthetic-rag',
        help='re-adjudicate related citations and rescore a saved synthetic run',
    )
    rescore_rag.add_argument('--output-dir', required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == 'validate':
        dataset = _dataset(args.dataset)
        predictions = _predictions(args.predictions) if args.predictions else []
        result = {'datasetCases': len(dataset), 'predictionCases': len(predictions)}
    elif args.command == 'score':
        result = score(
            _dataset(args.dataset),
            _predictions(args.predictions),
            retrieval_k=args.retrieval_k,
        )
    elif args.command == 'compare':
        dataset = _dataset(args.dataset)
        baseline = score(
            dataset, _predictions(args.baseline), retrieval_k=args.retrieval_k
        )
        candidate = score(
            dataset, _predictions(args.candidate), retrieval_k=args.retrieval_k
        )
        result = compare_reports(baseline, candidate)
    elif args.command == 'run-promotion':
        dataset = _dataset(args.dataset)
        rows = (
            legacy_predictions(dataset)
            if args.mode == 'legacy'
            else current_predictions(dataset)
        )
        write_jsonl(args.output, rows)
        result = {'mode': args.mode, 'predictionCases': len(rows), 'output': args.output}
    elif args.command == 'run-current':
        from .pipeline_runner import current_predictions as run_pipeline
        from .pipeline_runner import environment_manifest

        dataset = _dataset(args.dataset)
        rows = run_pipeline(dataset, tasks=args.task, profile=args.profile)
        write_jsonl(args.output, rows)
        if args.manifest:
            write_json(
                args.manifest,
                environment_manifest(args.dataset, profile=args.profile),
            )
        result = {
            'tasks': args.task or 'all',
            'profile': args.profile,
            'predictionCases': len(rows),
            'failures': sum(bool(row.get('error')) for row in rows),
            'output': args.output,
            'manifest': args.manifest,
        }
    elif args.command == 'model-pilot':
        import os
        import django

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from .model_selection import run

        report = run(args.dataset, args.output)
        result = {
            'benchmark': report['benchmark'],
            'output': args.output,
            'profiles': {
                name: value['usage'] for name, value in report['profiles'].items()
            },
        }
    elif args.command == 'ablation':
        from .ablation import build_ablation_report

        result = build_ablation_report(
            _dataset(args.dataset),
            _profile_predictions(args.profile),
            retrieval_k=args.retrieval_k,
        )
        write_json(args.output, result)
    elif args.command == 'synthetic-rag':
        import os
        import django

        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from .synthetic_rag import run

        result = run(args.output_dir, repeats=args.repeats)
    elif args.command == 'rescore-synthetic-rag':
        from .synthetic_rag import rescore

        result = rescore(args.output_dir)
    else:  # pragma: no cover
        raise AssertionError(args.command)

    output = getattr(args, 'output', None)
    if output and args.command in {'score', 'compare'}:
        write_json(output, result)
    _print(result)
    return 0
