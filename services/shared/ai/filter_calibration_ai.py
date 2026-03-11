import json
import time
from typing import Dict, Any, Tuple, List

from services.apollo_service import search_people_count
from services.debug.debug_trace_service import DebugTrace
from services.ai.apollo_industry_mapper import validate_and_map_industries
from core.logger import get_logger

logger = get_logger(__name__)

MAX_AI_ITERATIONS = 6

def _classify_titles_by_seniority(titles: List[str]) -> Dict[str, List[str]]:
    """Classify titles into seniority levels."""
    classified = {
        "C_LEVEL": [],
        "VP_LEVEL": [],
        "DIRECTOR_LEVEL": [],
        "HEAD_LEVEL": [],
        "MANAGER_LEVEL": []
    }
    for title in titles:
        t_lower = title.lower()
        if "chief" in t_lower or (t_lower.startswith("c") and t_lower.endswith("o") and len(t_lower) <= 4) or "founder" in t_lower:
            classified["C_LEVEL"].append(title)
        elif "vp" in t_lower or "vice president" in t_lower:
            classified["VP_LEVEL"].append(title)
        elif "director" in t_lower:
            classified["DIRECTOR_LEVEL"].append(title)
        elif "head" in t_lower:
            classified["HEAD_LEVEL"].append(title)
        else:
            classified["MANAGER_LEVEL"].append(title)
    return classified


def _classify_titles_by_cluster(titles: List[str]) -> Dict[str, List[str]]:
    """Group titles by department functional cluster."""
    clusters = {
        "marketing_cluster": [],
        "growth_cluster": [],
        "content_cluster": [],
        "product_cluster": [],
        "design_cluster": [],
        "seo_cluster": [],
        "sales_cluster": [],
        "data_cluster": [],
        "engineering_cluster": [],
        "other_cluster": []
    }
    for title in titles:
        t_lower = title.lower()
        if "market" in t_lower:
            clusters["marketing_cluster"].append(title)
        elif "growth" in t_lower:
            clusters["growth_cluster"].append(title)
        elif "content" in t_lower or "copy" in t_lower:
            clusters["content_cluster"].append(title)
        elif "product" in t_lower:
            clusters["product_cluster"].append(title)
        elif "design" in t_lower or "creative" in t_lower:
            clusters["design_cluster"].append(title)
        elif "seo" in t_lower:
            clusters["seo_cluster"].append(title)
        elif "sale" in t_lower or "account" in t_lower or "revenue" in t_lower:
            clusters["sales_cluster"].append(title)
        elif "data" in t_lower or "analytic" in t_lower:
            clusters["data_cluster"].append(title)
        elif "engineer" in t_lower or "develop" in t_lower or "tech" in t_lower:
            clusters["engineering_cluster"].append(title)
        else:
            clusters["other_cluster"].append(title)
    return clusters

METRO_CITIES = ["Bangalore", "Delhi", "Mumbai", "Hyderabad", "Pune"]

class CalibrationEngine:
    def __init__(self, target_leads: int, role_intelligence: Dict[str, Any] = None, debug_trace: DebugTrace = None):
        self.target_leads = target_leads
        self.lower_bound = 2500
        self.upper_bound = 5000
        self.debug_trace = debug_trace
        self.role_intelligence = role_intelligence or {}
        
        self.iteration_logs = []
        self.iteration_count = 0
        
        # State trackers
        self.location_level = 0  # 0: City, 1: Metros, 2: Country
        self.mode = "none" # "expansion" or "tightening"
        
        # Failsafe / History
        self.query_history = [] # Stack of (filters, count)
        self.best_payload = None
        self.best_count = -1
        self.best_distance = float('inf')
        self.blacklisted_mutations = set()

    def log_iteration(self, iter_num: int, filters_before: dict, prev_count: int, filters_after: dict, count: int, mutation: str, reason: str):
        log_entry = {
            "iteration_number": iter_num,
            "apollo_payload": filters_after.copy(), 
            "previous_count": prev_count,
            "new_count": count,
            "mutation_applied": mutation,
            "reason": reason
        }
        if self.debug_trace:
            self.debug_trace.add_calibration_iteration(log_entry)
        self.iteration_logs.append(log_entry)

    def run_count_query(self, filters: dict) -> int:
        test_payload = filters.copy()
        
        # SAFETY RULE: Ensure titles and locations are never empty
        if not test_payload.get("person_titles"):
            logger.error("[CALIBRATION] SAFETY TRIGGERED: Empty person_titles prevented.")
            return 0
        if not test_payload.get("person_locations"):
            logger.error("[CALIBRATION] SAFETY TRIGGERED: Empty person_locations prevented.")
            return 0
            
        logger.info("[CALIBRATION] Query payload: %s", test_payload)
        
        try:
            count = search_people_count(test_payload)
            # Update best query tracker
            mid_point = (self.lower_bound + self.upper_bound) / 2
            distance = abs(count - mid_point)
            if (self.best_payload is None or distance < self.best_distance) and count > 0:
                self.best_payload = test_payload.copy()
                self.best_count = count
                self.best_distance = distance
            return count
        except Exception as e:
            logger.error("[CALIBRATION] Apollo test query failed: %s", e)
            return 0

    def run(self, initial_filters: dict) -> Tuple[Dict[str, Any], int, List[Dict[str, Any]]]:
        logger.info("[CALIBRATION] Starting. Range: 2500-5000")
        
        # Apply industry mapping to initial filters if present
        current_filters = initial_filters.copy()
        if "organization_industries" in current_filters:
            mapped_init_ind = validate_and_map_industries(current_filters["organization_industries"])
            if mapped_init_ind:
                current_filters["organization_industries"] = mapped_init_ind
            else:
                current_filters.pop("organization_industries", None)

        count = self.run_count_query(current_filters)
        self.query_history.append((current_filters.copy(), count))
        self.log_iteration(0, initial_filters, -1, current_filters, count, "None", "Initial Query")

        for i in range(1, MAX_AI_ITERATIONS + 1):
            if self.lower_bound <= count <= self.upper_bound:
                logger.info("[CALIBRATION] Success at Iteration %d. Count: %d", i-1, count)
                return current_filters, count, self.iteration_logs

            filters_before = current_filters.copy()
            prev_count = count
            last_mutation = self.iteration_logs[-1]["mutation_applied"] if self.iteration_logs else "None"
            mutation = "None"
            reason = ""

            # --- FAILSAFE: REVERT AND BLACKLIST ON ZERO ---
            if count == 0 and len(self.query_history) > 1:
                logger.warning("[CALIBRATION] Zero results detected. Reverting to last valid state.")
                self.query_history.pop() # Remove current zero state
                
                # Blacklist the mutation that caused this zero state
                if last_mutation != "None":
                    self.blacklisted_mutations.add(last_mutation)
                    logger.warning(f"[CALIBRATION] Blacklisted mutation: '{last_mutation}'")
                
                current_filters, count = self.query_history[-1]
                self.log_iteration(i, filters_before, 0, current_filters, count, "Revert", f"Results became zero. Reverted to last valid query and blacklisted '{last_mutation}'.")
                continue

            if count < self.lower_bound:
                self.mode = "expansion"
                # EXPANSION RULES
                if self.location_level < 2 and "Expand Location" not in self.blacklisted_mutations:
                    self.location_level += 1
                    mutation = "Expand Location"
                    if self.location_level == 1:
                        current_filters["person_locations"] = METRO_CITIES
                        reason = f"Results {count} < {self.lower_bound}. Expanding to Top 5 Metros."
                    else:
                        current_filters["person_locations"] = ["India"]
                        reason = f"Results {count} < {self.lower_bound}. Expanding to Country (India)."
                
                elif "organization_num_employees_ranges" in current_filters and "Expand Company Size" not in self.blacklisted_mutations:
                    mutation = "Expand Company Size"
                    current_filters.pop("organization_num_employees_ranges")
                    reason = f"Results {count} < {self.lower_bound}. Removing company size filters."
                
                else:
                    reason = "Expansion paths exhausted or blacklisted."
                    break

            elif count > self.upper_bound:
                self.mode = "tightening"
                
                # TIGHTENING RULES
                locs = current_filters.get("person_locations", [])
                
                # 1. > 5000 logic
                if count > 5000:
                    curr_titles = current_filters.get("person_titles", [])
                    filtered_titles = [t for t in curr_titles if "vp" not in t.lower() and "director" not in t.lower()]
                    if len(filtered_titles) < len(curr_titles) and "Remove VP/Director Titles" not in self.blacklisted_mutations:
                        # Extra guard against emptying titles completely
                        if filtered_titles:
                            mutation = "Remove VP/Director Titles"
                            current_filters["person_titles"] = filtered_titles
                            reason = f"Results {count} > 5000. Removing broad senior titles."
                    elif "organization_num_employees_ranges" in current_filters and current_filters["organization_num_employees_ranges"] == ["11,50", "51,200", "201,1000"] and "Restrict Company Size" not in self.blacklisted_mutations:
                        mutation = "Restrict Company Size"
                        current_filters["organization_num_employees_ranges"] = ["11,50", "51,200"]
                        reason = f"Results {count} > 5000. Restricting to small/mid sizes."
                
                # 2. Tighten logic
                if mutation == "None":
                    if "organization_num_employees_ranges" in current_filters and current_filters["organization_num_employees_ranges"] == ["11,50", "51,200", "201,1000"] and "Restrict Company Size Further" not in self.blacklisted_mutations:
                        mutation = "Restrict Company Size Further"
                        current_filters["organization_num_employees_ranges"] = ["11,50", "51,200"]
                        reason = f"Results {count} > {self.upper_bound}. Restricting size further."
                    elif "India" in locs and len(locs) == 1 and "Restrict Location" not in self.blacklisted_mutations:
                        mutation = "Restrict Location"
                        current_filters["person_locations"] = METRO_CITIES
                        self.location_level = 1
                        reason = f"Results {count} > {self.upper_bound}. Restricting India to Metros."
                    elif len(locs) == len(METRO_CITIES) and all(m in locs for m in METRO_CITIES) and "Restrict Location" not in self.blacklisted_mutations:
                        mutation = "Restrict Location"
                        candidate_city = self.role_intelligence.get("locations", ["Bangalore"])[0]
                        current_filters["person_locations"] = [candidate_city]
                        self.location_level = 0
                        reason = f"Results {count} > {self.upper_bound}. Restricting to Candidate City ({candidate_city})."
                
                # 3. Industry fallback (ONLY i >= 3)
                if mutation == "None" and i >= 3 and "Add Industry Filter" not in self.blacklisted_mutations:
                    target_industries = self.role_intelligence.get("industry_expansion", [])
                    if target_industries and "organization_industries" not in current_filters:
                        mapped_industries = validate_and_map_industries(target_industries)
                        if mapped_industries:
                            mutation = "Add Industry Filter"
                            current_filters["organization_industries"] = mapped_industries[:3]
                            reason = f"Iteration {i} >= 3 and results {count} > {self.upper_bound}. Adding mapped industry filters."
                
                if mutation == "None":
                    # Only break if we are past the iteration where industry could be added,
                    # or if no industry expansion is possible.
                    if i >= 3:
                        reason = "Tightening paths exhausted or blacklisted."
                        break
                    else:
                        reason = "Waiting for Iteration 3 for industry filters..."
                        pass



            # Execute Mutation
            count = self.run_count_query(current_filters)
            self.query_history.append((current_filters.copy(), count))
            self.log_iteration(i, filters_before, prev_count, current_filters, count, mutation, reason)

        return current_filters, count, self.iteration_logs

def calibrate_filters_ai(
    filters: Dict[str, Any],
    target_leads: int,
    role_intelligence: Dict[str, Any] = None,
    debug_trace: DebugTrace = None,
) -> Tuple[Dict[str, Any], int, List[Dict[str, Any]]]:
    engine = CalibrationEngine(target_leads=target_leads, role_intelligence=role_intelligence, debug_trace=debug_trace)
    return engine.run(filters)
