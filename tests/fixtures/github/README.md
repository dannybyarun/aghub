# GitHub App test fixtures

These keys are generated test material, not credentials. They exist only so the
suite can sign a JWT offline and verify it without `cryptography` or OpenSSL.

- `app-private-key.pem` — PKCS#8 test key
- `app-private-key.pkcs1.pem` — the same key in PKCS#1 form
- `app-public-key.pem` — the matching public key
- `pull_request_review.approved.json` — a captured `pull_request_review` shape
- `ping.json` — a GitHub App `ping` delivery

`tests/test_github_jwt.py` covers JWT signing, installation-token exchange, and
webhook signature verification against these files.
