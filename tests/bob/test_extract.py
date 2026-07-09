"""Extraction plumbing (pure parts). The golden case is the SDE-Jobs
aggregator: 8 companies in one post must yield 8 opportunities."""

from services.bob.pipeline.extract import author_profile_from_url, parse_batch_output

ITEM = {"id": 7, "url": "https://www.linkedin.com/posts/sde-jobs_referral-activity-123-x",
        "source": "ctx_li_posts", "title": "t", "description": "d", "markdown": "m"}
BY_ID = {7: ITEM}


def _entry(opps, why=""):
    return [{"item_id": 7, "why_skipped": why, "opportunities": opps}]


def test_aggregator_fans_out_all_roles():
    opps = [{"company": f"Co{i}", "role": "AI Intern"} for i in range(8)]
    out = parse_batch_output(_entry(opps), BY_ID)
    assert len(out) == 1 and len(out[0][1]) == 8


def test_fanout_capped():
    opps = [{"company": f"Co{i}", "role": "R"} for i in range(25)]
    out = parse_batch_output(_entry(opps), BY_ID)
    assert len(out[0][1]) == 10


def test_placeholder_company_dropped_with_reason():
    out = parse_batch_output(_entry([{"company": "Stealth startup", "role": "R"},
                                     {"company": "Sherlock", "role": "AI Intern"}]), BY_ID)
    item_id, opps, why = out[0]
    assert [o["company"] for o in opps] == ["Sherlock"]
    assert "no real company" in why


def test_evidence_url_is_code_assigned():
    out = parse_batch_output(_entry([{"company": "X", "evidence_url": "https://evil.example"}]), BY_ID)
    assert out[0][1][0]["evidence_url"] == ITEM["url"]
    assert out[0][1][0]["source"] == "ctx_li_posts"


def test_author_profile_derived_from_post_slug():
    assert author_profile_from_url(
        "https://www.linkedin.com/posts/afreen-naz-85b05517a_hiring-fde-activity-747_x"
    ) == "https://www.linkedin.com/in/afreen-naz-85b05517a"
    assert author_profile_from_url("https://x.com/jagriti_3005/status/9") == "https://x.com/jagriti_3005"
    assert author_profile_from_url("https://unstop.com/i/x") == ""


def test_bad_affiliation_normalized_and_unknown_items_ignored():
    out = parse_batch_output(
        _entry([{"company": "X", "author_affiliation": "bestie"}]) +
        [{"item_id": 999, "opportunities": [{"company": "Ghost"}]}],
        BY_ID,
    )
    assert len(out) == 1
    assert out[0][1][0]["author_affiliation"] == "unknown"
