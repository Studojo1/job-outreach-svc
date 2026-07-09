"""Deterministic rails: these behaviors shipped bugs when they regressed,
so they are pinned here (each case names its incident)."""

from datetime import datetime, timezone

from services.bob import textrails as tr


def test_norm_company_drops_parentheticals():
    # 'Composio (Ashby job board)' duplicated the Composio row in run 10.
    assert tr.norm_company("Composio (Ashby job board)") == tr.norm_company("Composio")


def test_norm_url_collapses_variants():
    a = "https://www.linkedin.com/posts/abc_x-activity-123-yz/?utm=share"
    b = "https://in.linkedin.com/posts/abc_x-activity-123-yz"
    assert tr.norm_url(a) == tr.norm_url(b)


def test_contact_collision_flags_second_company():
    owners = {tr.norm_person("Kashvi Choudhary"): (tr.norm_company("Jydigitek"), "Jydigitek")}
    clean = {"contact_name": "Kashvi Choudhary"}
    assert tr.contact_collision(clean, tr.norm_company("Honeywell"), owners) == "Jydigitek"
    assert tr.contact_collision(clean, tr.norm_company("Jydigitek"), owners) == ""


def test_sanitize_strips_garbage_urls_and_em_dashes():
    cells = {
        "evidence_url": "https://lnkd.in/abc123",              # shortener never ships
        "website": "https://www.linkedin.com/company/foo",     # social is not a website
        "note": "stipend 20–30k",                              # en dash
    }
    clean, removed = tr.sanitize_cells(cells)
    assert clean["evidence_url"] == ""
    assert clean["website"] == ""
    assert clean["note"] == "stipend 20-30k"
    assert len(removed) == 2


def test_sanitize_rejects_company_page_as_evidence():
    clean, removed = tr.sanitize_cells({"evidence_url": "https://linkedin.com/company/sherlock"})
    assert clean["evidence_url"] == ""


def test_spam_orgs_blocklist():
    assert tr.SPAM_ORGS.search("NayePankh Foundation")
    assert tr.SPAM_ORGS.search("Basti Ki Pathshala")
    assert not tr.SPAM_ORGS.search("Aftershoot")


def test_post_author_handles():
    assert tr.post_author("https://x.com/jagriti_3005/status/123") == "x:jagriti_3005"
    assert tr.post_author("https://www.linkedin.com/posts/afreen-naz-85b05517a_hiring-x-123") == "li:afreen-naz-85b05517a"
    assert tr.post_author("https://unstop.com/internships/foo") == ""


NOW = datetime(2026, 7, 9, tzinfo=timezone.utc)


def test_parse_age_days_common_forms():
    assert tr.parse_age_days("14h") < 1
    assert tr.parse_age_days("5d") == 5
    assert tr.parse_age_days("3 days ago") == 3
    assert tr.parse_age_days("2 weeks ago") == 14
    assert tr.parse_age_days("1 month ago") == 30
    assert tr.parse_age_days("just now") == 0
    assert tr.parse_age_days("yesterday") == 1
    assert tr.parse_age_days("2026-07-02", now=NOW) == 7
    assert tr.parse_age_days("Jul 02", now=NOW) == 7


def test_parse_age_days_unknown_is_none_not_fresh():
    # The client shipped 2-month-old rows as open; unknown age must never
    # silently pass as fresh.
    assert tr.parse_age_days("") is None
    assert tr.parse_age_days("apply by 2026-07-17") is None
