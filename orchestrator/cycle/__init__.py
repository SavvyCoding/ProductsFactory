# Cycle subpackage — per-cycle helpers shared by the orchestrator's main loop.
#
# persona.py — _decide_action, the single source of truth for "what should
#              this product's session do this cycle." Called from
#              deploy/orchestrator/tools.py:determine_next_action.
#
# (selection.py, locks.py, loop_detector.py were deleted with the legacy
# host-mode poller on 2026-05-19 — their behaviors are now in
# deploy/orchestrator/tools.py + orchestrate.py.
# The determine_persona adapter that bridged persona.py to the legacy
# poller was deleted in the same dead-code sweep — no callers remained.)
