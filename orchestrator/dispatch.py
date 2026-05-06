"""
Phase 5 of OrchestratorRefactor — backward-compatibility shim.

The decision-list cascade that used to live here was replaced by the
canonical decision tree in orchestrator/cycle/persona.py (Option B —
adopted tools.determine_next_action's behavior as the single source of
truth across both legacy poller and deployed orchestrator).

This module remains as a thin shim so `from orchestrator.dispatch import
determine_persona` keeps working for any external caller. New code should
import from orchestrator.cycle.persona directly.

Behavioral migration notes are in cycle/persona.py's module docstring.
"""

from orchestrator.cycle.persona import determine_persona

__all__ = ["determine_persona"]
