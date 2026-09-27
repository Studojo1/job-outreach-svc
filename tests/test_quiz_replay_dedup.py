"""Dropping a retried answer without eating a legitimately repeated one.

The quiz stream endpoint rebuilds the answers dict by replaying the client's
chat history and assigning answers BY POSITION. One extra user message
therefore shifts every later answer onto the wrong question key, silently: the
student's city is stored as their company stage, the quiz completes, and the
leads are built from a profile nobody typed.

A failed-and-retried turn used to leave exactly that duplicate behind, and the
stream fetch now retries automatically, so the endpoint has to be able to
recognise one.

The trap is that "answered the same thing twice" is not itself the bug. "Skip"
to one question and "Skip" to the next is an ordinary quiz, and those two
answers look identical once the history is filtered down to user messages. What
separates them is the assistant's question in between. These tests pin that
distinction, because getting it wrong destroys real answers rather than
duplicated ones.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from services.candidate_intelligence.payload_builder import user_answers_from_history

# These tests call the production function. They used to call a copy of the
# loop "kept identical to the code under test on purpose", which meant a change
# to the endpoint could never fail them (quiz audit re-check, 27 Sep).
_replay = user_answers_from_history
_replay_hardened = user_answers_from_history


def _a(text):
    return {"role": "assistant", "content": text}


def _u(text):
    return {"role": "user", "content": text}


def test_retried_answer_is_collapsed():
    """The bug: one turn failed, was retried, and left two copies behind."""
    history = [
        _a("Q1"), _u("Student"),
        _a("Q2"), _u("Internship"), _u("Internship"),  # retry, no question between
        _a("Q3"), _u("Bangalore"),
    ]
    assert _replay(history) == ["Student", "Internship", "Bangalore"]


def test_same_answer_to_two_questions_is_kept():
    """The trap: skipping two questions in a row is an ordinary quiz."""
    history = [
        _a("Q1"), _u("Skip"),
        _a("Q2"), _u("Skip"),
        _a("Q3"), _u("Skip"),
    ]
    assert _replay(history) == ["Skip", "Skip", "Skip"]


def test_alternating_repeats_are_all_kept():
    history = [
        _a("Q1"), _u("Yes"),
        _a("Q2"), _u("Yes"),
        _a("Q3"), _u("No"),
        _a("Q4"), _u("Yes"),
    ]
    assert _replay(history) == ["Yes", "Yes", "No", "Yes"]


def test_sentinels_never_count_as_answers():
    """__resume__ is sent as the message on a restore and must not be replayed."""
    history = [
        _u("__start__"),
        _a("Q1"), _u("Student"),
        _u("__resume__"),
        _a("Q2"), _u("Internship"),
    ]
    assert _replay(history) == ["Student", "Internship"]


def test_a_sentinel_between_duplicates_does_not_rescue_them():
    """A resume in the middle of a retry is still one answer, not two."""
    history = [
        _a("Q1"), _u("Student"),
        _a("Q2"), _u("Internship"), _u("__resume__"), _u("Internship"),
    ]
    assert _replay(history) == ["Student", "Internship"]


def test_triplicate_collapses_to_one():
    history = [_a("Q1"), _u("Student"), _u("Student"), _u("Student")]
    assert _replay(history) == ["Student"]


def test_empty_history_is_empty():
    assert _replay([]) == []


# ── malformed history must not kill the turn ────────────────────────────────

def test_a_message_missing_content_does_not_raise():
    history = [_a("Q1"), {"role": "user"}, _a("Q2"), _u("Internship")]
    assert _replay_hardened(history) == ["", "Internship"]


def test_a_message_missing_role_is_not_treated_as_an_answer():
    """Safe reading: an unlabelled message cannot invent an answer."""
    history = [_a("Q1"), {"content": "Student"}, _a("Q2"), _u("Internship")]
    assert _replay_hardened(history) == ["Internship"]


def test_a_non_dict_entry_is_skipped():
    history = [_a("Q1"), "not a dict", None, 42, _u("Student")]
    assert _replay_hardened(history) == ["Student"]


def test_an_empty_dict_is_skipped():
    history = [_a("Q1"), {}, _u("Student")]
    assert _replay_hardened(history) == ["Student"]


def test_a_null_content_becomes_empty_not_a_crash():
    history = [_a("Q1"), {"role": "user", "content": None}]
    assert _replay_hardened(history) == [""]


# ── the two replays must agree ──────────────────────────────────────────────

def test_reconstruct_answers_matches_the_stream_replay():
    """payload_builder.reconstruct_answers and the stream endpoint replay the
    same history, so they must produce the same answers.

    They did not. reconstruct_answers filtered only __start__ (so a __resume__
    became a phantom answer), had no duplicate-answer dedupe (so a retried turn
    shifted every later answer by one), and read the LIVE resume_profile rather
    than the frozen _qps snapshot the quiz was served from — which could build a
    different question sequence than the student actually answered, and then
    file their answers against it.

    A divergence here is invisible: the quiz looks right, the profile is built
    from a different set of answers.
    """
    from services.candidate_intelligence.payload_builder import reconstruct_answers

    history = [
        _a("Q1"), _u("Student, not graduating soon"),
        _u("__resume__"),
        _a("Q2"), _u("Internship"), _u("Internship"),   # retried turn
        _a("Q3"), _u("Bengaluru"),
    ]

    class _Candidate:
        resume_text = "Some resume text"
        resume_profile = {"domain": "engineering", "likely_roles": ["Backend Engineer"]}
        parsed_json = {}

    got = reconstruct_answers(history, _Candidate())
    # The sentinel and the duplicate are both dropped, so three answers remain
    # and each lands on its own question key.
    assert list(got.values()) == [
        "Student, not graduating soon",
        "Internship",
        "Bengaluru",
    ], got
    assert len(got) == 3


def test_reconstruct_answers_prefers_the_frozen_snapshot():
    """The quiz freezes the profile into parsed_json["_qps"]; the replay must
    use the same one or it can build a different sequence."""
    from services.candidate_intelligence.payload_builder import reconstruct_answers

    class _Candidate:
        resume_text = "Some resume text"
        # Live column has drifted since the quiz was served.
        resume_profile = {"domain": "marketing", "likely_roles": ["Growth Marketer"]}
        parsed_json = {"_qps": {"domain": "engineering", "likely_roles": ["Backend Engineer"]}}

    got = reconstruct_answers([_a("Q1"), _u("Student, not graduating soon")], _Candidate())
    assert len(got) == 1


# ── one replay, and it knows each multi-select question's options ───────────

def test_replay_records_the_options_of_multi_select_questions():
    """The payload build needs the options the student was actually shown to
    split a multi-select answer safely. The replay is the only place that
    knows which question each answer belongs to, so it hands them over."""
    from services.candidate_intelligence.payload_builder import replay_answers

    history = [_a("Q1"), _u("Student, not graduating soon")]
    answers, options = replay_answers(
        history, {"domain": "engineering", "likely_roles": ["Backend Engineer"]}, "resume", {},
    )
    assert answers == {"career_stage": "Student, not graduating soon"}
    # career_stage is single-select, so no options are recorded for it.
    assert "career_stage" not in options


def test_stream_endpoint_uses_the_shared_replay():
    """Q35: there must be exactly one replay. The stream endpoint used to carry
    its own inline copy, which had already drifted from the payload builder's
    once. Guard against a copy creeping back in."""
    import inspect

    import api.routes_candidate as rc

    src = inspect.getsource(rc)
    assert "replay_answers(" in inspect.getsource(rc._chat_stream_turn)
    assert "raw_user_msgs" not in src
    assert "_run_generate_payload_background" not in src
