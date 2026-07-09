"""Pipeline orchestrator: owns budgets, stage sequencing, loop-until-N and
termination. The LLM never decides to stop; it is only consulted inside
stages (extract/score) and for tie-breaks (contact).

Phase 1 ships the framework (budget, stage runner, report); stages are wired
in as they land. run_pipeline() becomes callable end-to-end in the assembly
phase.
"""

import logging
import time
from dataclasses import dataclass, field

from services.bob.pipeline import state

logger = logging.getLogger(__name__)

DEFAULT_CREDIT_CAP = 30          # Context.dev credits per pipeline run
DEFAULT_WALL_CAP_S = 15 * 60     # hard wall-clock stop
MAX_RINGS = 2                    # harvest widening rounds (ring 2 = synonyms + more sources)


class PipelineError(Exception):
    pass


@dataclass
class Budget:
    """Hard resource limits owned by the orchestrator, not the model."""
    credit_cap: int = DEFAULT_CREDIT_CAP
    wall_cap_s: int = DEFAULT_WALL_CAP_S
    credits_used: int = 0
    started: float = field(default_factory=time.monotonic)

    def spend(self, credits: int) -> None:
        self.credits_used += int(credits or 0)

    @property
    def credits_left(self) -> int:
        return max(0, self.credit_cap - self.credits_used)

    @property
    def wall_left_s(self) -> float:
        return max(0.0, self.wall_cap_s - (time.monotonic() - self.started))

    def exhausted(self) -> str:
        """'' while budget remains, else the reason string for the report."""
        if self.credits_left <= 0:
            return f"credit cap reached ({self.credit_cap})"
        if self.wall_left_s <= 0:
            return f"wall clock cap reached ({self.wall_cap_s}s)"
        return ""


def run_stage(db, run_id: int, name: str, fn, *args, **kwargs) -> dict:
    """Execute one stage with timing + event instrumentation. Stages return a
    small summary dict; failures surface as events and re-raise (the caller
    decides whether the pipeline can continue without this stage)."""
    t0 = time.monotonic()
    state.push_event(db, run_id, "stage", f"{name}: started")
    try:
        summary = fn(*args, **kwargs) or {}
    except Exception as e:
        db.rollback()
        state.push_event(db, run_id, "stage", f"{name}: FAILED", str(e)[:300])
        logger.exception("[BOB/PIPE] stage %s failed (run %s)", name, run_id)
        raise
    dt = time.monotonic() - t0
    label = f"{name}: done in {dt:.0f}s"
    detail = ", ".join(f"{k}={v}" for k, v in summary.items() if not str(k).startswith("_"))
    state.push_event(db, run_id, "stage", label, detail[:380],
                     credits=int(summary.get("credits", 0)))
    return summary


def format_report(funnel: dict, params: dict, stopped_because: str) -> str:
    """Human-readable funnel for the conductor's summary. Every number comes
    from the DB snapshot, never from model claims."""
    raw = funnel.get("raw_items", {})
    opp = funnel.get("opportunities", {})
    lines = [
        f"PIPELINE REPORT (target {params.get('count', '?')} rows, "
        f"freshness {params.get('freshness_days', 7)}d, location {params.get('location', '?')})",
        f"harvested items: {sum(raw.values())} (extracted from: {raw.get('extracted', 0)}, "
        f"skipped non-hiring: {raw.get('skipped', 0)}, unprocessed: {raw.get('harvested', 0)})",
        f"opportunities: extracted {sum(opp.values())} | verified+ {sum(v for k, v in opp.items() if k in ('verified', 'scored', 'contacted', 'written'))} "
        f"| written {opp.get('written', 0)} | rejected {opp.get('rejected', 0)}",
    ]
    vetoes = funnel.get("vetoes", [])
    if vetoes:
        by_stage: dict = {}
        for v in vetoes:
            by_stage.setdefault(v["stage"], []).append(f"{v['reason']} x{v['count']}")
        for stg, rs in by_stage.items():
            lines.append(f"rejected at {stg}: " + "; ".join(rs[:6]))
    cs = funnel.get("contact_sources", {})
    if cs:
        lines.append("contacts by source: " + ", ".join(f"{k}: {v}" for k, v in sorted(cs.items())))
    lines.append(f"stopped because: {stopped_because}")
    return "\n".join(lines)


def run_pipeline(db, run_id: int, chat_id: int, params: dict) -> dict:
    """End-to-end staged run. Wired incrementally; callable once the assembly
    stage lands (until then it raises so nothing half-runs in production)."""
    raise PipelineError("pipeline stages are not fully wired yet (assembly phase pending)")
