"""Verification verdicts. Each case pins a shipped bug:
stale rows (client feedback), spam orgs, ledger bans, Kashvi-style dupes."""

from services.bob.pipeline.verify import verdict, _role_key
from services.bob.textrails import norm_company


def _opp(company="Acme", role="AI Intern", posted="3d", evidence="https://www.linkedin.com/posts/x_y-1"):
    return {"company": company, "company_norm": norm_company(company),
            "role": role, "posted": posted, "evidence_url": evidence}


LIVE = lambda u: ("open", "apply button live")
DEAD = lambda u: ("closed", "no longer accepting applications")


def _run(opp, **kw):
    args = dict(freshness_days=7, rejected={}, table_norms={}, seen_keys=set(), liveness=LIVE)
    args.update(kw)
    return verdict(opp, **args)


def test_fresh_post_passes():
    assert _run(_opp()) == ("verified", "")


def test_stale_posting_rejected_with_window_in_reason():
    status, reason = _run(_opp(posted="2 months ago"))
    assert status == "rejected" and "stale" in reason and "7d" in reason


def test_user_window_overrides_default():
    assert _run(_opp(posted="20 days ago"), freshness_days=30)[0] == "verified"


def test_unknown_age_passes_only_to_liveness_not_as_fresh():
    # guest/unstop items carry no age text; they are live by construction.
    assert _run(_opp(posted=""))[0] == "verified"


def test_spam_org_rejected():
    assert _run(_opp(company="NayePankh Foundation"))[0] == "rejected"


def test_ledger_ban_rejected():
    cn = norm_company("Quest Global")
    status, reason = _run(_opp(company="Quest Global"), rejected={cn: "Quest Global"})
    assert status == "rejected" and "user removed" in reason


def test_table_dedupe_rejected():
    cn = norm_company("Sherlock")
    status, reason = _run(_opp(company="Sherlock"), table_norms={cn: 135})
    assert status == "rejected" and "already in table" in reason


def test_pool_dedupe_same_company_role_but_fanout_roles_survive():
    seen = set()
    assert _run(_opp(role="AI Intern"), seen_keys=seen)[0] == "verified"
    # same company+role from another query: dup
    assert _run(_opp(role="AI Internship"), seen_keys=seen)[0] == "rejected"
    # same company, DIFFERENT role (multi-role post fan-out): survives
    assert _run(_opp(role="Rust Intern"), seen_keys=seen)[0] == "verified"


def test_dead_linkedin_job_rejected_posts_not_checked():
    dead_job = _opp(evidence="https://www.linkedin.com/jobs/view/4434714422", posted="")
    status, reason = _run(dead_job, liveness=DEAD)
    assert status == "rejected" and "dead evidence" in reason
    # a POST url has no deterministic check; liveness fn must not be consulted
    boom = lambda u: (_ for _ in ()).throw(AssertionError("checked a post URL"))
    assert _run(_opp(posted="2d"), liveness=boom)[0] == "verified"


def test_role_key_folds_internship_variants():
    assert _role_key("AI Internship") == _role_key("AI Intern")
