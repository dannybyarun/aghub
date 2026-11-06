# Security policy

## Supported versions

aghub is a single 0.x line. Only the latest release on `main` is supported.
Security fixes land on `main` and in the next release; there are no backports to
older 0.x releases. If you are running a fork or a pinned commit, reproduce the
issue against current `main` if you can.

## Reporting a vulnerability

Use GitHub private security advisories: open the repository's **Security** tab
and choose **Report a vulnerability**. That channel is private between you and
the maintainers.

Do not open a public issue for a suspected vulnerability, and do not include
exploit details in a public issue, pull request, or discussion. If private
advisories are unavailable for some reason, open a minimal public issue asking
for a private channel, with no technical details.

A useful report includes:

- The affected version, commit, or release tag.
- The deployment shape: git-only board, hub with SQLite, hub with Postgres,
  ACLs enabled or not, sandbox backend, and whether the GitHub bridge is in use.
- A reproduction: commands, a minimal repository or board state, and the
  observed result.
- The impact you believe it has, and any known preconditions.

## In scope

- **Hub authentication and authorization.** Token parsing and comparison, role
  and project-scope enforcement, `reader` / `writer` / `admin` separation,
  non-loopback binding without a token, admin-only endpoints, per-tenant audit
  export scoping, and quota accounting that lets one tenant consume another
  tenant's budget.
- **Lease fencing.** Accepting a stale fencing token, a lease takeover that
  should have been rejected, releasing or renewing another agent's lease, or
  bypassing the board writer lease in a way that breaks its stated guarantee
  that a stale snapshot cannot be published.
- **Board write ACLs.** A `reader` mutating the board through a CLI or API path,
  role checks that can be skipped, the last-admin rule, or key revocation that
  does not actually invalidate old and future signatures.
- **Signature verification.** A forged, modified, or replayed record accepted as
  valid; a hash-chain break that verifies; a record verified against the wrong
  key; or a way to substitute a published key without detection.
- **Sandbox escapes.** Escaping the Bubblewrap sandbox used by evals and merge
  checks. The `auto` fallback and `--sandbox off` are documented as weaker and
  are covered under out-of-scope below.
- **GitHub bridge and webhook handling.** `X-Hub-Signature-256` verification
  bypass, replay handling that records a duplicate or wrong review, review
  recording from a fork or untrusted association when it should be refused,
  App private-key or installation-token leakage, commit binding that lets an
  approval move to a different head, and payload-size or content-type handling
  that bypasses the webhook limits.

## Out of scope

These are documented limits, not vulnerabilities. See "Known limits" in the
README for the full text.

- **Signatures bind records to published keys, not to people.** A board writer
  who can force-push can replace an agent's published key. Signatures make
  tampering visible; they do not prove a human identity. A record that verifies
  against the key the board publishes is working as designed.
- **Board ACLs are guardrails, not a git firewall.** Anyone who can force-push
  `aghub/board` can rewrite it. Event hashes and signatures expose the rewrite
  after the fact; they do not prevent it. Hub tokens protect the HTTP API, not
  git. Compromise of the git remote or of an account with push access to trunk
  is outside the threat model.
- **The merge-check sandbox is not a security boundary.** `submit` and
  `queue run` execute `project.check` through the sandbox runner, but
  `--check-sandbox off`, `--sandbox off`, and the non-Bubblewrap fallback
  intentionally run code with host access. Treat a check command as code you
  chose to run, not as untrusted input isolated by aghub.
- **The Ed25519 and RSA helpers are pure Python and not constant-time.** They
  are intended for a trusted local repository and a trusted local process.
  Timing side channels against a co-tenant on the same host, or against an
  attacker who can measure signing operations, are out of scope. Use a vetted
  crypto library if you need constant-time behavior.
- **Advisory claims without a hub.** Without a hub, claims stop most races, not
  all. A lost race that the merge queue catches is the documented design.
- **Misconfigured failover.** A failover set that points at independent
  databases, or several hub processes writing one SQLite file without a single
  writer, is a deployment error. Failover provides availability, not consensus.
- **Volumetric denial of service.** Floods that exceed the documented rate
  limits and request-size caps are a capacity problem. Quotas are coarse fixed
  windows by design; a burst at a window boundary is known.
- **Generated code as a security control.** Codegen covers an OpenAPI subset and
  emits a starting point. Validation gaps documented in the README are not
  vulnerabilities in aghub itself.

## Response expectations

This is a small project without a dedicated security team. Reports are handled
on a best-effort basis:

- We aim to acknowledge a private report within a few days.
- We will confirm whether the report is in scope, ask for missing reproduction
  details, and tell you when a fix is likely to land. There is no guaranteed
  fix deadline.
- Please give us a reasonable window to release a fix before public disclosure.
  We will coordinate the disclosure date with you in the advisory.
- Credit is given in the advisory if you want it. There is no bug bounty.

If a report turns out to be a documented limit, we will say so and point at the
README rather than treat it as a vulnerability.
