import logging
from typing import Dict, Any, List
from sqlalchemy import or_

from database.models import contact import Contact
from services.apollo_service import search_people_chunked
from services.lead_collector_service import parse_apollo_person
from core.logger import get_logger

logger = get_logger(__name__)

def collect_leads_pool(
    filters: Dict[str, Any],
    campaign_id: int,
    target_pool_size: int,
    db
) -> List[Dict[str, Any]]:
    """Collect a pool of unstructured leads from Apollo into memory.
    
    This replaces the immediate-save loop with an in-memory collection loop.
    It still deduplicates against the database to ensure we only return
    actually new leads for the pool.

    Args:
        filters: AI-calibrated Apollo filters.
        campaign_id: The campaign these leads belong to.
        target_pool_size: Typically 4x the final target.
        db: SQLAlchemy session for deduplication checks.

    Returns:
        A list of un-saved parsed lead dictionaries ready for scoring.
    """
    logger.info("[COLLECTOR] Building lead pool. Target pool size: %d", target_pool_size)
    
    collected_pool = []
    page = 1
    
    while len(collected_pool) < target_pool_size:
        logger.info("[COLLECTOR] Fetching Page %d. Current pool size: %d/%d", page, len(collected_pool), target_pool_size)
        
        # The AI generates a dictionary perfectly matching Apollo's API schemas
        apollo_payload = filters.copy()
        apollo_payload["page"] = page
        apollo_payload["per_page"] = 100
        # Always require verified emails from Apollo
        apollo_payload["contact_email_status"] = ["verified"]
        
        try:
            api_response = search_people_chunked(apollo_payload)
            people = api_response.get("people", [])
            
            if not people:
                logger.info("[COLLECTOR] Apollo search exhausted on page %d.", page)
                break
                
            for person in people:
                if len(collected_pool) >= target_pool_size:
                    break
                    
                parsed_data = parse_apollo_person(person)
                
                if not parsed_data["name"]:
                    continue
                    
                apollo_id = parsed_data.get("apollo_person_id")
                linkedin = parsed_data.get("linkedin_url")
                
                if not apollo_id:
                    continue
                    
                # Deduplicate PER CAMPAIGN
                conditions = [Contact.apollo_person_id == apollo_id]
                if linkedin:
                    conditions.append(Contact.linkedin_url == linkedin)
                    
                existing = db.query(Contact).filter(
                    Contact.campaign_id == campaign_id,
                    or_(*conditions)
                ).first()
                
                if existing:
                    logger.debug("[COLLECTOR] Skipped duplicate lead id=%s", apollo_id)
                    continue
                
                # Add to memory pool
                collected_pool.append(parsed_data)
                
            page += 1
            
        except Exception as e:
            logger.error("[COLLECTOR] Fetch loop failed on page %d: %s", page, e, exc_info=True)
            break
            
    logger.info("[COLLECTOR] Finished building pool: %d leads", len(collected_pool))
    return collected_pool
