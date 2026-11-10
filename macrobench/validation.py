"""Validity checks for a completed macrobenchmark run."""


def summarize_run_validity(completed_work, workers, steps_per_worker, timed_out,
                           worker_exceptions=0):
    """Keep successful steps separate from successful SQL operations."""
    completed_steps = sum(work.get("steps", 0) for work in completed_work.values())
    operation_errors = sum(
        sum(work.get("operation_errors", {}).values())
        for work in completed_work.values()
    )
    reasons = []
    if timed_out:
        reasons.append("runtime_cap")
    if worker_exceptions:
        reasons.append("worker_exceptions")
    if len(completed_work) != workers:
        reasons.append("missing_workers")
    if completed_steps != workers * steps_per_worker:
        reasons.append("incomplete_steps")
    if operation_errors:
        reasons.append("operation_errors")
    if any(work.get("status") != "completed" for work in completed_work.values()):
        reasons.append("worker_status")
    return {
        "valid": not reasons,
        "invalid_reasons": reasons,
        "total_operation_errors": operation_errors,
    }
