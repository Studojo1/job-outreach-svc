"""Service for retrieving decision maker titles based on candidate roles."""

from typing import List

from sqlalchemy.orm import Session

from database.models import decision_maker_model import DecisionMaker
from core.logger import get_logger

logger = get_logger(__name__)


def get_titles_for_roles(roles: List[str], db: Session) -> List[str]:
    """Retrieve unique decision maker titles for the provided roles.

    Args:
        roles: List of candidate inferred roles.
        db: SQLAlchemy database session.

    Returns:
        List of distinct target titles.  Returns empty list if no matches.
    """
    if not roles:
        logger.info("No roles provided, returning empty titles list")
        return []

    try:
        # Query all matching decision makers
        records = db.query(DecisionMaker).filter(DecisionMaker.role.in_(roles)).all()

        # Extract titles and remove duplicates while preserving order
        seen = set()
        titles = []
        for record in records:
            if record.title not in seen:
                seen.add(record.title)
                titles.append(record.title)

        logger.info("Found %d distinct titles for roles %s: %s", len(titles), roles, titles)
        return titles

    except Exception as e:
        logger.error("Error querying decision makers for roles %s: %s", roles, e)
        return []
