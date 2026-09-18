"""Title expansion service.

Instead of blindly prepending prefixes (creating nonsense like
"Head Engineering Lead"), this service adds REAL title variations
that people actually use on LinkedIn/Apollo.
"""

from typing import List

from core.logger import get_logger

logger = get_logger(__name__)

# Common real-world title variations to add
_TITLE_VARIATIONS = {
    "Engineering Manager": ["Software Engineering Manager", "Development Manager"],
    "Head of Engineering": ["VP Engineering", "VP of Engineering"],
    "Director of Engineering": ["Engineering Director"],
    "Tech Lead": ["Technical Lead", "Lead Engineer"],
    "Product Manager": ["Technical Product Manager"],
    "Head of Product": ["VP Product", "VP of Product"],
    "Director of Product": ["Product Director"],
    "Analytics Manager": ["Data Analytics Manager"],
    "Head of Data": ["VP Data", "VP of Data"],
    "Director of Data": ["Data Director"],
    "Marketing Manager": ["Marketing Lead"],
    "Head of Marketing": ["VP Marketing", "VP of Marketing"],
    "Director of Marketing": ["Marketing Director"],
    "Operations Manager": ["Business Operations Manager"],
    "Head of Operations": ["VP Operations"],
    "Director of Operations": ["Operations Director"],
    "Head of Design": ["VP Design"],
    "Director of Design": ["Design Director"],
    "Head of Security": ["VP Security"],
}


def expand_titles(title_list: List[str]) -> List[str]:
    """Expand a list of base titles by adding real-world variations.

    Instead of prepending arbitrary prefixes, adds known real-world
    alternative forms of titles that exist on LinkedIn/Apollo.

    Args:
        title_list: The base titles to expand.

    Returns:
        A deduplicated, expanded list of titles.
    """
    seen = set()
    expanded: List[str] = []

    def _add(title: str) -> None:
        if title not in seen:
            seen.add(title)
            expanded.append(title)

    for title in title_list:
        _add(title)
        # Add known variations
        for variation in _TITLE_VARIATIONS.get(title, []):
            _add(variation)

    logger.info(
        "Expanded %d base titles to %d total titles",
        len(title_list), len(expanded),
    )
    return expanded