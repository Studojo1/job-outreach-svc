from services.bob.pipeline.assemble import build_cells


def _opp(**kw):
    base = {"company": "Sherlock", "role": "AI Engineer Intern", "location": "Remote",
            "stipend": "₹50,000 per month", "posted": "5d", "source": "ctx_li_posts",
            "website": "https://thesherlock.ai", "evidence_url": "https://www.linkedin.com/posts/m_x-1",
            "evidence_quote": "We're hiring 2 AI Engineer Interns", "what_they_do": "AI infrastructure",
            "fit_score": 85, "contact_name": "Priya", "contact_title": "named application contact",
            "contact_tier": "T1", "contact_source": "t0_apply_channel",
            "contact_email": "priya@sherlock.sh", "contact_profile_url": "",
            "author_affiliation": "unknown"}
    base.update(kw)
    return base


def test_cells_carry_contact_email_and_tier():
    cells = build_cells(_opp(), candidate="Soham (AI/ML/DS)")
    assert cells["contact_email"] == "priya@sherlock.sh"
    assert cells["tier"] == "T1"
    assert cells["source"] == "LinkedIn post"
    assert cells["candidate"] == "Soham (AI/ML/DS)"
    assert cells["fit_score"] == "85"


def test_aggregator_provenance_labeled():
    cells = build_cells(_opp(author_affiliation="aggregator"))
    assert "via aggregator post" in cells["source"]


def test_cells_are_sanitized():
    cells = build_cells(_opp(website="https://linkedin.com/company/sherlock",
                             stipend="20–30k"))
    assert cells["website"] == ""          # social page is not a company website
    assert cells["stipend"] == "20-30k"    # em/en dashes never ship


def test_unstated_stipend_is_explicit():
    assert build_cells(_opp(stipend=""))["stipend"] == "not stated"
