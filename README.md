# ghost 👻

**A private, unrestricted agentic harness.** ghost is a real terminal agent -- it runs commands, edits files, executes code, and searches the web -- routing every hosted request through OpenGradient's private TEE gateway. It answers what you actually ask, in full, with no refusals or moralizing, and can drop to a fully-offline local model where nothing leaves the box. Built on the [Hermes Agent](https://github.com/NousResearch/hermes-agent) engine by Nous Research, wired to OpenGradient's gateway and to only open-weight, unrestricted models.

<table>
<tr><td><b>Private by construction</b></td><td>Every hosted request is HPKE/OHTTP-encrypted by <a href="https://github.com/OpenGradient/veil">og-veil</a> and run inside a TEE enclave: the relay sees only ciphertext and the enclave never sees who you are. Optional <code>ghost --scrub</code> also strips your name/secrets locally before encryption (off by default, so the agent keeps full fidelity for real work).</td></tr>
<tr><td><b>Unrestricted, open-weight only</b></td><td>DeepSeek V4 Pro (default), Hermes 4 405B/70B -- open-weight models only. The default is steered to drop the usual refusals; closed, refusing models (Claude, GPT, Gemini, Grok) aren't offered, and the gateway rejects anything off the list.</td></tr>
<tr><td><b>Verified responses</b></td><td>og-veil checks the enclave's signature on every response and refuses to emit a token it can't verify.</td></tr>
<tr><td><b>Offline mode</b></td><td>Opt in with <code>GHOST_LOCAL=1</code> and switch with <code>ghost --local</code> -- a local abliterated model, zero egress, nothing leaves your machine.</td></tr>
<tr><td><b>Relentless agent</b></td><td>Reads real errors, installs what it's missing, changes tactics, and keeps going until the task is done -- it doesn't stop to ask after one failure.</td></tr>
<tr><td><b>No memory, no telemetry</b></td><td>Catalog served locally, web search via local <code>ddgs</code>, no third-party search account, isolated state in <code>~/.ghost</code>.</td></tr>
</table>

---

## Quick Install

**macOS only** (the privacy stack runs as launchd services). One command installs everything -- the engine into `~/.ghost-engine` (it leaves any existing `hermes` install untouched), the privacy stack, and the `ghost` + `ghost-login` commands. Idempotent:

```bash
./install.sh
```

Then connect once and go:

```bash
ghost-login        # browser login -> session token for this machine
ghost              # chat (default: DeepSeek V4 Pro via the TEE gateway)
ghost --yolo       # auto-accept tool approvals (skip-permissions)
ghost --resume <session_id>     # resume a past session  (find one with: ghost sessions browse)
ghost --local      # force the offline local model (needs GHOST_LOCAL)
```

Optional at install time:

```bash
GHOST_LOCAL=1     ./install.sh   # also install a local model (offline/incognito fallback)
GHOST_LOCAL_32B=1 ./install.sh   # pull the stronger 32B local model too (26GB)
```

By default ghost is hosted-only -- no Ollama; the fallback and auxiliary tasks also run hosted over the same private path.

**Update any time** with `ghost update` -- it pulls the latest source and re-runs the installer (idempotent, keeps your options). To also move to a newer upstream Hermes engine, run `hermes update` first, then `ghost update`.

---

## Models

Switch with `/model` -- all open-weight, nothing closed or refusing:

| Model | What it is |
|---|---|
| `deepseek/deepseek-v4-pro` **(default)** | Strongest open reasoning + coding model; best for agentic work. Uncensored via ghost's per-model steer. |
| `nous/hermes-4-405b` | Flagship uncensored open model, most steerable. Also the hosted fallback. |
| `nous/hermes-4-70b` | Fast, low-cost; runs ghost's auxiliary tasks. |
| local (opt-in) | Abliterated 7B (`ghost-tool`), or 32B (`uncensored-local`) with `GHOST_LOCAL_32B`. Fully offline. |

---

## How it stays private

Every hosted request takes the same private path: ghost's local bridge hands it to [og-veil](https://github.com/OpenGradient/veil) (the `opengradient-veil` package, the same one the [chat.opengradient.ai](https://chat.opengradient.ai) site uses), which encrypts it and relays it over Oblivious HTTP to a TEE enclave:

```
ghost engine
  └─ bridge (:8788)       strip provider prefix, model steer (+ PII/secret scrub if --scrub)
       └─ og-veil (:11435)   HPKE-encrypt, OHTTP relay, verify signature before emit
            └─ chat-api relay   sees your account token + IP, but only ciphertext
                 └─ TEE enclave   decrypts, runs the model, signs the output
```

Two boundaries: the **relay** sees your account + IP but only ciphertext; the **enclave** sees the prompt but never your identity. Redaction is **off by default** so ghost stays full-fidelity (it can read and use secrets during real work, e.g. authorized pentesting); `ghost --scrub` turns on a local pass that strips your name/secrets before encryption. Either way the hosted path is **private, not anonymous**: your OpenGradient account is still authenticated and the relay sees your IP. For true anonymity, use the local model -- zero egress.

---

## Honest limits

- **The local model is opt-in and weaker.** Off by default (install with `GHOST_LOCAL`); it's a weaker agentic searcher and may still lean on the hosted gateway for tool orchestration under tool-use enforcement.
- **The engine is forked, not rewritten.** Internal package names stay `hermes_cli`, and `ghost update` (not `hermes update`) is what refreshes the fork.

---

## License

[MIT](LICENSE). The Hermes Agent engine it builds on is under its own license.

## Security

ghost is a privacy tool; a PII/secret leak is treated as a P0. See [SECURITY.md](SECURITY.md) for how to report one privately.
