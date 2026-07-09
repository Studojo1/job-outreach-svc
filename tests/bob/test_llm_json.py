import pytest

from services.bob.pipeline.llm import extract_json


def test_extract_json_plain_array():
    assert extract_json('[{"a": 1}]') == [{"a": 1}]


def test_extract_json_fenced_with_prose():
    out = "Here you go:\n```json\n{\"roles\": []}\n```\nDone."
    assert extract_json(out) == {"roles": []}


def test_extract_json_prose_prefix_and_trailer():
    out = 'Sure. [{"id": 3, "fit": 72}] hope this helps'
    assert extract_json(out) == [{"id": 3, "fit": 72}]


def test_extract_json_raises_on_no_json():
    with pytest.raises(ValueError):
        extract_json("I could not find anything.")
