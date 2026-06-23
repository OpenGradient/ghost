#!/usr/bin/env python3
"""NER-based PII detection with reversible placeholders for ghost's hosted-path scrubber.

Upgrades the old regex+denylist scrubber. Presidio + spaCy (en_core_web_md) detect PII
semantically, so it:
  - catches names/PII WITHOUT a hand-curated denylist (fixes under-redaction: the old
    denylist shipped empty, so a fresh user's name was never scrubbed),
  - does NOT flag your own domain/company/city -- we deliberately omit URL / ORGANIZATION /
    LOCATION from the redaction set (fixes the over-redaction that mangled
    https://chat.opengradient.ai into https://chat.[REDACTED_PII].ai and broke the query),
  - replaces PII with STABLE PLACEHOLDERS (<PERSON_1>, <EMAIL_2>) + a local map, so the
    model reasons over coherent text (not "[REDACTED_PII][REDACTED_PII]" mush) and the real
    values can be restored into the model's RESPONSE locally (reversible; map never leaves
    the box),
  - always scrubs secrets (API keys, JWTs, private keys) via regex, even when PII redaction
    is off -- leaking a credential never helps a query.

spaCy NER + Presidio pattern recognizers (credit-card Luhn, RFC-822 email, etc.). The engine
is loaded once per process (~250ms first call, warm thereafter). Detection offsets + scores
are also what powers the render/preview.
"""
import codecs
import json
import re
import threading

# Personal-PII entity types we redact. Deliberately NO URL / ORGANIZATION / LOCATION so a
# user's own domain, company, or city in a prompt is not mangled.
REDACT_ENTITIES = [
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "CREDIT_CARD", "US_SSN",
    "IP_ADDRESS", "IBAN_CODE", "US_BANK_NUMBER", "CRYPTO", "MEDICAL_LICENSE",
]
SCORE_THRESHOLD = 0.5  # ignore low-confidence hits to avoid over-redaction

# Secrets: always scrubbed (regex), even with PII redaction off. Shared with the legacy scrubber
# via scrub_patterns.py so the two paths never drift on the security-critical secret set.
from scrub_patterns import SECRET_RES  # noqa: E402

_PH_RE = re.compile(r"<([A-Z_]+)_(\d+)>")
_analyzer = None
_lock = threading.Lock()

# Tool-call args are de-anonymized before LOCAL execution (so the real value hits disk / runs),
# but NOT for tools that egress externally -- a web_search / browser / messaging / image-gen /
# delegation call must never leak the real name/secret to a third party; those keep the
# <placeholder>. This is DEFAULT-DENY: only an explicit allow-list of local-execution tools
# (filesystem, shell, code, local state) gets real values restored. Anything else -- web,
# browser, computer-use, comms, image/tts, delegation, and any UNKNOWN tool -- is treated as
# egress and keeps the placeholder, so a newly-added tool can't silently leak by default.
# Names match the engine's actual tool ids (terminal/read_file/execute_code/patch/...).
_LOCAL_TOOLS = frozenset({
    "terminal", "shell", "bash", "sh", "process",
    "execute_code", "code_execution", "code", "python", "run_code", "run",
    "read_file", "write_file", "edit_file", "create_file", "str_replace",
    "str_replace_editor", "apply_patch", "patch", "file", "files",
    "search_files", "list_files", "glob", "grep",
    "todo", "todos", "memory", "session_search", "clarify",
    "skill_manage", "skill_view", "skills_list",
})


def _is_local_tool(name):
    return bool(name) and name.lower() in _LOCAL_TOOLS


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        with _lock:
            if _analyzer is None:
                from presidio_analyzer import AnalyzerEngine
                from presidio_analyzer.nlp_engine import NlpEngineProvider
                prov = NlpEngineProvider(nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [{"lang_code": "en", "model_name": "en_core_web_md"}],
                })
                a = AnalyzerEngine(nlp_engine=prov.create_engine(), supported_languages=["en"])
                a.analyze(text="warmup", entities=REDACT_ENTITIES, language="en")  # warm
                _analyzer = a
    return _analyzer


def detect(text):
    """Return PII spans as (start, end, entity_type, score, surface), above threshold, sorted."""
    if not text or not isinstance(text, str):
        return []
    res = _get_analyzer().analyze(text=text, entities=REDACT_ENTITIES, language="en")
    spans = [(r.start, r.end, r.entity_type, float(r.score), text[r.start:r.end])
             for r in res if r.score >= SCORE_THRESHOLD]
    return sorted(spans, key=lambda x: x[0])


def _scrub_secrets(text):
    n = 0
    for rx in SECRET_RES:
        text, c = rx.subn("[REDACTED_SECRET]", text)
        n += c
    return text, n


def anonymize(text, mapping=None, pii=True):
    """Replace PII + secrets with stable, REVERSIBLE placeholders (<PERSON_1>, <SECRET_1>).

    Returns (anonymized_text, mapping, count). `mapping` is {placeholder: original}; pass an
    existing one to keep placeholders consistent across messages/turns. With pii=False only
    secrets are placeholdered. Secrets are reversible (not a fixed [REDACTED_SECRET]) so that
    agentic tasks which write a secret to a file restore the real value locally -- the
    model/relay only ever see <SECRET_n>; tool-call args are de-anonymized before execution.
    """
    if not isinstance(text, str) or not text:
        return text, (mapping or {}), 0
    mapping = mapping if mapping is not None else {}
    rev = {v: k for k, v in mapping.items()}  # original -> placeholder
    counters = {}
    for ph in mapping:
        m = _PH_RE.fullmatch(ph)
        if m:
            counters[m.group(1)] = max(counters.get(m.group(1), 0), int(m.group(2)))

    def _assign(surface, etype):
        ph = rev.get(surface)
        if ph is None:
            counters[etype] = counters.get(etype, 0) + 1
            ph = f"<{etype}_{counters[etype]}>"
            mapping[ph] = surface
            rev[surface] = ph
        return ph

    count = 0
    if pii:
        for start, end, etype, score, surface in sorted(detect(text), key=lambda x: -x[0]):
            text = text[:start] + _assign(surface, etype) + text[end:]
            count += 1
    for rx in SECRET_RES:  # secrets -> reversible <SECRET_n> placeholders (always)
        for surface in {m.group(0) for m in rx.finditer(text)}:
            ph = _assign(surface, "SECRET")
            text = text.replace(surface, ph)
            count += 1
    return text, mapping, count


def deanonymize(text, mapping):
    """Restore originals from placeholders -- used on the model's RESPONSE shown to the user."""
    if not mapping or not isinstance(text, str) or not text:
        return text
    # longest placeholders first so <PERSON_10> isn't partially hit by <PERSON_1>
    for ph in sorted(mapping, key=len, reverse=True):
        text = text.replace(ph, mapping[ph])
    return text


class StreamDeanonymizer:
    """Restore placeholders in a streamed OpenAI SSE response, handling placeholders that are
    split across token deltas/chunks. Content deltas are accumulated, the safe prefix is
    de-anonymized and re-emitted as a content SSE event (reusing the envelope of the last real
    event); a trailing partial placeholder is held until the next chunk. Non-content frames
    (role, finish_reason, usage, [DONE]) flush pending content then pass through unchanged.
    """
    _MAXPH = 28  # longest placeholder we hold for, e.g. <EMAIL_ADDRESS_999>

    def __init__(self, mapping):
        self.map = mapping or {}
        self.raw = ""       # undecoded SSE text not yet split into complete \n\n events
        self.pending = ""   # decoded content awaiting safe emit
        self.env = None     # last content-event envelope dict, reused to re-emit
        self.tc = {}        # tool_calls buffer: {index: {"id","type","name","args"}}
        self.tc_env = None  # tool_call envelope, reused to re-emit
        # Incremental UTF-8 decoder: a multi-byte char split across chunk boundaries is held
        # until complete instead of being dropped by a per-chunk errors='ignore' decode.
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _emit(self, text):
        if self.env is None or not text:
            return b""
        ev = json.loads(json.dumps(self.env))  # deep copy, swap ONLY the content field
        try:
            ev["choices"][0]["delta"]["content"] = text
        except Exception:
            return b""
        return ("data: " + json.dumps(ev) + "\n\n").encode()

    def _safe_split(self, s):
        i = s.rfind("<")
        if i == -1 or ">" in s[i:]:
            return s, ""
        if len(s) - i > self._MAXPH:  # too far back to be a real placeholder start
            return s, ""
        return s[:i], s[i:]

    def _flush_content(self):
        if not self.pending:
            return b""
        out = self._emit(deanonymize(self.pending, self.map))
        self.pending = ""
        return out

    def _flush_tool_calls(self):
        # Emit all buffered tool_calls as one delta, with arguments DE-ANONYMIZED so the engine
        # executes the real value locally (e.g. writes the real secret to a file) -- the model
        # and relay only ever saw <SECRET_n>/<PERSON_n>.
        if not self.tc or self.tc_env is None:
            self.tc = {}
            return b""
        tcs = []
        for i in sorted(self.tc):
            slot = self.tc[i]
            tc = {"index": i}
            if slot.get("id") is not None:
                tc["id"] = slot["id"]
            tc["type"] = slot.get("type", "function")
            nm = slot.get("name", "")
            args = slot.get("args", "")
            tc["function"] = {"name": nm,
                              "arguments": deanonymize(args, self.map) if _is_local_tool(nm) else args}
            tcs.append(tc)
        self.tc = {}
        ev = json.loads(json.dumps(self.tc_env))
        try:
            ev["choices"][0]["delta"] = {"tool_calls": tcs}
        except Exception:
            return b""
        return ("data: " + json.dumps(ev) + "\n\n").encode()

    def _flush(self):
        return self._flush_content() + self._flush_tool_calls()

    def _handle(self, event_text):
        # SSE allows multiple `data:` lines per event that concatenate with newlines; collect
        # them all (taking only the first would drop/misparse multi-line frames and forward the
        # raw event WITHOUT de-anon, leaking a placeholder).
        data_lines = [l.strip()[len("data:"):].lstrip()
                      for l in event_text.splitlines() if l.strip().startswith("data:")]
        if not data_lines:
            return event_text.encode()
        payload = "\n".join(data_lines).strip()
        if not payload or payload == "[DONE]":
            return self._flush() + event_text.encode()
        try:
            ev = json.loads(payload)
            ch = ev.get("choices")
            delta = ch[0].get("delta", {}) if (isinstance(ch, list) and ch) else {}
        except Exception:
            return event_text.encode()
        tool_calls = delta.get("tool_calls")
        content = delta.get("content")
        if isinstance(tool_calls, list):  # buffer tool-call fragments per index; emit on flush
            self.tc_env = ev
            for tcd in tool_calls:
                if not isinstance(tcd, dict):
                    continue
                slot = self.tc.setdefault(tcd.get("index", 0), {"args": ""})
                if tcd.get("id") is not None:
                    slot["id"] = tcd["id"]
                if tcd.get("type") is not None:
                    slot["type"] = tcd["type"]
                fn = tcd.get("function") or {}
                if fn.get("name") is not None:
                    slot["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    slot["args"] += fn["arguments"]
            if isinstance(content, str):  # rare co-occurring content -> buffer it too
                self.env = ev
                self.pending += content
            return b""
        # Pure content delta -> buffer + de-anon the safe prefix. Anything else (finish_reason,
        # role-only, usage) flushes buffers then passes through unchanged.
        if content is not None and not (set(delta.keys()) - {"content", "role"}):
            self.env = ev
            self.pending += content
            emit, self.pending = self._safe_split(self.pending)
            return self._emit(deanonymize(emit, self.map)) if emit else b""
        return self._flush() + event_text.encode()

    def feed(self, plaintext):
        out = bytearray()
        self.raw += self._dec.decode(plaintext) if isinstance(plaintext, bytes) else plaintext
        while "\n\n" in self.raw:
            event, self.raw = self.raw.split("\n\n", 1)
            out += self._handle(event + "\n\n")
        return bytes(out)

    def close(self):
        # Flush any bytes the incremental decoder was still holding, then any buffered state.
        tail = self._dec.decode(b"", final=True)
        if tail:
            self.raw += tail
        out = bytearray(self._flush())
        if self.raw.strip():
            out += self.raw.encode()
        self.raw = ""
        return bytes(out)


def preview(text):
    """Render what the scrubber would redact for a given text: an entity table, the exact
    string the model would see (anonymized), and the local restore map. Powers `ghost --preview`."""
    spans = detect(text)
    anon, mapping, _ = anonymize(text)
    L = []
    L.append(f"\033[1mDetected {len(spans)} PII item(s):\033[0m" if spans else "\033[1mNo PII detected.\033[0m")
    for _s, _e, etype, score, surface in spans:
        L.append(f"  \033[33m{etype:<15}\033[0m {score:.2f}  {surface!r}")
    secret_hits = [m.group(0) for rx in SECRET_RES for m in rx.finditer(text)]
    for s in secret_hits:
        L.append(f"  \033[31m{'SECRET':<15}\033[0m       {s[:8]!r}…  (always scrubbed)")
    L.append("\n\033[1mWhat the model/relay sees (anonymized):\033[0m")
    L.append("  " + anon)
    if mapping:
        L.append("\n\033[1mRestored locally for you (never leaves the box):\033[0m")
        for ph, orig in mapping.items():
            L.append(f"  \033[36m{ph}\033[0m → {orig}")
    return "\n".join(L)


if __name__ == "__main__":
    import sys
    txt = " ".join(sys.argv[1:]) or sys.stdin.read()
    print(preview(txt))
