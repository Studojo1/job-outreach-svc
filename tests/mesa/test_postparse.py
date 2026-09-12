"""postparse rails — the digest/unicode parsing that unlocked 19->44 rows per run."""
from services.mesa.postparse import fold_unicode, split_digest


def test_fold_unicode_bold_labels():
    styled = "\U0001D5D6\U0001D5FC\U0001D5FA\U0001D5FD\U0001D5EE\U0001D5FB\U0001D606: Zycus"
    assert fold_unicode(styled) == "Company: Zycus"


def test_fold_unicode_passthrough():
    assert fold_unicode("plain ascii ₹40,000 stays") == "plain ascii ₹40,000 stays"
    assert fold_unicode("") == ""


def test_split_digest_multi_job():
    body = ("Referral alert 1) Company - Wadhwani AI Role - Machine Learning Intern "
            "Stipend - ₹30,000 - ₹40,000/month Batch - 2025/2026/2027 Location - Remote "
            "2) Company - Playo Role - Software Engineering Intern Stipend - ₹50,000/month "
            "Location - Bengaluru Apply: https://lnkd.in/abc123")
    jobs = split_digest(body)
    assert len(jobs) == 2
    assert jobs[0]["company"] == "Wadhwani AI"
    assert jobs[0]["role"] == "Machine Learning Intern"
    assert "30,000" in jobs[0]["stipend"]
    assert jobs[1]["company"] == "Playo"
    assert jobs[1]["location"] == "Bengaluru"
    assert jobs[1]["apply_url"].startswith("https://lnkd.in/")


def test_split_digest_single_post_returns_empty():
    assert split_digest("We are hiring a Backend Intern at Acme. DM me!") == []
