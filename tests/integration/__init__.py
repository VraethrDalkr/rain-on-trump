"""
tests/integration/
~~~~~~~~~~~~~~~~~~
Integration tests that call real external APIs.

These tests are SKIPPED by default. To run them:

    INTEGRATION_TESTS=1 pytest tests/integration/ -v

Rate Limit Considerations:
- OpenSky: 100 req/day anonymous - avoid in CI
- Nominatim: 1 req/sec - rate limited in code
- Others: Be respectful with request frequency
"""
