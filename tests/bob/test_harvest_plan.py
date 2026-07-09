from services.bob.pipeline.harvest import build_query_plan, _freshness


PARAMS = {"keywords": ["ai intern", "machine learning intern"],
          "location": "Bengaluru, India", "freshness_days": 7}


def test_plan_covers_all_default_sources():
    plan = build_query_plan(PARAMS)
    sources = {p["source"] for p in plan}
    assert {"linkedin_jobs", "unstop", "ctx_li_posts", "ctx_x"} <= sources
    # boards only join in ring 1 (they are the widening move, not the core sweep)
    assert "ctx_boards" not in sources


def test_ring1_is_a_superset_with_boards():
    core = build_query_plan(PARAMS, ring=0)
    wide = build_query_plan(PARAMS, ring=1)
    assert len(wide) > len(core)
    assert any(p["source"] == "ctx_boards" for p in wide)
    assert all(p.get("include_domains") for p in wide if p["source"] == "ctx_boards")


def test_freshness_window_follows_user_days():
    assert _freshness(1) == "last_24_hours"
    assert _freshness(7) == "last_week"
    assert _freshness(30) == "last_month"
    plan = build_query_plan({**PARAMS, "freshness_days": 1})
    assert all(p["freshness"] == "last_24_hours" for p in plan if p["kind"] == "ctx")


def test_guest_jobs_window_matches_freshness():
    plan = build_query_plan(PARAMS)
    guest = [p for p in plan if p["source"] == "linkedin_jobs"]
    assert guest and all(p["hours_back"] == 7 * 24 for p in guest)


def test_sources_filter_respected():
    plan = build_query_plan({**PARAMS, "sources": ["x"]})
    assert plan and all(p["source"] == "ctx_x" for p in plan)


def test_no_duplicate_specs():
    plan = build_query_plan({**PARAMS, "keywords": ["ai intern", "ai intern", " ai intern "]})
    keys = [(p["source"], p.get("query") or p.get("keywords")) for p in plan]
    assert len(keys) == len(set(keys))


def test_city_extracted_from_location():
    plan = build_query_plan(PARAMS)
    ctx_qs = [p["query"] for p in plan if p["kind"] == "ctx" and p["source"] == "ctx_li_posts"]
    assert any("Bengaluru" in q and "Bengaluru, India" not in q for q in ctx_qs)
