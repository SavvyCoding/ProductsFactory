# Session lifecycle subpackage.
#
# Phase 1 of OrchestratorRefactor split docker_runner.py:
#   state_machine.py — _apply_session_entry, _PROGRESS_RANK, _ALLOWED_BACKWARD
#   result_io.py     — _read/_delete/_live_poll session_result.json
#   reconciler.py    — _reconcile_session_result, _rollback_stuck_features
#
# All symbols are re-exported from orchestrator.docker_runner for backward
# compatibility with deploy/orchestrator/orchestrate.py and tests/test_docker.py.
