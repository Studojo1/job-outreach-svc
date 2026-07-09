"""Scoring: pre-gate word boundaries, floor in code, no silent passes."""

from services.bob.pipeline.score import apply_scores, gate_tokens, off_function

TOKENS = gate_tokens(["ai intern", "machine learning intern", "data science"])


def test_gate_tokens_drop_generics_keep_phrases():
    assert "ai intern" in TOKENS and "ai" in TOKENS and "data science" in TOKENS
    assert "intern" not in TOKENS  # generic would pass everything


def test_off_function_word_boundary():
    # 'ai' must not match inside 'email' (the rail's founding bug)
    assert off_function({"role": "Email Marketing Intern", "evidence_quote": "", "what_they_do": ""}, TOKENS)
    assert not off_function({"role": "AI Engineer Intern", "evidence_quote": "", "what_they_do": ""}, TOKENS)
    # hardware/robotics padding: no mandate token -> gated
    assert off_function({"role": "Robotics Hardware Intern", "evidence_quote": "solder PCBs",
                         "what_they_do": "STEM kits"}, TOKENS)


def test_off_function_open_when_no_text_or_no_tokens():
    assert not off_function({"role": "", "evidence_quote": "", "what_they_do": ""}, TOKENS)
    assert not off_function({"role": "Anything"}, [])


OPPS = [{"id": 1}, {"id": 2}, {"id": 3}]


def test_apply_scores_floor_and_clamp():
    out = apply_scores(OPPS, [{"id": 1, "fit": 88, "reason": "exact"},
                              {"id": 2, "fit": 40, "reason": "weak"},
                              {"id": 3, "fit": 150, "reason": "hype"}], 55)
    assert out[0] == (1, "scored", 88, "exact")
    assert out[1][1] == "rejected" and "below floor" in out[1][3]
    assert out[2][2] == 100  # clamped


def test_missing_verdict_is_explicit_rejection_not_silent_pass():
    out = apply_scores(OPPS, [{"id": 1, "fit": 70, "reason": "ok"}], 55)
    fates = {oid: action for oid, action, _, _ in out}
    assert fates[2] == "rejected" and fates[3] == "rejected"


def test_garbage_llm_output_rejects_everything_loudly():
    out = apply_scores(OPPS, "not json at all", 55)
    assert all(action == "rejected" for _, action, _, _ in out)
