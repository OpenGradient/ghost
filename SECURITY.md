# Security Policy

ghost is a privacy tool. Its core promise is that what you send a hosted model is readable only
inside an attested TEE enclave, never by the relay or anyone else on the way, and that a reply is
verified before a token of it is shown. A bug that breaks that promise is the most serious kind of
bug here.

## Reporting a vulnerability

Please report privately, not in a public issue:

- Open a private security advisory via the repository's **Security** tab
  (GitHub: *Security → Report a vulnerability*), or
- Contact the OpenGradient maintainers directly.

Include what you found, how to reproduce it, and the impact. We aim to acknowledge quickly.

## What we treat as P0

- **Anything you send a hosted model leaving this machine unsealed**, or the inference route being
  repointed away from the local bridge.
- **Trusting the wrong enclave**: a relay, TEE registry or key taken from anywhere but ghost itself
  and opengradient-ohttp.
- **A login leaking** -- your session or API key readable by another user, logged, or sent anywhere
  but DreamFort.
- **A response accepted without verification** (the verify-before-emit guarantee bypassed).
- **The engine phoning home** -- an update check, telemetry, a remote catalog or plugin fetch, or a
  side task that leaves the private path.

## Scope

This repo owns the local harness: the bridge and the login (`privacy/`), the install and service
wiring, and the engine fork. The OHTTP/HPKE/TEE protocol and response verification live in
[opengradient-ohttp](https://github.com/OpenGradient/sdk/tree/main/packages/opengradient-ohttp); report
issues in that layer there. The underlying agent engine is [Hermes Agent](https://hermes-agent.nousresearch.com).

## Before you commit

Run `pytest tests/` before sending a change that touches `privacy/` or the installers.

The engine is pinned in `engine.pin`. To move it: change both lines there, run
`scripts/fork-engine.sh` (it stops if a privacy patch no longer applies to the new release), then
`GHOST_ENGINE=~/.ghost-engine pytest tests/test_engine.py`, which checks that the profile matches the
release's config schema and that nothing phones home. CI does the same on macOS, Linux and Windows.
