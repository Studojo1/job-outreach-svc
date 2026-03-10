"""Filter Calibration Engine — Relevance-Preserving Adaptive Optimizer.

DESIGN PRINCIPLES:
    1. SEED from the candidate's initial filters — never discard them.
    2. Titles are tightened LAST — they carry the most relevance.
    3. When titles must be tightened, remove SENIOR tiers first (VP/Director),
       keeping the mid-level roles relevant to the candidate.
    4. Floor constraint: final results must be ≥ target. Max 1.8× target.
    5. Candidate's location and industry preferences are always respected
       as the starting point — tightening narrows within them, not away.

TIGHTENING PRIORITY (least relevant dimension first):
    1. Company size → narrow
    2. Geography → narrow within candidate preferences
    3. Industry → specialize within candidate preferences
    4. Titles → LAST RESORT, remove senior tiers first

LOOSENING PRIORITY:
    1. Titles → add back tiers
    2. Company size → widen
    3. Geography → broaden
    4. Industry → remove filter
"""

from typing import Dict, Any, List, Tuple, Optional, NamedTuple

from job_outreach_tool.services.shared.schemas.filter_schema import LeadFilter
from job_outreach_tool.services.shared.schemas.target_segment_schema import TargetSegment
from job_outreach_tool.services.lead_discovery.apollo_query_builder import build_apollo_query
from job_outreach_tool.services.lead_discovery.apollo_service import search_people_count
from job_outreach_tool.core.logger import get_logger

logger = get_logger(__name__)

MAX_ITERATIONS = 10
APOLLO_MAX_TITLES = 100


# ═══════════════════════════════════════════════════════════════════════════════
# COMPANY SIZE TIERS — for tightening/loosening within candidate sizes
# ═══════════════════════════════════════════════════════════════════════════════

ALL_SIZE_OPTIONS = [
    "1,50", "51,200", "201,500", "501,1000", "1001,5000", "5001,10000",
    "201,1000", "1001,10000",
]

# ═══════════════════════════════════════════════════════════════════════════════
# LOCATION NARROWING — within India
# ═══════════════════════════════════════════════════════════════════════════════

INDIA_CITY_LADDER = [
    ["India"],
    ["Bangalore", "Delhi", "Mumbai", "Hyderabad", "Pune", "Chennai"],
    ["Bangalore", "Delhi", "Mumbai", "Hyderabad", "Pune"],
    ["Bangalore", "Delhi", "Mumbai", "Hyderabad"],
    ["Bangalore", "Delhi", "Mumbai"],
    ["Bangalore", "Delhi"],
    ["Bangalore"],
]


# ═══════════════════════════════════════════════════════════════════════════════
# INDUSTRY SPECIALIZATION — expand or narrow within saas-adjacent
# ═══════════════════════════════════════════════════════════════════════════════

INDUSTRY_SPECIALIZATIONS = {
    "saas": ["saas", "computer software", "internet", "information technology"],
    "fintech": ["fintech", "financial services", "banking"],
    "default": None,
}


# ═══════════════════════════════════════════════════════════════════════════════
# CALIBRATION STATE — Seeded from initial filters
# ═══════════════════════════════════════════════════════════════════════════════

class CalibrationState:
    """Tracks calibration position. Always seeded from the candidate's filters."""

    def __init__(self, initial_filters: LeadFilter):
        # ── Seed from initial filter data ────────────────────────────────
        # Extract all unique titles from segments, preserving order
        seen = set()
        all_titles: List[str] = []
        for seg in initial_filters.target_segments:
            for t in seg.person_titles:
                if t not in seen:
                    seen.add(t)
                    all_titles.append(t)

        # Split candidate titles into seniority tiers
        self.title_tiers = self._classify_titles(all_titles)
        self.active_tiers = {k: True for k in self.title_tiers}

        # Sizes from segments
        self.all_sizes = list(dict.fromkeys(
            seg.company_size_range for seg in initial_filters.target_segments
        ))
        self.active_sizes = list(self.all_sizes)

        # Locations — candidate's preference
        self.candidate_locations = list(initial_filters.person_locations)
        self.active_locations = list(self.candidate_locations)
        self._location_ladder_idx = 0  # index into narrowing ladder

        # Industries — candidate's preference
        self.candidate_industries = list(initial_filters.organization_industries or [])
        self.active_industries = list(self.candidate_industries) if self.candidate_industries else None

        # Exclusions
        self.exclude_titles = list(initial_filters.person_titles_exclude or [])

    def _classify_titles(self, titles: List[str]) -> Dict[str, List[str]]:
        """Split titles into senior / mid / core tiers based on keywords.

        - senior: VP, Director, Head, Chief (tightened FIRST — removed first)
        - mid: Principal, Group, Lead, Senior (tightened SECOND)
        - core: everything else (Product Manager, etc.) — NEVER removed
        """
        senior_keywords = ["vp", "director", "head", "chief", "vice president"]
        mid_keywords = ["principal", "group", "lead", "senior"]

        tiers: Dict[str, List[str]] = {"senior": [], "mid": [], "core": []}

        for title in titles:
            lower = title.lower()
            if any(kw in lower for kw in senior_keywords):
                tiers["senior"].append(title)
            elif any(kw in lower for kw in mid_keywords):
                tiers["mid"].append(title)
            else:
                tiers["core"].append(title)

        # Ensure core always has at least the primary titles
        if not tiers["core"] and tiers["mid"]:
            tiers["core"] = tiers["mid"][:2]
            tiers["mid"] = tiers["mid"][2:]

        return tiers

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def titles(self) -> List[str]:
        result = []
        for tier_name in ["core", "mid", "senior"]:
            if self.active_tiers.get(tier_name, False):
                result.extend(self.title_tiers.get(tier_name, []))
        return result[:APOLLO_MAX_TITLES]

    def to_filters(self) -> LeadFilter:
        titles = self.titles
        segments = [
            TargetSegment(company_size_range=s, person_titles=list(titles))
            for s in self.active_sizes
        ]
        return LeadFilter(
            target_segments=segments,
            person_titles_exclude=self.exclude_titles,
            person_locations=self.active_locations,
            organization_locations=self.active_locations,
            organization_industries=self.active_industries,
            email_status=["verified"],
        )

    def snapshot(self) -> Dict[str, Any]:
        titles = self.titles
        return {
            "titles_count": len(titles),
            "segments_count": len(self.active_sizes),
            "company_size_count": len(self.active_sizes),
            "location_count": len(self.active_locations),
            "industry_count": len(self.active_industries) if self.active_industries else 0,
            "titles": list(titles),
            "company_sizes": list(self.active_sizes),
            "locations": list(self.active_locations),
            "industries": list(self.active_industries) if self.active_industries else [],
            "tier_status": {k: v for k, v in self.active_tiers.items()},
        }

    def clone(self) -> "CalibrationState":
        """Deep copy for revert support."""
        from job_outreach_tool.services.shared.schemas.filter_schema import LeadFilter
        c = CalibrationState.__new__(CalibrationState)
        c.title_tiers = {k: list(v) for k, v in self.title_tiers.items()}
        c.active_tiers = dict(self.active_tiers)
        c.all_sizes = list(self.all_sizes)
        c.active_sizes = list(self.active_sizes)
        c.candidate_locations = list(self.candidate_locations)
        c.active_locations = list(self.active_locations)
        c._location_ladder_idx = self._location_ladder_idx
        c.candidate_industries = list(self.candidate_industries)
        c.active_industries = list(self.active_industries) if self.active_industries else None
        c.exclude_titles = list(self.exclude_titles)
        return c


# ═══════════════════════════════════════════════════════════════════════════════
# ACTIONS — Each returns (action_label, estimated_impact)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_tighten_actions(state: CalibrationState) -> List[Tuple[str, float, str]]:
    """Return (name, estimated_reduction_fraction, internal_id) tuples.

    PRIORITY ORDER: size → location → industry → titles (senior first).
    """
    actions = []

    # 1. Company size — remove smallest or largest segments
    if len(state.active_sizes) > 1:
        actions.append(("tighten_size_remove_smallest", 0.25, "size_small"))
    if len(state.active_sizes) > 2:
        actions.append(("tighten_size_remove_largest", 0.20, "size_large"))

    # 2. Geography — narrow within currently active locations
    if "India" in state.active_locations:
        actions.append(("narrow_india_to_cities", 0.30, "loc_india"))
    elif len(state.active_locations) > 2:
        actions.append(("remove_lowest_density_city", 0.18, "loc_remove"))

    # 3. Industry — specialize
    if state.active_industries is None:
        actions.append(("add_industry_filter", 0.45, "ind_add"))
    elif len(state.active_industries) > 1:
        actions.append(("narrow_industry", 0.25, "ind_narrow"))

    # 4. Titles — remove SENIOR tier first, then mid. Core is NEVER removed.
    if state.active_tiers.get("senior") and len(state.title_tiers.get("senior", [])) > 0:
        senior_frac = len(state.title_tiers["senior"]) / max(len(state.titles), 1)
        actions.append(("remove_senior_titles", senior_frac * 0.8, "title_senior"))
    if state.active_tiers.get("mid") and len(state.title_tiers.get("mid", [])) > 0:
        mid_frac = len(state.title_tiers["mid"]) / max(len(state.titles), 1)
        actions.append(("remove_mid_titles", mid_frac * 0.7, "title_mid"))

    return actions


def _get_loosen_actions(state: CalibrationState) -> List[Tuple[str, float, str]]:
    """Return (name, estimated_expansion_factor, internal_id) tuples.

    PRIORITY ORDER: titles → size → location → industry.
    """
    actions = []

    # 1. Titles — re-add tiers
    if not state.active_tiers.get("mid"):
        actions.append(("add_mid_titles", 1.4, "title_mid_add"))
    if not state.active_tiers.get("senior"):
        actions.append(("add_senior_titles", 1.2, "title_senior_add"))

    # 2. Company size — add back segments
    if len(state.active_sizes) < len(state.all_sizes):
        actions.append(("expand_company_size", 1.5, "size_expand"))

    # 3. Geography — broaden
    if state.active_locations != state.candidate_locations:
        actions.append(("broaden_location", 1.6, "loc_broaden"))

    # 4. Industry — remove filter
    if state.active_industries is not None:
        actions.append(("remove_industry_filter", 2.0, "ind_remove"))

    return actions


def _apply_tighten(state: CalibrationState, action_id: str) -> None:
    """Mutate state for a tightening action."""
    if action_id == "size_small":
        state.active_sizes = state.active_sizes[1:]
    elif action_id == "size_large":
        state.active_sizes = state.active_sizes[:-1]
    elif action_id == "loc_india":
        # Replace "India" with top tech cities
        state.active_locations = ["Bangalore", "Delhi", "Mumbai", "Hyderabad", "Pune"]
    elif action_id == "loc_remove":
        state.active_locations = state.active_locations[:-1]
    elif action_id == "ind_add":
        # Add industry filter matching candidate's preferences
        if state.candidate_industries:
            key = state.candidate_industries[0].lower()
            state.active_industries = INDUSTRY_SPECIALIZATIONS.get(
                key, [key]
            )
        else:
            state.active_industries = ["software", "internet"]
    elif action_id == "ind_narrow":
        if state.active_industries and len(state.active_industries) > 1:
            state.active_industries = state.active_industries[:len(state.active_industries) // 2 + 1]
    elif action_id == "title_senior":
        state.active_tiers["senior"] = False
    elif action_id == "title_mid":
        state.active_tiers["mid"] = False


def _apply_loosen(state: CalibrationState, action_id: str) -> None:
    """Mutate state for a loosening action."""
    if action_id == "title_mid_add":
        state.active_tiers["mid"] = True
    elif action_id == "title_senior_add":
        state.active_tiers["senior"] = True
    elif action_id == "size_expand":
        # Re-add one size from the original set
        for s in state.all_sizes:
            if s not in state.active_sizes:
                state.active_sizes.append(s)
                break
    elif action_id == "loc_broaden":
        state.active_locations = list(state.candidate_locations)
    elif action_id == "ind_remove":
        state.active_industries = None


# ═══════════════════════════════════════════════════════════════════════════════
# SCORING — Pick the action whose impact best matches the needed adjustment
# ═══════════════════════════════════════════════════════════════════════════════

def _pick_best_tighten(
    actions: List[Tuple[str, float, str]],
    needed_reduction: float,
    allow_multi: bool = False,
) -> List[Tuple[str, float, str]]:
    """Select action(s) whose estimated reduction best matches what's needed.

    For extreme overshoot (needed > 0.85), stacks multiple actions.
    """
    if not actions:
        return []

    sorted_actions = sorted(actions, key=lambda a: a[1], reverse=True)

    if allow_multi and needed_reduction > 0.85:
        selected = []
        remaining = needed_reduction
        for act in sorted_actions:
            if remaining <= 0.10:
                break
            selected.append(act)
            remaining -= act[1] * (1 - (needed_reduction - remaining) / 2)
        return selected
    else:
        # Single best: closest match to needed reduction
        best = min(sorted_actions, key=lambda a: abs(a[1] - needed_reduction))
        return [best]


def _pick_best_loosen(
    actions: List[Tuple[str, float, str]],
) -> List[Tuple[str, float, str]]:
    """Pick the loosening action with the largest expected expansion."""
    if not actions:
        return []
    return [max(actions, key=lambda a: a[1])]


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN CALIBRATION LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def calibrate_filters(
    filters: LeadFilter,
    target_leads: int,
) -> Tuple[LeadFilter, int, List[Dict[str, Any]]]:
    """Run relevance-preserving adaptive calibration.

    Seeds from the candidate's initial filters. Tightens non-title
    dimensions first, tightens senior titles before mid/core titles.
    Never drops below target.

    Args:
        filters: Initial LeadFilter from the candidate's filter generator.
        target_leads: Desired number of leads.

    Returns:
        Tuple of (final_filters, total_entries, iteration_logs).
    """
    # Floor constraint: never go below target. Ceiling: 1.8× target.
    lower_bound = target_leads
    upper_bound = int(target_leads * 1.8)

    logger.info(
        "Calibration starting: target=%d, range=[%d, %d]",
        target_leads, lower_bound, upper_bound,
    )

    state = CalibrationState(filters)
    iteration_logs: List[Dict[str, Any]] = []
    prev_count = -1
    prev_state: Optional[CalibrationState] = None
    total_entries = 0
    swing_count = 0  # Track consecutive swings to detect deadlocks
    
    # Track the best known valid state in case we exhaust iterations
    best_state: Optional[CalibrationState] = None
    best_entries: int = 0
    # Track states that produced good overshoots (for deadlock breaking)
    best_over_state: Optional[CalibrationState] = None
    best_over_entries: int = 0
    
    def _is_better_result(current: int, best: int) -> bool:
        if best == 0 and current > 0: return True
        if lower_bound <= current <= upper_bound: return True
        # We prefer an overshoot to an undershoot, because the user can 
        # still pull target_leads from an overshoot.
        if current >= lower_bound and best < lower_bound: return True
        if current > upper_bound and best > upper_bound:
            return current < best # closer to upper is better
        if current < lower_bound and best < lower_bound:
            return current > best # closer to lower is better
        return False

    for i in range(1, MAX_ITERATIONS + 1):
        current_filters = state.to_filters()
        query = build_apollo_query(current_filters, page=1)

        try:
            total_entries = search_people_count(query)
        except Exception as e:
            logger.error("Apollo API error at iteration %d: %s", i, e)
            total_entries = 0

        snap = state.snapshot()
        log_entry: Dict[str, Any] = {
            "iteration": i,
            "total_entries": total_entries,
            "action": "",
            **snap,
        }

        print(f"\n[CALIBRATION ITERATION {i}]")
        print(f"  titles: {snap['titles_count']} ({snap['tier_status']})")
        print(f"  locations: {snap['locations']}")
        print(f"  company_sizes: {snap['company_sizes']}")
        print(f"  industries: {snap['industries']}")
        print(f"  results: {total_entries}")
        print(f"  target range: [{lower_bound}, {upper_bound}]")

        # Update best known state
        if _is_better_result(total_entries, best_entries):
            best_state = state.clone()
            best_entries = total_entries
        
        # Track best overshoot for deadlock breaking
        if total_entries > upper_bound:
            if best_over_entries == 0 or total_entries < best_over_entries:
                best_over_state = state.clone()
                best_over_entries = total_entries

        # ── In range → ACCEPT ────────────────────────────────────────────
        if lower_bound <= total_entries <= upper_bound:
            log_entry["action"] = "ACCEPTED"
            iteration_logs.append(log_entry)
            print(f"[CALIBRATION COMPLETE] {total_entries} in [{lower_bound}, {upper_bound}]")
            return current_filters, total_entries, iteration_logs

        # ── Swing detection ──────────────────────────────────────────────
        if prev_count >= 0 and prev_state is not None:
            was_over = prev_count > upper_bound
            was_under = prev_count < lower_bound
            now_over = total_entries > upper_bound
            now_under = total_entries < lower_bound

            if (was_over and now_under) or (was_under and now_over):
                swing_count += 1
                logger.warning("Swing #%d detected: %d → %d", swing_count, prev_count, total_entries)
                print(f"  ⚠️ SWING #{swing_count}: {prev_count} → {total_entries}")

                # ── DEADLOCK BREAK: after 2+ swings, abandon revert strategy ─
                if swing_count >= 2:
                    print(f"  🔧 DEADLOCK DETECTED — breaking cycle with aggressive tightening")
                    logger.warning("Deadlock after %d swings. Breaking cycle.", swing_count)
                    
                    # Use the overshoot state and tighten aggressively
                    # DO NOT use industry filter (it causes 0 results)
                    if best_over_state is not None:
                        state = best_over_state.clone()
                    elif prev_count > total_entries:
                        state = prev_state.clone()
                    # else keep current state if it's the overshoot
                    
                    # Apply ALL non-industry tightenings at once
                    actions = _get_tighten_actions(state)
                    # Filter out industry actions (they cause 0)
                    safe_actions = [a for a in actions if a[2] not in ("ind_add", "ind_narrow")]
                    
                    names = []
                    for name, impact, aid in safe_actions:
                        _apply_tighten(state, aid)
                        names.append(name)
                    
                    if names:
                        log_entry["action"] = "DEADLOCK_BREAK: " + " + ".join(names)
                    else:
                        log_entry["action"] = "DEADLOCK_BREAK (no safe actions)"
                    
                    # Reset swing tracking and update prev_state
                    swing_count = 0
                    prev_state = state.clone()
                    prev_count = total_entries
                    iteration_logs.append(log_entry)
                    continue

                # ── First swing: try revert + soft adjust ────────────────
                state = prev_state.clone()
                if now_under:
                    actions = _get_loosen_actions(state)
                    if actions:
                        softest = min(actions, key=lambda a: a[1])
                        _apply_loosen(state, softest[2])
                        log_entry["action"] = f"REVERT+SOFT_LOOSEN → {softest[0]}"
                    else:
                        log_entry["action"] = "REVERT (No loosen available)"
                else:
                    actions = _get_tighten_actions(state)
                    if actions:
                        softest = min(actions, key=lambda a: a[1])
                        _apply_tighten(state, softest[2])
                        log_entry["action"] = f"REVERT+SOFT_TIGHTEN → {softest[0]}"
                    else:
                        log_entry["action"] = "REVERT (No tighten available)"
                iteration_logs.append(log_entry)
                prev_count = total_entries
                continue
            else:
                # No swing this iteration → reset counter
                swing_count = 0

        prev_state = state.clone()

        # ── Too MANY → TIGHTEN ───────────────────────────────────────────
        if total_entries > upper_bound:
            needed = 1.0 - (upper_bound / max(total_entries, 1))
            actions = _get_tighten_actions(state)

            if not actions:
                log_entry["action"] = "TIGHTEN_EXHAUSTED"
                iteration_logs.append(log_entry)
                break

            allow_multi = needed > 0.85
            selected = _pick_best_tighten(actions, needed, allow_multi)
            names = []
            for name, impact, aid in selected:
                _apply_tighten(state, aid)
                names.append(name)
            log_entry["action"] = " + ".join(names)
            print(f"  → TIGHTEN: {log_entry['action']} (needed reduction: {needed:.0%})")

        # ── Too FEW → LOOSEN ─────────────────────────────────────────────
        elif total_entries < lower_bound:
            actions = _get_loosen_actions(state)

            if not actions:
                log_entry["action"] = "LOOSEN_EXHAUSTED"
                iteration_logs.append(log_entry)
                break

            selected = _pick_best_loosen(actions)
            names = []
            for name, impact, aid in selected:
                _apply_loosen(state, aid)
                names.append(name)
            log_entry["action"] = " + ".join(names)
            print(f"  → LOOSEN: {log_entry['action']}")

        iteration_logs.append(log_entry)
        prev_count = total_entries

    # Exhausted iterations — return the absolute best state we found.
    # This guarantees we don't accidentally return 34 results just because Iteration 6
    # happened to apply a strict filter before cut-off.
    if best_state is None:
        best_state = state
        best_entries = total_entries

    final_filters = best_state.to_filters()
    
    print(f"[CALIBRATION EXHAUSTED] Best results found: {best_entries} after {len(iteration_logs)} iterations")
    logger.info("Calibration exhausted: %d iterations. Best results: %d", len(iteration_logs), best_entries)
    
    return final_filters, best_entries, iteration_logs
