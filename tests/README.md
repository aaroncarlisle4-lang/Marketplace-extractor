# Tests

## Python helper tests
Run without external dependencies:

```bash
python -m unittest tests/test_vinted_scraper.py -v
```

## Pytest-style tests
If `pytest` is installed:

```bash
pytest -q tests/test_vinted_scraper_pytest.py
```

## Convex logic validation
Convex screening/notification logic can be validated by running:
- `ingest.js:upsertListings`
- `jobs.js:runNotificationPass`
- `queries.js:getRecentMatches`
- `queries.js:getRunStats`

This repo uses those calls as the end-to-end proof-of-concept path.
