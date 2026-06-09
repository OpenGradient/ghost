# ghost

**An incognito, uncensored agentic harness.** Censorship-resistant open intelligence that
runs local-by-default and leaks nothing it doesn't have to.

ghost is a hardened, de-branded profile for the [Hermes Agent](https://hermes-agent.nousresearch.com)
engine. It defaults to a fully-local uncensored model, routes any optional hosted traffic
through a rotating proxy + a PII/secret scrubber, searches the web privately, and phones
home to no one.

> **Uncensored by default.** ghost answers everything. The privacy layer governs what leaks
> *out*, never what ghost can *do*. On the local default, nothing is filtered or redacted.

## What you get

| Layer | Behaviour |
|---|---|
| **Default model** | Local 32B abliterated (`uncensored-local`, Qwen2.5-32B) -- fully offline, non-reasoning, clean output |
| **Tool / auxiliary model** | Local 7B abliterated (`ghost-tool`) runs titling, compression, triage -- never a hosted provider |
| **Hosted (opt-in via `/model`)** | `hermes-4-405b` / `hermes-4-70b` only -- routed through the scrubber + rotating proxy |
| **Model picker** | Locked to those three. No Claude / GPT / Gemini, ever |
| **Web search** | Local `ddgs` → your rotating Webshare proxy → engines. No third-party search API sees the query |
| **PII + secret scrubber** | Strips your name/email **and** API keys/tokens/JWTs/private-keys from outbound hosted requests |
| **Egress proxy** | Rotating residential exits per connection + a blocklist that refuses telemetry/pricing phone-homes |
| **Memory** | Off. No persistence, no profiling, no "I remember you across sessions" |
| **Telemetry** | None. Catalog served locally; update/pricing/analytics calls blocked |

## Architecture

```
        ghost (local 32B, default)  ── offline, nothing leaves the box
            │
            ├─ web search ─► ddgs ─► rotating proxy (8899) ─► search engines (Webshare IPs)
            │
   /model ─►├─ hosted 405B/70B ─► PII+secret scrubber (8788) ─► rotating proxy (8899) ─► Nous
            │
            └─ auxiliary tasks ─► local 7B (ghost-tool)
```

## Install

Requires [Ollama](https://ollama.com) and the Hermes Agent engine.

```bash
./install.sh
```

It pulls the local models, installs the privacy scripts + launchd services, writes the
profile, optionally fetches your Webshare proxy list, and installs the `ghost` command.

```bash
ghost                       # chat on the local default (offline, uncensored)
ghost --yolo -z "..."       # one-shot
# inside: /model  -> switch between local, hermes-4-405b, hermes-4-70b
```

## Layout

- `profile/` -- `config.yaml` (incognito settings), `SOUL.md` (the Ghost identity), `.env.example`, `pii_denylist.example.txt`, `uncensored_prefill.json`
- `privacy/` -- `rotating_proxy.py`, `scrubbing_proxy.py` (PII+secret guard + local model catalog), `ensure_scrubber_route.py`, `gen_searxng_settings.py`
- `launchd/` -- the rotating-proxy + scrubber service templates
- `bin/ghost` -- the launcher (privacy preflight + warns if infra is down)
- `models.txt` -- the local models to pull

## Honest limits

- **Hosted models aren't anonymous.** Nous still authenticates your account when you opt
  into 405B/70B. The proxy hides your IP and the scrubber hides your PII/secrets, but the
  account link remains. The **local default is the true incognito mode.**
- **Engine fork.** `install.sh` forks the engine into a standalone, debranded `~/.ghost-engine`
  (`scripts/fork-engine.sh` copies + relocates the venv, `scripts/debrand.py` scrubs visible
  strings) -- the banner, logo (👻), and user-facing text all read **Ghost**, and your normie
  `hermes` install is untouched. Internal Python package names stay `hermes_cli` (invisible to
  users; renaming them across ~1M lines would break imports for no user-facing gain).
- **Proxies are trust-shifted.** Webshare sees your real IP unless you also run a VPN in front.

## License

Personal tooling. The Hermes Agent engine is under its own license.
