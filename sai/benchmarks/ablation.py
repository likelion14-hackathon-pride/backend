from .scoring import compare_reports, score


PROFILE_ORDER = (
    'llm-only',
    'naive-rag',
    'structure-dense',
    'hybrid',
    'current',
)


def _ordered_profiles(predictions_by_profile):
    known = [name for name in PROFILE_ORDER if name in predictions_by_profile]
    extras = sorted(set(predictions_by_profile) - set(PROFILE_ORDER))
    return known + extras


def build_ablation_report(dataset, predictions_by_profile, *, retrieval_k=5):
    """여러 pipeline prediction을 동일 scorer로 비교한다.

    prediction 생성과 scoring을 분리해 각 profile을 별도 worktree/평가 DB에서
    실행할 수 있게 한다. 긴 문서의 chunk PK가 profile마다 달라도 document-level
    retrieval 지표는 비교 가능하다.
    """
    if len(predictions_by_profile) < 2:
        raise ValueError('at least two pipeline profiles are required')

    order = _ordered_profiles(predictions_by_profile)
    reports = {
        name: score(dataset, predictions_by_profile[name], retrieval_k=retrieval_k)
        for name in order
    }
    comparisons = {}
    for baseline, candidate in zip(order, order[1:]):
        comparison = compare_reports(reports[baseline], reports[candidate])
        comparisons[f'{baseline}->{candidate}'] = (
            comparison['deltaCandidateMinusBaseline']
        )

    for baseline in ('llm-only', 'naive-rag'):
        if baseline in reports and 'current' in reports:
            comparison = compare_reports(reports[baseline], reports['current'])
            comparisons[f'{baseline}->current'] = (
                comparison['deltaCandidateMinusBaseline']
            )

    return {
        'schemaVersion': 1,
        'measurementType': 'ablation',
        'profileOrder': order,
        'reports': reports,
        'comparisons': comparisons,
    }
