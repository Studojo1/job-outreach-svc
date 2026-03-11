import heapq
from typing import Dict, Any, List

from core.logger import get_logger

logger = get_logger(__name__)

# Scoring Weights (Max 100)
WEIGHT_TITLE = 40
WEIGHT_INDUSTRY = 20
WEIGHT_COMPANY_SIZE = 15
WEIGHT_LOCATION = 15
WEIGHT_DEPARTMENT = 10

def score_and_select_leads(
    leads: List[Dict[str, Any]],
    role_intelligence: Dict[str, Any],
    target_count: int,
) -> List[Dict[str, Any]]:
    """Score a pool of leads against hiring intelligence and pick the best N."""
    logger.info("[LEAD_SCORING] Scoring %d leads to find top %d", len(leads), target_count)
    
    if not leads:
        return []

    target_titles = [t.lower() for t in role_intelligence.get("hiring_roles", [])]
    target_industries = [i.lower() for i in role_intelligence.get("industry_expansion", [])]
    target_sizes = [s.lower() for s in role_intelligence.get("company_size_preferences", [])]
    target_departments = [d.lower() for d in role_intelligence.get("departments", [])]
    target_seniorities = [s.lower() for s in role_intelligence.get("target_seniorities", [])]
    
    scored_leads = []
    
    for lead in leads:
        score = 0
        
        # 1. Title Match (Max 40 pts)
        title = (lead.get("title") or "").lower()
        if "head of" in title or title.startswith("head"):
            score += 40
        elif "growth manager" in title:
            score += 35
        elif "marketing manager" in title:
            score += 30
        elif any(t in title for t in target_titles):
            score += 25  # Fallback for other valid titles
            
        # 2. Industry Match (Max 20 pts)
        industry = (lead.get("industry") or "").lower()
        if any(i == industry for i in target_industries):
            score += 20
        elif any(i in industry for i in target_industries):
            score += 10 # Partial match
            
        # 3. Company Size Fit (Max 15 pts)
        size = (lead.get("company_size") or "").lower()
        if any(size in s or s in size for s in target_sizes):
            score += 15
            
        # 4. Location Match (Max 15 pts)
        location = (lead.get("location") or "").lower()
        pref_city = (role_intelligence.get("locations") or ["India"])[0].lower()
        if pref_city in location:
            score += 15
        elif "india" in location:
            score += 8
            
        # 5. Department Match (Max 10 pts)
        if any(d in title for d in target_departments):
            score += 10
            
        # Append to sorting queue
        scored_leads.append((score, lead))
        
    # Sort descending by score. Since Python's default sort uses the first tuple element:
    scored_leads.sort(key=lambda x: x[0], reverse=True)
    
    # Extract the top N leads and inject the score into their JSON
    best_leads = []
    for score, lead in scored_leads[:target_count]:
        lead["score"] = score
        best_leads.append(lead)
        
    logger.info("[LEAD_SCORING] Selected top %d leads from pool. Avg Top Score: %s",
                len(best_leads), 
                sum(l["score"] for l in best_leads) / len(best_leads) if best_leads else 0)
                
    return best_leads
