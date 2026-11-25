# ghost 👻

[![License: MIT](https://img.shields.io/badge/License-MIT-3DDC97?style=flat-square)](LICENSE) &nbsp;![Platform](https://img.shields.io/badge/platform-macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-lightgrey?style=flat-square) &nbsp;[![Built on Hermes Agent](https://img.shields.io/badge/built%20on-Hermes%20Agent-7C5CFF?style=flat-square)](https://github.com/NousResearch/hermes-agent) &nbsp;![Open-weight only](https://img.shields.io/badge/models-open--weight%20only-FF8A3D?style=flat-square)

**A private, unrestricted agentic harness.** A real terminal agent that runs commands, edits files, executes code, and searches the web, with every hosted request routed through OpenGradient's TEE gateway so the model provider never sees your prompts. It answers what you actually ask, drops to a fully-offline local model on demand, and phones home to no one.

Built on the [Hermes Agent](https://github.com/NousResearch/hermes-agent) engine by Nous Research, wired to OpenGradient's gateway and to only open-weight, unrestricted models.

<p align="center">
  <img src="assets/ghost-demo.gif" alt="ghost writing and running a port scanner, fully private" width="820">
</p>

## Install

One command, about two minutes. The only prerequisite is `git`; ghost brings its own Python, Node and engine, and touches nothing else on your machine.

**macOS, Linux, WSL2:**

```bash
curl -fsSL https://raw.githubusercontent.com/OpenGradient/ghost/main/install.sh | bash
```

**Windows (PowerShell, no administrator needed):**

```powershell
irm https://raw.githubusercontent.com/OpenGradient/ghost/main/install.ps1 | iex
```

Then:

```bash
ghost
```

The first run asks how you want to log in. Nothing is contacted before that.

| | |
|---|---|
| **DreamFort account** | Your plan and credits. ghost shows a code, you type it on [dreamfort.ai](https://www.dreamfort.ai), done. Works over SSH too. |
| **DreamFort API key** | Billed by usage. Create one at [api.dreamfort.ai](https://api.dreamfort.ai/api-keys) and paste it. |

`ghost login` switches between them. In a script or a container: `ghost-login --with-api-key < keyfile`.

## Everyday commands

| | |
|---|---|
| `ghost` | Start chatting. `ghost -c` continues your last chat. |
| `ghost -z "question"` | Ask once and print the answer. |
| `ghost --local` | Use the offline model: nothing leaves this machine. |
| `ghost usage` / `ghost budget` | What hosted models have cost you, and the hourly limit on it. |
| `ghost login` / `ghost logout` | Change how you're connected. |
| `ghost update` | Get the latest release, with the options you installed with. |
| `ghost uninstall` | Remove ghost. Your chats and settings stay unless you add `--purge`. |

Inside a chat, `/model` switches models, `/goal <objective>` sets a standing goal, `/help` lists the rest.

<details>
<summary>Install options</summary>

Set these as environment variables on the install command (`curl ... | GHOST_LOCAL=1 bash`). `ghost update` remembers them; `=0` switches one off again.

| | |
|---|---|
| `GHOST_LOCAL=1` | Also install Ollama and an offline model (6GB), which becomes the fallback. `GHOST_LOCAL_32B=1` adds the stronger 32B (26GB). |
| `GHOST_NO_BROWSER=1` | Skip Chromium and the computer-use driver: a smaller, faster install. |
| `GHOST_VERBOSE=1` | Show everything the installer runs. Otherwise it's in `~/.ghost/install.log`. |

On Windows, from a clone: `.\install.ps1 -Local -NoBrowser`.

An update installs that release's `config.yaml` and `SOUL.md`; if you changed yours, they're kept beside them as `.prev`.

</details>

## Why ghost exists

### Problem #1: The Model Lectures You Instead of Working

> "The Net interprets censorship as damage and routes around it."
>
> John Gilmore, [EFF cofounder](https://en.wikipedia.org/wiki/John_Gilmore_(activist))

**The Problem.** Frontier models refuse, moralize, and water answers down. You ask something direct, security research, something adult, something dual-use, something merely uncomfortable, and you get a disclaimer and a redirect to "safer alternatives."

**The Fix.** ghost only connects **open-weight, unrestricted models** (Abliterated Large v2 by default, with its refusals removed from the weights; plus DeepSeek V4 Pro and Hermes 4 405B, steered), so the default answers in full with no sermon. Closed, refusing models (Claude, GPT, Gemini, Grok) aren't offered, and the gateway rejects anything off the list. It treats you as a competent adult, but it isn't an edgelord either: it won't volunteer illegal or shock content, it just won't refuse you.

### Problem #2: The Provider Reads Everything You Send

> "Privacy is the power to selectively reveal oneself to the world."
>
> Eric Hughes, [A Cypherpunk's Manifesto](https://www.activism.net/cypherpunk/manifesto.html)

**The Problem.** "Hosted inference" means your prompts, your code, your secrets, whatever you're working on, land in plaintext on someone else's servers, logged and trained on.

**The Fix.** Every hosted request is sealed on your machine (HPKE/OHTTP, with [opengradient-ohttp](https://github.com/OpenGradient/sdk/tree/main/packages/opengradient-ohttp)) and run inside a **TEE enclave**: the relay sees only ciphertext and never the prompt, the enclave runs the model but never learns who you are, and the enclave's signature is checked before a single token reaches you. Need zero egress? `ghost --local` runs an offline model where nothing leaves the box.

> [!TIP]
> And it doesn't give up. Most agents stop and ask after the first error; ghost reads the actual error, installs what's missing, changes tactics, and keeps going until the task is done. Set a standing goal with `/goal <objective>` and it works toward it across turns on its own.

## ghost vs the alternatives

|  | ghost | a vanilla coding agent | a hosted chat app |
|---|:---:|:---:|:---:|
| Provider sees your prompts | **No** -- TEE + OHTTP | Yes | Yes |
| Refuses / moralizes | **No** -- open-weight + steer | Often | Often |
| Runs fully offline | **Yes** -- `--local` | No | No |
| Real terminal + tools | **Yes** | Yes | No |
| Open-weight models | **Only** | Rarely | Rarely |
| Install needs an LLM | **No** -- one `curl` | Sometimes | n/a |

## The model line-up

Switch with `/model`, all open-weight, nothing closed or refusing:

| Model | What it is |
|---|---|
| `abliteration/abliterated-model-large-v2` **(default)** | Uncensored by abliteration, with refusals removed from the weights rather than steered past. 1M context; built for authorized offensive-security, red-team and long-horizon agent work. |
| `deepseek/deepseek-v4-pro` | Strongest open reasoning + coding model; uncensored via ghost's steer. |
| `abliteration/deepseek-v4.1-flash-abliterated` | Fast, very low-cost abliterated DeepSeek; runs ghost's auxiliary tasks (titles, compression, etc.). |
| `nous/hermes-4-405b` | Flagship uncensored open model, the most steerable. Also the hosted fallback. |
| local (opt-in) | Abliterated 7B / 32B via `GHOST_LOCAL`, fully offline, zero egress. |

## How the private path works

<details>
<summary>The full request path: bridge → relay → TEE enclave</summary>

```
ghost engine
  └─ bridge (127.0.0.1:8788)   limits and budget, seal to an attested enclave
       └─ relay (chat-api)       sees your login and IP, but only ciphertext
            └─ TEE enclave       decrypts, runs the model, signs the reply (the bridge checks it)
```

Two boundaries, the same path [dreamfort.ai](https://www.dreamfort.ai) uses: the **relay** sees your account + IP but only ciphertext; the **enclave** sees the prompt but never your identity. So the hosted path is **private, not anonymous** -- your account is still authenticated and the relay sees your IP. For true anonymity, use the local model (zero egress).

</details>

## Honest limits

- **Hosted models cost credits.** `ghost usage` shows what you've spent, and ghost pauses itself after $5 in an hour so nothing runs away with your balance; change that with `ghost budget 20` or `ghost budget off`. A reply is never paid for twice: what comes back unusable is reported, not retried.
- **Replies arrive whole.** The signature covers the complete reply, so ghost shows it once it's verified rather than word by word.
- **The local model is opt-in and weaker.** Off by default (`GHOST_LOCAL`); it's a weaker agentic searcher and may still lean on the hosted gateway for tool orchestration.
- **The engine is forked, not rewritten.** It's one pinned Hermes release ([`engine.pin`](engine.pin)), patched and self-contained: no separate Hermes install is needed or touched, and `ghost update` is what moves it. Internal package names stay `hermes_cli`.
- **Two small background processes.** The bridge is a service (launchd, systemd or Task Scheduler) that starts with your machine. Your sessions run in a local gateway that ghost starts when you do and that exits after 10 idle minutes (unless you install it as a service with `ghost gateway install`).

## License

[MIT](LICENSE). The Hermes Agent engine it builds on is under its own license.

## Security

ghost is a privacy tool; a leak of your prompts is treated as a P0. See [SECURITY.md](SECURITY.md) for how to report one privately.
