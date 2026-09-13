def legacy_predictions(dataset):
    """Represent the pre-auto-promotion policy: every candidate needs review."""
    return [
        {
            'caseId': case['caseId'],
            'output': {'promotionType': 'PENDING_REVIEW'},
            'telemetry': {'latencyMs': 0},
        }
        for case in dataset
        if case['task'] == 'promotion'
    ]


def current_predictions(dataset):
    """Backward-compatible promotion-only entry point."""
    from .pipeline_runner import current_predictions as run_pipeline

    return run_pipeline(dataset, tasks={'promotion'})
