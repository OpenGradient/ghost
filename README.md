# ghost

**An incognito, uncensored agentic harness.** Censorship-resistant open intelligence that
defaults to a frontier open model over a hardened privacy path, drops to a fully-offline
local model on demand, and phones home to no one.

ghost is a **standalone, debranded fork** of the [Hermes Agent](https://hermes-agent.nousresearch.com)
engine. It runs its own engine at `~/.ghost-engine` (your normie `hermes` install is left
completely untouched), launches as the `ghost` command, and wraps every hosted request in a
PII/secret scrubber + a rotating residential proxy.

> **Uncensored by default.** ghost answers everything. The privacy layer governs what leaks
> *out*, never what ghost can *do*. Nothing is filtered, moralized, or redacted in its replies.

---

## Quickstart

One command installs **everything** -- Ollama, the Hermes engine, the local models, the forked +
debranded engine, the privacy stack, and the `ghost` command. Idempotent (safe to re-run):

```bash
unzip ghost.zip -d ~/ghost && cd ~/ghost && ./install.sh
```

Then run **`ghost`**. Default is Hermes 405B (scrubbed + proxied); inside, `/model` drops to the
fully-local 32B. Optional config via env:

```bash
NOUS_API_KEY=sk-nous-... ./install.sh             # auth with a key instead of the browser login
GHOST_DIRECT=1           ./install.sh             # no Webshare proxy/scrubber; talk to Nous directly
NOUS_API_KEY=sk-... GHOST_DIRECT=1 ./install.sh   # share setup: key + no personal privacy stack
```

Prerequisites are auto-installed; full details under [Install](#install) below.

---

## Two modes

ghost runs one of two models, and the difference is a deliberate privacy/capability trade:

| | **Default: Hermes 405B** | **Fallback / on-demand: local 32B** |
|---|---|---|
| Model | `nousresearch/hermes-4-405b` (Nous Portal) | `uncensored-local` (Qwen2.5-32B-abliterated, Q6) |
| Where it runs | Nous, reached through scrubber → proxy | Your machine, fully offline |
| Privacy | IP hidden + PII/secrets scrubbed, **but account-linked to Nous** | **True incognito -- nothing leaves the box** |
| Strength | Frontier agentic quality | Weaker agentic searcher; clean, uncensored prose |
| When | Always, if the Nous portal is reachable | Auto-fallback if 405B is unavailable, or via `/model` |

The default is 405B because it is the stronger agent; the privacy path (below) makes it
"private but not anonymous." Switch to the local model any time you want **zero** egress.

---

## What you get

| Layer | Behaviour |
|---|---|
| **Default model** | `hermes-4-405b` via the `nous` provider -- routed scrubber (`:8788`) → rotating proxy (`:8899`) → Nous |
| **Fallback model** | `fallback_model` → local `uncensored-local` (32B) if 405B is unreachable |
| **Tool / auxiliary model** | Local 7B abliterated (`ghost-tool`) runs titling, compression, triage -- never a hosted provider |
| **Model picker** | Locked to exactly three: `hermes-4-405b`, `hermes-4-70b`, `uncensored-local`. No Claude / GPT / Gemini, ever (served from a local catalog, not nousresearch.com) |
| **Web search** | Local `ddgs` → rotating Webshare proxy → engines. No third-party search API sees the query |
| **PII + secret scrubber** | Strips your name/email/handles **and** API keys, tokens, JWTs, private keys from outbound hosted requests |
| **Egress proxy** | Rotating residential exit per connection + a blocklist that refuses telemetry/pricing phone-homes (e.g. openrouter.ai) |
| **Memory** | Off. No persistence, no profiling, no "I remember you across sessions" |
| **Telemetry** | None. Catalog served locally; update/pricing/analytics calls blocked; brightdata + codex MCPs removed; TTS local (piper) |
| **Skills** | Created/installed skills go to `~/.ghost/skills-ghost` -- isolated from your normie `hermes` skills |
| **Branding** | Forked engine fully debranded -- **GHOST** title banner, 👻 figure, and all visible text read Ghost |

---

## Architecture

```
  ghost  ──►  ~/.ghost-engine  (standalone, debranded fork; normie `hermes` untouched)
                 │
                 │   default
                 ├─ Hermes 405B ─► PII+secret scrubber (:8788) ─► rotating proxy (:8899) ─► Nous
                 │                     (name/keys stripped)         (Webshare exit, IP hidden)
                 │   /model or auto-fallback
                 ├─ local 32B (uncensored-local) ─────────────────────────────► offline, zero egress
                 │
                 ├─ web search ─► ddgs ─► rotating proxy (:8899) ─► search engines (Webshare IPs)
                 │
                 └─ 12 auxiliary tasks ─► local 7B (ghost-tool)   (titling / compression / triage)

  launchd keeps the two services alive:  com.advait.hermes-proxy · com.advait.hermes-pii-scrubber
```

A launch preflight (`bin/ghost`) checks the scrubber `/healthz` + the proxy, prints the exit IP,
and warns (never hard-blocks) if the privacy infra is down -- so the offline path still runs.

---

## The privacy model -- what each layer actually protects

- **Scrubber (`:8788`)** -- an OpenAI-compatible reverse proxy in front of Nous. Redacts a denylist
  (your name/email/handles) + regex secrets (API keys, tokens, JWTs, private keys) from request
  bodies before they leave the machine. Outbound-only: the local model never routes through it, so
  local replies are never scrubbed.
- **Rotating proxy (`:8899`)** -- zero-dependency CONNECT proxy that picks a fresh Webshare residential
  exit per connection, so Nous + search engines see a rotating IP, never yours. Carries a **blocklist**
  that 403s known phone-homes (the engine's anonymous openrouter.ai pricing fetch).
- **Private search** -- `ddgs` honours `DDGS_PROXY` (it ignores `HTTPS_PROXY`), so every query egresses
  through the rotating proxy to the engines. No Nous gateway, no BrightData, no search-API account.
- **No memory, no telemetry** -- memory toolset off; model catalog served locally; gateway/update/pricing
  calls blocked or removed.
- **Skill + state isolation** -- ghost's skills live in their own dir; nothing it creates pollutes `hermes`.

---

## Layout

- `profile/` -- `config.yaml` (the full incognito profile), `SOUL.md` (the Ghost identity), `.env.example`, `pii_denylist.example.txt`, `uncensored_prefill.json`
- `privacy/` -- `rotating_proxy.py` (Webshare rotation + blocklist), `scrubbing_proxy.py` (PII/secret scrubber + local model-catalog endpoint), `ensure_scrubber_route.py`, `gen_searxng_settings.py`
- `scripts/` -- `fork-engine.sh` (copy + relocate venv + isolate skills) and `debrand.py` (scrub visible strings + the two ASCII-art logos)
- `launchd/` -- the rotating-proxy + scrubber service templates
- `bin/ghost` -- the launcher (privacy preflight; execs the forked engine on the `uncensored` profile)
- `install.sh` -- end-to-end installer · `models.txt` -- the local models to pull

---

## Install

**One command installs everything** -- [Ollama](https://ollama.com), the
[Hermes Agent](https://hermes-agent.nousresearch.com) engine, the local models, the forked +
debranded engine, the privacy stack, and the `ghost` command. Idempotent (safe to re-run):

```bash
./install.sh
```

The default is the full private setup: it auto-installs the prerequisites, pulls the local models,
starts the rotating proxy + PII/secret scrubber, forks + debrands the engine into `~/.ghost-engine`,
opens the Nous Portal browser login for the default 405B, wires the scrubber routing, installs
`ghost`, and smoke-tests it.

**Config modes (optional env vars):**

```bash
NOUS_API_KEY=sk-nous-... ./install.sh            # auth 405B with a key, no browser login
GHOST_DIRECT=1           ./install.sh            # no Webshare proxy/scrubber; talk to Nous directly
NOUS_API_KEY=sk-... GHOST_DIRECT=1 ./install.sh  # share setup: key + no personal privacy stack
```

- **`NOUS_API_KEY`** -- non-interactive auth (headless / sharing); uses the engine's `auth add` under the hood.
- **`GHOST_DIRECT=1`** -- for a machine without your Webshare proxies: skips the local privacy stack, points 405B straight at Nous, and `ghost` skips its privacy gate.

After a default install, personalize `~/.ghost/privacy/pii_denylist.txt` with your own
name/email/handles so the scrubber redacts them on the hosted path.

```bash
ghost                       # chat (default = Hermes 405B, scrubbed + proxied)
ghost --yolo -z "..."       # one-shot
ghost --paths "..."         # agentic file work: real filesystem paths reach 405B
                            #   (your name + secrets in content are still scrubbed)
# inside:  /model           -> switch between hermes-4-405b, hermes-4-70b, uncensored-local
```

### Agentic file work -- two ways

By default the scrubber redacts everything outbound, **including filesystem paths** -- which
breaks file ops on 405B (it sees `/Users/[REDACTED_PII]/...`). Two ways to do real file work:

- **`ghost --paths`** -- flips on *path-aware* mode for that session: real paths pass through to
  405B so it can read/write files, while your name + secrets in prose are still scrubbed. The
  trade is that your home/username becomes visible to Nous inside those paths.
- **Local model** (`/model` → `uncensored-local`) -- never touches the scrubber, so paths are
  always real and **nothing leaves the box**. Weaker agent, but the fully-private option.

---

## Honest limits

- **The default (405B) is private, not anonymous.** Nous still authenticates your account. The
  scrubber hides your name/secrets and the proxy hides your IP, but the account link remains. For
  zero-egress anonymity, switch to the **local 32B** (`/model`) -- that is the true-incognito mode.
- **The local fallback isn't perfectly offline under tool-use enforcement.** `tool_use_enforcement: true`
  makes search reliable, but the 32B's agentic loop will lean on hosted Nous inference for tool
  orchestration (still scrubbed + proxied, but account-linked), and the 32B is a weak agentic searcher.
  A fully-offline local agent needs per-model enforcement scoping -- not yet wired.
- **Proxies are trust-shifted.** Webshare sees your real IP unless you run a VPN in front. NordVPN on
  this Mac is GUI-only (no CLI), so it can't be scripted -- enable its auto-connect manually for the extra hop.
- **The engine is forked, not rewritten.** Internal Python package names stay `hermes_cli` (invisible to
  users; renaming across ~1M lines / 2,282 files would break imports for no user-facing gain). `hermes update`
  updates only the original install; re-run `scripts/fork-engine.sh` to pull upstream changes into the fork.

---

## License

Personal tooling. The Hermes Agent engine is under its own license.
