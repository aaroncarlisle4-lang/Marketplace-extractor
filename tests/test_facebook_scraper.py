import unittest

from workers.facebook_scraper import build_search_url, miles_to_radius_km, parse_listing_age_minutes


class TestFacebookScraperHelpers(unittest.TestCase):
    def test_radius_conversion(self):
        self.assertEqual(miles_to_radius_km(40), 64)

    def test_build_search_url(self):
        url = build_search_url("captain's chair", location_slug="belfast", radius_miles=40)
        self.assertIn("facebook.com/marketplace/belfast/search/", url)
        self.assertIn("query=captain%27s%20chair", url)
        self.assertIn("radiusKM=64", url)

    def test_parse_listing_age_minutes(self):
        self.assertEqual(parse_listing_age_minutes("Listed 5 minutes ago"), 5)
        self.assertEqual(parse_listing_age_minutes("Listed 2 hours ago"), 120)
        self.assertEqual(parse_listing_age_minutes("Listed yesterday"), 24 * 60)


if __name__ == "__main__":
    unittest.main()
