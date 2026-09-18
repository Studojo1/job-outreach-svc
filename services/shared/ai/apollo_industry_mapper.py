import logging
from typing import List

from core.logger import get_logger

logger = get_logger(__name__)

# Strict mapping from Human -> Apollo Taxonomy
APOLLO_INDUSTRY_MAP = {
    "saas": ["Computer Software", "Internet"],
    "e-commerce": ["Retail"],
    "ecommerce": ["Retail"],
    "digital / creative agencies": ["Marketing & Advertising"],
    "digital agencies": ["Marketing & Advertising"],
    "creative agencies": ["Marketing & Advertising"],
    "marketing": ["Marketing & Advertising"],
    "media & publishing": ["Media Production", "Online Media", "Publishing"],
    "media": ["Media Production", "Online Media"],
    "publishing": ["Publishing"],
    "edtech": ["E-Learning"],
    "fintech": ["Financial Services"],
    "gaming": ["Computer Games"],
    "consumer apps": ["Internet", "Computer Software"],
    "software": ["Computer Software", "Internet"],
    "tech": ["Computer Software", "Information Technology & Services"],
    # Identity mappings (already in Apollo format)
    "computer software": ["Computer Software"],
    "internet": ["Internet"],
    "retail": ["Retail"],
    "marketing & advertising": ["Marketing & Advertising"],
    "financial services": ["Financial Services"],
    "e-learning": ["E-Learning"],
    "computer games": ["Computer Games"]
}

def validate_and_map_industries(raw_industries: List[str]) -> List[str]:
    """
    Validates and translates human-readable industries into Apollo's taxonomy.
    1. Removes unknown industries.
    2. Translates known industries.
    3. Flattens the list.
    4. Deduplicates.
    """
    if not raw_industries:
        return []
        
    mapped_industries = []
    dropped_industries = []
    
    for industry in raw_industries:
        clean_key = industry.lower().strip()
        
        # Check mapping
        if clean_key in APOLLO_INDUSTRY_MAP:
            translated = APOLLO_INDUSTRY_MAP[clean_key]
            mapped_industries.extend(translated)
            logger.info(f"[INDUSTRY_MAP] Translated '{industry}' -> {translated}")
        else:
            dropped_industries.append(industry)
            logger.warning(f"[INDUSTRY_MAP] Dropped unknown industry: '{industry}'")
            
    # Flatten & Deduplicate
    unique_industries = list(set(mapped_industries))
    
    if unique_industries:
        logger.info(f"[INDUSTRY_MAP] Final verified industry list: {unique_industries}")
        
    return unique_industries
