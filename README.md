# ghost

**An incognito, uncensored agentic harness.** Censorship-resistant open intelligence that
defaults to a frontier model over a hardened privacy path, drops to a fully-offline
local model on demand, and phones home to no one.

ghost is a **standalone, debranded fork** of the [Hermes Agent](https://hermes-agent.nousresearch.com)
engine. It runs its own engine at `~/.ghost-engine` (your normie `hermes` install is left
completely untouched), launches as the `ghost` command, and sends every hosted request through a
PII/secret scrubber and then over **Oblivious HTTP (OHTTP) to the OpenGradient chat-api TEE
gateway** -- the same private path the [chat.opengradient.ai](https://chat.opengradient.ai)
website uses.

> **Uncensored by default.** ghost answers everything. The privacy layer governs what leaks
> *out*, never what ghost can *do*. Nothing is filtered, moralized, or redacted in its replies.

---

## Quickstart

One command installs **everything** -- Ollama, the Hermes engine, the local models, the forked +
debranded engine, the privacy stack, and the `ghost` + `ghost-login` commands. Idempotent
(safe to re-run):

```bash
unzip ghost.zip -d ~/ghost && cd ~/ghost && ./install.sh
```

Then connect your account once and run **`ghost`**:

```bash
ghost-login        # browser login -> hands a session token back to this machine
ghost              # chat (default = Hermes 405B via the OpenGradient TEE gateway, OHTTP-private)
```

Inside, `/model` switches between the hosted line-up (Hermes, Claude, GPT, Gemini, Grok) and the
fully-local 32B. Optional install config via env:

```bash
GHOST_DIRECT=1     ./install.sh   # OHTTP bridge talks to chat-api directly (no Webshare rotation)
GHOST_NO_LOCAL=1   ./install.sh   # hosted-only: no Ollama, no local models (lightest)
GHOST_LOCAL_32B=1  ./install.sh   # also pull the stronger 32B local model (26GB)
```

Prerequisites are auto-installed; full details under [Install](#install) below.

---

## How hosted models reach you

Hosted (non-local) models no longer go to Nous directly. They run through the **OpenGradient
chat-api OHTTP relay to a TEE (Trusted Execution Environment) gateway**, resolved from the
on-chain TEE registry:

```
ghost engine
  └─ hosted model ─► OHTTP bridge (:8788)   [scrub PII/secrets, then HPKE-encrypt]
                       └─► chat-api /api/v1/chat/ohttp   [Supabase bearer; RELAY only]
                             └─► TEE gateway   [decrypts in-enclave, runs model, signs output]
```

Two boundaries, like the website: the **relay (chat-api)** sees your account token + IP but only
ciphertext; the **enclave** sees the prompt but never your identity. The scrubber runs *before*
encryption, on plaintext localhost, so your name/email/secrets reach neither the relay nor the
enclave. The TEE's HPKE key + RSA signing key come from a **full read of the on-chain registry**
(the same `getActiveTEEs` / `ohttpConfig` the browser reads; see `privacy/ohttp_client.py`), and
responses are optionally verified against that signing key.

---

## Two modes

ghost runs one of two kinds of model, a deliberate privacy/capability trade:

| | **Default: hosted (Hermes 405B + others)** | **Fallback / on-demand: local 32B** |
|---|---|---|
| Model | `nous/hermes-4-405b` (and Claude/GPT/Gemini/Grok) via the TEE gateway | `uncensored-local` (Qwen2.5-32B-abliterated, Q6) |
| Where it runs | An OpenGradient TEE enclave, reached scrubber → OHTTP → chat-api relay | Your machine, fully offline |
| Privacy | IP hidden from the enclave, content hidden from the relay, PII/secrets scrubbed, **but account-linked to your OpenGradient login** | **True incognito -- nothing leaves the box** |
| Strength | Frontier agentic quality; pick any hosted model with `/model` | Weaker agentic searcher; clean, uncensored prose |
| When | Always, if you're logged in and the gateway is reachable | Auto-fallback if hosted is unavailable, or via `/model` |

The default is the hosted Hermes 405B because it is the stronger agent; the OHTTP path makes it
"private but not anonymous." Switch to the local model any time you want **zero** egress.

---

## What you get

| Layer | Behaviour |
|---|---|
| **Default model** | `nous/hermes-4-405b` via the `opengradient` provider -- scrubber + OHTTP bridge (`:8788`) → chat-api relay → TEE gateway |
| **Hosted line-up** | Hermes 4 (405B/70B), Claude (Fable 5 / Opus / Sonnet / Haiku), GPT-5.x, Gemini 3.5/2.5, Grok 4.x, Seed -- all over the one OHTTP path |
| **Fallback model** | `fallback_model` → local `uncensored-local` (32B) if the hosted gateway is unreachable |
| **Tool / auxiliary model** | Local 7B abliterated (`ghost-tool`) runs titling, compression, triage -- never a hosted provider |
| **Auth** | A Supabase session from `ghost-login` (browser); auto-refreshed. The relay authenticates it; the enclave never sees it |
| **Encryption** | HPKE (DHKEM-X25519 / HKDF-SHA256 / ChaCha20-Poly1305) per request; verified against RFC 9180 vectors |
| **Registry** | Full on-chain read of the active TEE: endpoint, HPKE `ohttpConfig`, and RSA signing key |
| **PII + secret scrubber** | Strips your name/email/handles **and** API keys, tokens, JWTs, private keys from outbound requests, before encryption |
| **Egress proxy** | Rotating residential exit hides your IP from the chat-api relay (skipped in `GHOST_DIRECT`) |
| **Web search** | Local `ddgs` → rotating proxy → engines. No third-party search API sees the query |
| **Memory / telemetry** | Off / none. Catalog served locally; brightdata + codex MCPs removed; TTS local (piper) |
| **Skills** | Created/installed skills go to `~/.ghost/skills-ghost` -- isolated from your normie `hermes` |
| **Branding** | Forked engine fully debranded -- **GHOST** banner, 👻 figure, all visible text reads Ghost |

---

## Architecture

```
  ghost  ──►  ~/.ghost-engine  (standalone, debranded fork; normie `hermes` untouched)
                 │
                 │   default (any hosted model)
                 ├─ Hermes 405B / Claude / GPT / Gemini / Grok
                 │     └─► OHTTP bridge (:8788)  [scrub name/keys, then HPKE-encrypt]
                 │           └─► [rotating proxy (:8899), IP hidden]  ─► chat-api /api/v1/chat/ohttp
                 │                 └─► TEE gateway  [decrypts in-enclave, runs model, signs]
                 │   /model or auto-fallback
                 ├─ local 32B (uncensored-local) ─────────────────────► offline, zero egress
                 │
                 ├─ web search ─► ddgs ─► rotating proxy (:8899) ─► search engines (Webshare IPs)
                 │
                 └─ 12 auxiliary tasks ─► local 7B (ghost-tool)   (titling / compression / triage)

  launchd keeps the services alive:  com.advait.hermes-pii-scrubber (OHTTP bridge)
                                      com.advait.hermes-proxy (rotating proxy; omitted in GHOST_DIRECT)
```

A launch preflight (`bin/ghost`) checks the OHTTP bridge `/healthz`, your login status, and the
proxy exit IP, and warns (never hard-blocks) if anything is down -- so the offline path still runs.

---

## The privacy model -- what each layer actually protects

- **Scrubber + OHTTP bridge (`:8788`)** -- an OpenAI-compatible endpoint the engine talks to over
  plaintext localhost. It redacts a denylist (your name/email/handles) + regex secrets (API keys,
  tokens, JWTs, private keys) from request bodies, then **HPKE-encrypts** the request and relays it
  to chat-api. Outbound-only: the local model never routes through it, so local replies are never
  scrubbed.
- **OHTTP / TEE split** -- chat-api is only a *relay*: it sees your bearer token + IP but the request
  body is ciphertext it cannot read. The TEE enclave decrypts and runs the model but is reached
  through the relay, so it never learns who you are. Responses carry a TEE signature verifiable
  against the registry's signing key (`GHOST_TEE_VERIFY=off|warn|strict`, default `warn`).
- **Rotating proxy (`:8899`)** -- picks a fresh Webshare residential exit per connection, so the
  chat-api relay sees a rotating IP, never yours. Also carries the engine's own egress (web search).
- **Private search** -- `ddgs` honours `DDGS_PROXY` (it ignores `HTTPS_PROXY`), so every query
  egresses through the rotating proxy to the engines. No search-API account.
- **No memory, no telemetry** -- memory toolset off; model catalog served locally; gateway/update
  calls blocked or removed.
- **Skill + state isolation** -- ghost's skills live in their own dir; nothing it creates pollutes `hermes`.

---

## Layout

- `profile/` -- `config.yaml` (the full incognito profile), `SOUL.md` (the Ghost identity), `.env.example`, `pii_denylist.example.txt`, `uncensored_prefill.json`
- `privacy/`
  - `scrubbing_proxy.py` -- the PII/secret scrubber + **OHTTP bridge** to chat-api + local model-catalog endpoint
  - `ohttp_client.py` -- HPKE/OHTTP encryption, chunked-stream decryption, TEE-signature verification, and **full on-chain TEE registry reading**
  - `chat_auth.py` -- stores + auto-refreshes the Supabase session captured at login
  - `chat_login.py` -- the `ghost-login` flow (loopback listener + browser hand-off; `--paste` fallback)
  - `rotating_proxy.py` -- Webshare rotation + blocklist · `gen_searxng_settings.py`
- `scripts/` -- `fork-engine.sh` (copy + relocate venv + isolate skills) and `debrand.py` (scrub visible strings + the two ASCII-art logos)
- `launchd/` -- the OHTTP-bridge + rotating-proxy service templates
- `bin/ghost` -- the launcher (privacy preflight) · `bin/ghost-login` -- account connect/refresh
- `install.sh` -- end-to-end installer · `models.txt` -- the local models to pull

---

## Install

**One command installs everything** -- [Ollama](https://ollama.com), the
[Hermes Agent](https://hermes-agent.nousresearch.com) engine, the local models, the forked +
debranded engine, the privacy stack (httpx + cryptography + web3), and the `ghost` +
`ghost-login` commands. Idempotent (safe to re-run):

```bash
./install.sh
```

The default is the full private setup: it auto-installs prerequisites, pulls the local models,
starts the OHTTP bridge + rotating proxy, forks + debrands the engine into `~/.ghost-engine`,
offers to run the account login, installs `ghost`, and smoke-tests it.

**Config modes (optional env vars):**

```bash
GHOST_DIRECT=1     ./install.sh   # OHTTP bridge talks to chat-api directly (no Webshare rotation, no personal denylist)
GHOST_NO_LOCAL=1   ./install.sh   # hosted-only (auxiliary + fallback routed to hosted hermes-4-70b)
GHOST_LOCAL_32B=1  ./install.sh   # also pull the stronger 32B local model (26GB)
GHOST_CHAT_APP_URL=https://...    # override the website used for ghost-login (default chat.opengradient.ai)
```

- **`GHOST_DIRECT=1`** -- for a shared box without your Webshare proxies: keeps the OHTTP bridge (it's
  the hosted path) but skips the rotating proxy and the personal PII denylist; the bridge connects to
  chat-api directly.

After install, **connect your account** and personalize the scrubber denylist:

```bash
ghost-login                 # browser login (or: ghost-login --paste for headless)
ghost-login --status        # who am I logged in as?
# edit ~/.ghost/privacy/pii_denylist.txt with your name/email/handles
```

```bash
ghost                       # chat (default = Hermes 405B via the TEE gateway, OHTTP-private)
ghost --yolo -z "..."       # one-shot
ghost --paths "..."         # agentic file work: real filesystem paths reach the hosted model
                            #   (your name + secrets in content are still scrubbed)
# inside:  /model           -> switch between the hosted line-up and uncensored-local
```

### Agentic file work -- two ways

By default the scrubber redacts everything outbound, **including filesystem paths** -- which
breaks file ops on the hosted model (it sees `/Users/[REDACTED_PII]/...`). Two ways to do real
file work:

- **`ghost --paths`** -- flips on *path-aware* mode for that session: real paths pass through to the
  hosted model so it can read/write files, while your name + secrets in prose are still scrubbed.
  The trade is that your home/username becomes visible inside those paths.
- **Local model** (`/model` → `uncensored-local`) -- never touches the scrubber/bridge, so paths are
  always real and **nothing leaves the box**. Weaker agent, but the fully-private option.

---

## Honest limits

- **The hosted default is private, not anonymous.** chat-api still authenticates your OpenGradient
  account. The scrubber hides your name/secrets, OHTTP hides your content from the relay, and the
  proxy hides your IP -- but the account link remains. For zero-egress anonymity, switch to the
  **local 32B** (`/model`) -- that is the true-incognito mode.
- **Response verification is best-effort by default.** `GHOST_TEE_VERIFY=warn` logs a warning on any
  signature/hash mismatch but still serves the reply (so subtle request-shape differences from an
  agentic, tool-using request never break chat). Set `GHOST_TEE_VERIFY=strict` to refuse unverified
  hosted responses, or `off` to skip it.
- **The local fallback isn't perfectly offline under tool-use enforcement.** `tool_use_enforcement: true`
  makes search reliable, but the 32B's agentic loop will lean on the hosted gateway for tool
  orchestration (still scrubbed + OHTTP, but account-linked), and the 32B is a weak agentic searcher.
- **Proxies are trust-shifted.** Webshare sees your real IP unless you run a VPN in front. NordVPN on
  this Mac is GUI-only (no CLI), so enable its auto-connect manually for the extra hop.
- **The engine is forked, not rewritten.** Internal Python package names stay `hermes_cli` (invisible to
  users). `hermes update` updates only the original install; re-run `scripts/fork-engine.sh` to pull
  upstream changes into the fork.

---

## License

Personal tooling. The Hermes Agent engine is under its own license.
