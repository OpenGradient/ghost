# Security Policy

ghost is a privacy tool. Its core promise is that your prompts, names, and secrets never reach
the relay or the TEE enclave in the clear, and that responses are verified before a token is
emitted. A bug that breaks that promise is the most serious kind of bug here.

## Reporting a vulnerability

Please report privately, not in a public issue:

- Open a private security advisory via the repository's **Security** tab
  (GitHub: *Security → Report a vulnerability*), or
- Contact the OpenGradient maintainers directly.

Include what you found, how to reproduce it, and the impact. We aim to acknowledge quickly.

## What we treat as P0

- **Any PII or secret leak** to og-veil, the chat-api relay, or the enclave -- e.g. a request
  field that is not scrubbed, a placeholder that is de-anonymized into an egress tool, or the
  inference route being repointed away from the local scrubber.
- **A response accepted without verification** (the verify-before-emit guarantee bypassed).
- **The engine phoning home** -- an update check, telemetry, a remote catalog or plugin fetch, or a
  side task that leaves the private path.

## Scope

This repo owns the local harness: the PII/secret scrubber (`privacy/`), the install + service
wiring, and the engine fork. The OHTTP/HPKE/TEE protocol and response verification live in
[og-veil](https://github.com/OpenGradient/veil); report issues in that layer there. The
underlying agent engine is [Hermes Agent](https://hermes-agent.nousresearch.com).

## Before you commit

The scrubber's own test suite guards against leak regressions (`pytest tests/`, including the
golden leak canary). Run it before sending a change that touches `privacy/`.

The engine is pinned in `engine.pin`. To move it: change both lines there, run
`scripts/fork-engine.sh` (it stops if a privacy patch no longer applies to the new release), then
`GHOST_ENGINE=~/.ghost-engine pytest tests/test_engine.py`, which checks that the profile matches the
release's config schema and that nothing phones home. CI does the same on macOS, Linux and Windows.
