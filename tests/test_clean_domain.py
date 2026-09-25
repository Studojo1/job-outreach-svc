"""company_domain comes from LLM research and sometimes arrives wrapped in
prose or markdown. Only a bare hostname may be stored."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.lead_discovery.domain_utils import clean_domain as _clean_domain


@pytest.mark.parametrize("raw,want", [
    ("neurofin.ai. ([neurofin.ai](https://neurofin.ai/))", "neurofin.ai"),
    ("https://www.CashBook.in/", "cashbook.in"),
    ("unmannd.com", "unmannd.com"),
    ("app.example.co.uk/path?x=1", "app.example.co.uk"),
    ("Visit acme.io for more", "acme.io"),
    ("no domain here", None),
    ("", None),
    (None, None),
])
def test_clean_domain(raw, want):
    assert _clean_domain(raw) == want
