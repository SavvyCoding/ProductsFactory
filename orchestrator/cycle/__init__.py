# Cycle subpackage — the orchestrator's main loop and its supporting pieces.
#
# Phase 4 of OrchestratorRefactor:
#   loop_detector.py — _LoopDetector (3-in-a-row + 4-element alternating)
#                      and _heal_loop (root-cause diagnostic)
#   selection.py     — round-robin selection, run_now priority, reviewer
#                      preempt, retro preempt, quiet hours, daily caps
#   locks.py         — _acquire_db_lock, _release_db_lock, _heartbeat_loop
#                      (INVARIANTS I.1–I.4)
#   runner.py        — main() — the cycle controller (Phase 5 target)
