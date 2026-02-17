import unittest

from workers.vinted_scraper import (
    _to_minor_gbp,
    build_search_url,
    define_criteria_query,
)


class TestVintedScraperHelpers(unittest.TestCase):
    def test_define_criteria_query(self):
        query = define_criteria_query(
            order=["most recent first"],
            catalog=["men"],
            condition=["very good condition", "good condition"],
        )
        self.assertIn("order=newest_first", query)
        self.assertIn("catalog[]=5", query)
        self.assertIn("status[]=2", query)
        self.assertIn("status[]=3", query)

    def test_build_search_url(self):
        url = build_search_url("patagonia r1", "order=newest_first", page=2)
        self.assertEqual(
            url,
            "https://www.vinted.co.uk/catalog?search_text=patagonia%20r1&page=2&order=newest_first",
        )

    def test_price_parsing(self):
        self.assertEqual(_to_minor_gbp("£80"), 8000)
        self.assertEqual(_to_minor_gbp("£79.99"), 7999)
        self.assertEqual(_to_minor_gbp("80"), 8000)
        self.assertIsNone(_to_minor_gbp(""))


if __name__ == "__main__":
    unittest.main()
