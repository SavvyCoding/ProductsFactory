# Cycle subpackage — per-cycle helpers shared by the orchestrator's main loop.
#
# persona.py — _decide_action and the determine_persona adapter
#              (Phase 5 of OrchestratorRefactor — adopted tools.determine_next_action's
#              behavior as the single source of truth across paths).
#
# (selection.py, locks.py, loop_detector.py were deleted with the legacy
# host-mode poller on 2026-05-19 — their behaviors are now in
# deploy/orchestrator/tools.py + orchestrate.py.)
