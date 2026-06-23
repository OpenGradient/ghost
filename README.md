# ghost

**An incognito, uncensored agentic harness.** Censorship-resistant open intelligence that
defaults to a frontier model over a hardened privacy path, drops to a fully-offline
local model on demand, and phones home to no one.

ghost is a **standalone, debranded fork** of the [Hermes Agent](https://hermes-agent.nousresearch.com)
engine. It runs its own engine at `~/.ghost-engine` (your normie `hermes` install is left
completely untouched), launches as the `ghost` command, and sends every hosted request through a
PII/secret scrubber and then through **[og-veil](https://github.com/OpenGradient/veil)**, which
relays it over **Oblivious HTTP (OHTTP) to the OpenGradient chat-api TEE gateway** -- the same
private path the [chat.opengradient.ai](https://chat.opengradient.ai) website uses.

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
GHOST_PROXY=1      ./install.sh   # opt in to the Webshare rotating proxy (IP-mask the relay; off by default)
GHOST_LOCAL=1      ./install.sh   # also install Ollama + a local model for an offline/incognito fallback
GHOST_LOCAL_32B=1  ./install.sh   # pull the stronger 32B local model too (26GB; implies GHOST_LOCAL)
```

Local models are opt-in. By default ghost is hosted-only -- no Ollama, and the fallback + auxiliary
tasks route to a hosted 70B over the same private og-veil path.

Prerequisites are auto-installed; full details under [Install](#install) below.

---

## How hosted models reach you

Hosted (non-local) models no longer go to Nous directly. They run through the **OpenGradient
chat-api OHTTP relay to a TEE (Trusted Execution Environment) gateway**, resolved from the
on-chain TEE registry. ghost no longer hand-rolls that protocol -- it delegates it to
[og-veil](https://github.com/OpenGradient/veil) (the `opengradient-veil` package), the same
implementation the chat-app and the `og-veil` CLI use:

```
ghost engine
  └─ hosted model ─► scrubber (:8788)   [scrub PII/secrets, strip provider prefix]
                       └─► og-veil (:11435)   [HPKE-encrypt, OHTTP relay, verify]
                             └─► chat-api /api/v1/chat/ohttp   [Supabase bearer; RELAY only]
                                   └─► TEE gateway   [decrypts in-enclave, runs model, signs output]
```

Two boundaries, like the website: the **relay (chat-api)** sees your account token + IP but only
ciphertext; the **enclave** sees the prompt but never your identity. The scrubber runs *before*
og-veil encrypts, on plaintext localhost, so your name/email/secrets reach neither the relay nor
the enclave. og-veil reads the TEE's HPKE key + RSA signing key from the on-chain registry and
**verifies every response against that signing key before a single token is returned**.

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
| **Default model** | `nous/hermes-4-405b` via the `opengradient` provider -- scrubber (`:8788`) → og-veil (`:11435`) → chat-api relay → TEE gateway |
| **Hosted line-up** | Hermes 4 (405B/70B), Claude (Fable 5 / Opus / Sonnet / Haiku), GPT-5.x, Gemini 3.5/2.5, Grok 4.x, Seed -- all over the one OHTTP path |
| **Fallback model** | `fallback_model` → local `uncensored-local` (32B) if the hosted gateway is unreachable |
| **Tool / auxiliary model** | Local 7B abliterated (`ghost-tool`) runs titling, compression, triage -- never a hosted provider |
| **Auth** | A Supabase session from `ghost-login` (browser), held + auto-refreshed by og-veil. The relay authenticates it; the enclave never sees it |
| **Encryption** | HPKE (DHKEM-X25519 / HKDF-SHA256 / ChaCha20-Poly1305) per request, done by og-veil; verification before emit |
| **Registry** | og-veil reads the active TEE from the on-chain registry: endpoint, HPKE `ohttpConfig`, and RSA signing key |
| **PII + secret scrubber** | Strips your name/email/handles **and** API keys, tokens, JWTs, private keys from outbound requests, before og-veil encrypts |
| **Egress proxy** | **Opt-in** (`GHOST_PROXY=1`): a rotating residential exit hides your IP from the chat-api relay. Off by default (direct) |
| **Web search** | Local `ddgs` → engines (direct), or via the rotating proxy when `GHOST_PROXY=1`. No third-party search API sees the query |
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
                 │     └─► scrubber (:8788)  [scrub name/keys, strip provider prefix]
                 │           └─► og-veil (:11435)  [HPKE-encrypt, OHTTP, verify]
                 │                 └─► [rotating proxy (:8899), IP hidden — opt-in GHOST_PROXY]  ─► chat-api /api/v1/chat/ohttp
                 │                       └─► TEE gateway  [decrypts in-enclave, runs model, signs]
                 │   /model or auto-fallback
                 ├─ local 32B (uncensored-local) ─────────────────────► offline, zero egress
                 │
                 ├─ web search ─► ddgs ─► [rotating proxy (:8899) if GHOST_PROXY] ─► search engines
                 │
                 └─ 12 auxiliary tasks ─► local 7B (ghost-tool)   (titling / compression / triage)

  launchd keeps the services alive:  com.advait.hermes-pii-scrubber (scrubber)
                                      com.advait.hermes-veil (og-veil; OHTTP/TEE/verify + auth)
                                      com.advait.hermes-proxy (rotating proxy; only with GHOST_PROXY)
```

A launch preflight (`bin/ghost`) checks the scrubber `/healthz`, og-veil `/health`, your login
status, and the proxy exit IP, and warns (never hard-blocks) if anything is down -- so the offline
path still runs.

---

## The privacy model -- what each layer actually protects

- **Scrubber (`:8788`)** -- an OpenAI-compatible endpoint the engine talks to over plaintext
  localhost. It redacts a denylist (your name/email/handles) + regex secrets (API keys, tokens,
  JWTs, private keys) from request bodies, then forwards the cleaned request to og-veil. Outbound-
  only: the local model never routes through it, so local replies are never scrubbed.
- **og-veil (`:11435`)** -- the [`opengradient-veil`](https://github.com/OpenGradient/veil) package
  owns the protocol: it reads the active TEE from the on-chain registry, **HPKE-encrypts** each
  request, relays it over OHTTP to chat-api, and verifies the enclave's signature on every response
  **before emitting a token** (verify-before-emit). One implementation, shared with the chat-app --
  ghost no longer maintains its own copy.
- **OHTTP / TEE split** -- chat-api is only a *relay*: it sees your bearer token + IP but the request
  body is ciphertext it cannot read. The TEE enclave decrypts and runs the model but is reached
  through the relay, so it never learns who you are. (By default ghost is **direct** -- the relay
  sees your real IP; the content stays private regardless. Hide the IP too with the opt-in proxy below.)
- **Rotating proxy (`:8899`) -- opt-in (`GHOST_PROXY=1`), off by default** -- picks a fresh Webshare
  residential exit per connection, so the chat-api relay sees a rotating IP, never yours -- og-veil's
  egress is routed through it. Also carries the engine's own egress (web search).
- **Private search** -- when the proxy is enabled, `ddgs` honours `DDGS_PROXY` (it ignores
  `HTTPS_PROXY`), so every query egresses through the rotating proxy to the engines; otherwise it
  goes out directly. No search-API account either way.
- **No memory, no telemetry** -- memory toolset off; model catalog served locally; gateway/update
  calls blocked or removed.
- **Skill + state isolation** -- ghost's skills live in their own dir; nothing it creates pollutes `hermes`.

---

## Layout

- `profile/` -- `config.yaml` (the full incognito profile), `SOUL.md` (the Ghost identity), `.env.example`, `pii_denylist.example.txt`, `uncensored_prefill.json`
- `privacy/`
  - `scrubbing_proxy.py` -- the PII/secret scrubber + local model-catalog endpoint; forwards cleaned requests to og-veil
  - `rotating_proxy.py` -- Webshare rotation + blocklist · `gen_searxng_settings.py`
  - `ensure_scrubber_route.py` -- self-heals the engine's hosted route back to the scrubber after a token refresh
  - _(the OHTTP/HPKE/registry/verification + Supabase auth that used to live here now comes from the `opengradient-veil` package -- run `og-veil`)_
- `scripts/` -- `fork-engine.sh` (copy + relocate venv + isolate skills) and `debrand.py` (scrub visible strings + the two ASCII-art logos)
- `launchd/` -- the scrubber + og-veil + rotating-proxy service templates
- `bin/ghost` -- the launcher (privacy preflight) · `bin/ghost-login` -- account connect/refresh
- `install.sh` -- end-to-end installer · `models.txt` -- the local models to pull

---

## Install

**One command installs everything** -- [Ollama](https://ollama.com), the
[Hermes Agent](https://hermes-agent.nousresearch.com) engine, the local models, the forked +
debranded engine, the privacy stack (og-veil + httpx), and the `ghost` +
`ghost-login` commands. Idempotent (safe to re-run):

```bash
./install.sh
```

The default is the **direct** private setup: it auto-installs prerequisites, pulls the local models,
starts the scrubber + og-veil, forks + debrands the engine into `~/.ghost-engine`, offers to run the
account login, installs `ghost`, and smoke-tests it. No proxy is set up; og-veil talks to chat-api
directly (content is still private via OHTTP/TEE).

**Config modes (optional env vars):**

```bash
GHOST_PROXY=1      ./install.sh   # opt in to the Webshare rotating proxy (IP-mask the chat-api relay)
GHOST_LOCAL=1      ./install.sh   # opt in to Ollama + a local model (offline/incognito fallback)
GHOST_LOCAL_32B=1  ./install.sh   # pull the stronger 32B local model too (26GB; implies GHOST_LOCAL)
GHOST_CHAT_APP_URL=https://...    # override the website used for ghost-login (default chat.opengradient.ai)
```

- **`GHOST_PROXY=1`** -- opt in to IP-masking: prompts for your Webshare proxy list, runs the rotating
  proxy, and routes both og-veil's egress to chat-api and the engine's web-search egress through it, so
  the relay sees a rotating IP instead of yours. Off by default (direct); the scrubber + personal PII
  denylist run either way.

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
  account. The scrubber hides your name/secrets and OHTTP hides your content from the relay -- but the
  account link remains, and by default (direct) the relay also sees your real IP. Enable the opt-in
  proxy (`GHOST_PROXY=1`) to mask the IP too. For zero-egress anonymity, switch to the **local 32B**
  (`/model`) -- that is the true-incognito mode.
- **Responses are verified before they reach you.** og-veil checks the enclave's signature on every
  hosted response and refuses to emit a single token it can't verify (verify-before-emit) -- ghost no
  longer carries its own best-effort `GHOST_TEE_VERIFY` knob; verification now lives in og-veil.
- **The local fallback isn't perfectly offline under tool-use enforcement.** `tool_use_enforcement: true`
  makes search reliable, but the 32B's agentic loop will lean on the hosted gateway for tool
  orchestration (still scrubbed + OHTTP, but account-linked), and the 32B is a weak agentic searcher.
- **Proxies are opt-in and trust-shifted.** ghost is direct by default (no proxy). With `GHOST_PROXY=1`,
  Webshare sees your real IP unless you run a VPN in front. NordVPN on this Mac is GUI-only (no CLI), so
  enable its auto-connect manually for the extra hop.
- **The engine is forked, not rewritten.** Internal Python package names stay `hermes_cli` (invisible to
  users). `hermes update` updates only the original install; re-run `scripts/fork-engine.sh` to pull
  upstream changes into the fork.

---

## License

Personal tooling. The Hermes Agent engine is under its own license.
