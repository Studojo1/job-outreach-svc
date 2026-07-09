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


def _default_stages() -> dict:
    from services.bob.pipeline import assemble, contact, extract, harvest, score, verify
    return {"harvest": harvest.run, "extract": extract.run, "verify": verify.run,
            "score": score.run, "contact": contact.run, "assemble": assemble.run}


def run_pipeline(db, run_id: int, chat_id: int, params: dict, stages: dict | None = None) -> dict:
    """End-to-end staged run. The orchestrator, never the model, decides when
    to widen (ring 2 adds phrasings + boards) and when to stop:
      target met | rings exhausted | budget exhausted.
    Chat-scoped stage queries make this resumable: pending work left by an
    interrupted run is picked up before any new harvest spends a credit."""
    if not params.get("table_id"):
        raise PipelineError("run_pipeline needs table_id (create the table first)")
    if not params.get("keywords"):
        raise PipelineError("run_pipeline needs mandate keywords")
    count = max(1, int(params.get("count") or 10))
    stages = stages or _default_stages()
    budget = Budget(credit_cap=int(params.get("credit_cap") or DEFAULT_CREDIT_CAP))
    state.save_params(db, run_id, {"pipeline": {k: v for k, v in params.items()}})

    def _stage(name, fn, *a):
        """A stage failure is logged and swallowed: downstream stages still
        process whatever reached the DB, and failed work keeps its prior
        status so the next run resumes it. One flaky stage never zeros a run."""
        try:
            return run_stage(db, run_id, name, fn, *a)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            stage_errors.append(f"{name}: {e.__class__.__name__}")
            return {}

    written_total = 0
    stopped = ""
    stage_errors: list[str] = []
    for ring in range(MAX_RINGS + 1):
        reason = budget.exhausted()
        if reason:
            stopped = reason
            break
        _stage(f"harvest(ring {ring})", stages["harvest"], db, run_id, chat_id, params, budget, ring)
        _stage("extract", stages["extract"], db, run_id, chat_id, params)
        _stage("verify", stages["verify"], db, run_id, chat_id, params)
        _stage("score", stages["score"], db, run_id, chat_id, params)
        _stage("contact", stages["contact"], db, run_id, chat_id, params, budget)
        out = _stage("assemble", stages["assemble"], db, run_id, chat_id, params, count - written_total)
        written_total += int(out.get("written") or 0)
        if written_total >= count:
            stopped = f"target met ({written_total}/{count})"
            break
    if not stopped:
        stopped = (budget.exhausted() or
                   f"sources exhausted after {MAX_RINGS + 1} harvest rings "
                   f"({written_total}/{count} delivered)")
    if stage_errors:
        stopped += " | stage errors: " + "; ".join(dict.fromkeys(stage_errors))

    funnel = state.funnel_snapshot(db, run_id)
    state.save_params(db, run_id, {"funnel": funnel, "stopped_because": stopped,
                                   "credits_used": budget.credits_used})
    report = format_report(funnel, params, stopped)
    state.push_event(db, run_id, "stage", f"pipeline finished: {written_total} rows", stopped)
    return {"written": written_total, "report": report, "funnel": funnel,
            "credits_used": budget.credits_used, "stopped_because": stopped}
