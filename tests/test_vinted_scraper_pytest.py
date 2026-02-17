from workers.vinted_scraper import _to_minor_gbp, build_search_url, define_criteria_query


def test_define_criteria_query():
    query = define_criteria_query(
        order=["most recent first"],
        catalog=["men"],
        condition=["very good condition", "good condition"],
    )
    assert "order=newest_first" in query
    assert "catalog[]=5" in query
    assert "status[]=2" in query
    assert "status[]=3" in query


def test_build_search_url():
    assert (
        build_search_url("patagonia r1", "order=newest_first", page=1)
        == "https://www.vinted.co.uk/catalog?search_text=patagonia%20r1&page=1&order=newest_first"
    )


def test_to_minor_gbp():
    assert _to_minor_gbp("£80") == 8000
    assert _to_minor_gbp("£79.99") == 7999
    assert _to_minor_gbp("") is None
