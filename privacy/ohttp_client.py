#!/usr/bin/env python3
"""OHTTP (Oblivious HTTP) client for the OpenGradient chat-api TEE gateway.

This is the Python counterpart of the chat-app browser client
(`lib/api/ohttp.ts`). It lets ghost talk to the *same* hosted-inference path the
website uses, so ghost's non-local models run through OpenGradient's privacy
architecture instead of going to Nous directly:

    ghost engine
      -> local OpenAI bridge (scrubbing_proxy.py, 127.0.0.1:8788)
      -> THIS: full on-chain TEE registry read  +  HPKE/OHTTP encryption
      -> chat-api  /api/v1/chat/ohttp   (Supabase bearer; relay only -- sees
                                          ciphertext + your token, never content)
      -> TEE gateway (decrypts inside the enclave, runs the model, signs output)

Two privacy boundaries, same as the website: the relay (chat-api) sees who you
are but not what you ask; the enclave sees what you ask but not who you are.

What's implemented here, end to end:
  * Full registry reading -- resolves an active LLM-proxy TEE from the on-chain
    TEERegistry, including the complete `ohttpConfig` (HPKE key ids + public key
    + key config) and the TEE's RSA signing key. The SDK's bundled registry read
    historically stopped at endpoint + TLS cert; this reads the whole record.
  * HPKE encapsulation (DHKEM(X25519, HKDF-SHA256) + ChaCha20-Poly1305), the
    OHTTP request/response key schedule, and the chunked-response streaming
    transport -- byte-for-byte compatible with the TEE gateway and the browser.
  * Optional TEE response verification (request/output hash + RSA-PSS signature)
    against the registry signing key.

Crypto deps: `cryptography` (HPKE primitives + RSA-PSS) and `web3` (contract
read + keccak). Both are installed by ghost's install.sh into the privacy venv.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric import padding, x25519
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_der_public_key,
)
from web3 import Web3

logger = logging.getLogger("ghost.ohttp")

# ── OHTTP / HPKE constants (must match lib/api/ohttp.ts and the gateway) ──────
OHTTP_REQUEST_MEDIA_TYPE = "message/ohttp-req"
OHTTP_RESPONSE_MEDIA_TYPE = "message/ohttp-res"
OHTTP_CHUNKED_RESPONSE_MEDIA_TYPE = "message/ohttp-chunked-res"
OHTTP_ENDPOINT = "/api/v1/chat/ohttp"

KEY_CONFIG_ID = 0x01
KEM_ID_X25519 = 0x0020
KDF_ID_HKDF_SHA256 = 0x0001
AEAD_ID_CHACHA20_POLY1305 = 0x0003
NK = 32  # AEAD key length / OHTTP response-nonce length
NN = 12  # AEAD nonce length
NH = 32  # KDF hash output length

LABEL_REQUEST = b"message/bhttp request"
LABEL_RESPONSE = b"message/bhttp response"
LABEL_CHUNKED_RESPONSE = b"message/bhttp chunked response"
LABEL_FINAL = b"final"
HPKE_VERSION = b"HPKE-v1"

# TEE registry type for LLM proxies (matches the SDK's TEE_TYPE_LLM_PROXY).
TEE_TYPE_LLM_PROXY = 0

# Full TEERegistry ABI including the `ohttpConfig` struct and the signing
# `publicKey`. This mirrors the contract the chat-app reads; the SDK's bundled
# ABI omits ohttpConfig, so we carry the complete shape here for full reads.
_TEE_INFO_COMPONENTS = [
    {"name": "owner", "type": "address"},
    {"name": "paymentAddress", "type": "address"},
    {"name": "endpoint", "type": "string"},
    {"name": "publicKey", "type": "bytes"},
    {"name": "tlsCertificate", "type": "bytes"},
    {"name": "pcrHash", "type": "bytes32"},
    {"name": "teeType", "type": "uint8"},
    {"name": "enabled", "type": "bool"},
    {"name": "registeredAt", "type": "uint256"},
    {"name": "lastHeartbeatAt", "type": "uint256"},
    {
        "name": "ohttpConfig",
        "type": "tuple",
        "components": [
            {"name": "keyId", "type": "uint8"},
            {"name": "kemId", "type": "uint16"},
            {"name": "kdfId", "type": "uint16"},
            {"name": "aeadId", "type": "uint16"},
            {"name": "publicKey", "type": "bytes"},
            {"name": "keyConfig", "type": "bytes"},
            {"name": "registeredAt", "type": "uint256"},
        ],
    },
]

TEE_REGISTRY_ABI = [
    {
        "type": "function",
        "name": "getEnabledTEEs",
        "stateMutability": "view",
        "inputs": [{"name": "teeType", "type": "uint8"}],
        "outputs": [{"name": "", "type": "bytes32[]"}],
    },
    {
        "type": "function",
        "name": "getTEE",
        "stateMutability": "view",
        "inputs": [{"name": "teeId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "tuple", "components": _TEE_INFO_COMPONENTS}],
    },
    {
        "type": "function",
        "name": "getActiveTEEs",
        "stateMutability": "view",
        "inputs": [{"name": "teeType", "type": "uint8"}],
        "outputs": [{"name": "", "type": "tuple[]", "components": _TEE_INFO_COMPONENTS}],
    },
]


class OhttpError(RuntimeError):
    """Raised for OHTTP transport / verification failures."""


@dataclass(frozen=True)
class OhttpConfig:
    """A resolved TEE OHTTP configuration (the full registry record we need)."""

    tee_id: str  # 0x-prefixed bytes32 hex, sent as the X-TEE-ID header
    endpoint: str
    host: str
    key_id: int
    kem_id: int
    kdf_id: int
    aead_id: int
    hpke_public_key: bytes  # X25519 recipient public key (32 bytes)
    signing_public_key_der: bytes  # TEE RSA signing key (DER SPKI)

    def validate(self) -> None:
        if (
            self.key_id != KEY_CONFIG_ID
            or self.kem_id != KEM_ID_X25519
            or self.kdf_id != KDF_ID_HKDF_SHA256
            or self.aead_id != AEAD_ID_CHACHA20_POLY1305
        ):
            raise OhttpError("Unsupported TEE OHTTP key configuration")
        if len(self.hpke_public_key) != 32:
            raise OhttpError("TEE HPKE public key is not a 32-byte X25519 key")


# ── Full on-chain registry reading ───────────────────────────────────────────


def read_registry_ohttp_config(
    rpc_url: str,
    registry_address: str,
    tee_type: int = TEE_TYPE_LLM_PROXY,
    app_env: str = "production",
) -> OhttpConfig:
    """Resolve an active TEE and its full OHTTP config from the on-chain registry.

    Mirrors the chat-app's selection logic: in development it lists enabled TEE
    ids and fetches one with getTEE; otherwise it uses getActiveTEEs (which the
    contract filters by heartbeat/PCR freshness) and derives the teeId from the
    signing public key.

    Args:
        rpc_url: JSON-RPC endpoint for the chain hosting the registry.
        registry_address: Deployed TEERegistry contract address.
        tee_type: TEE type to resolve (0 = LLM proxy).
        app_env: "development" uses getEnabledTEEs + getTEE; anything else uses
            getActiveTEEs.

    Returns:
        A validated OhttpConfig for a randomly selected active TEE.
    """
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(registry_address),
        abi=TEE_REGISTRY_ABI,
    )

    if app_env == "development":
        tee_ids = contract.functions.getEnabledTEEs(tee_type).call()
        if not tee_ids:
            raise OhttpError("TEE registry has no enabled OHTTP gateways")
        tee_id_bytes = random.choice(tee_ids)
        raw = contract.functions.getTEE(tee_id_bytes).call()
        tee_id = "0x" + bytes(tee_id_bytes).hex()
        return _config_from_tee_struct(raw, tee_id)

    tees = contract.functions.getActiveTEEs(tee_type).call()
    if not tees:
        raise OhttpError("TEE registry has no active OHTTP-enabled gateways")
    raw = random.choice(tees)
    signing_key_der = bytes(raw[3])
    tee_id = "0x" + Web3.keccak(signing_key_der).hex().removeprefix("0x")
    return _config_from_tee_struct(raw, tee_id)


def _config_from_tee_struct(raw: Any, tee_id: str) -> OhttpConfig:
    """Build an OhttpConfig from a decoded TEEInfo tuple (positional)."""
    endpoint = raw[2]
    signing_public_key_der = bytes(raw[3])
    ohttp = raw[10]  # ohttpConfig tuple
    cfg = OhttpConfig(
        tee_id=tee_id,
        endpoint=endpoint,
        host=_host_from_endpoint(endpoint),
        key_id=int(ohttp[0]),
        kem_id=int(ohttp[1]),
        kdf_id=int(ohttp[2]),
        aead_id=int(ohttp[3]),
        hpke_public_key=bytes(ohttp[4]),
        signing_public_key_der=signing_public_key_der,
    )
    cfg.validate()
    return cfg


def _host_from_endpoint(endpoint: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(endpoint).netloc or endpoint
    except Exception:
        return endpoint


# ── HPKE primitives (RFC 9180 base mode, DHKEM(X25519)/HKDF-SHA256/ChaCha20) ──


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    out = b""
    t = b""
    counter = 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        out += t
        counter += 1
    return out[:length]


def _suite_id() -> bytes:
    return (
        b"HPKE"
        + KEM_ID_X25519.to_bytes(2, "big")
        + KDF_ID_HKDF_SHA256.to_bytes(2, "big")
        + AEAD_ID_CHACHA20_POLY1305.to_bytes(2, "big")
    )


def _kem_suite_id() -> bytes:
    return b"KEM" + KEM_ID_X25519.to_bytes(2, "big")


def _labeled_extract(suite: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return _hkdf_extract(salt, HPKE_VERSION + suite + label + ikm)


def _labeled_expand(suite: bytes, prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
    labeled_info = length.to_bytes(2, "big") + HPKE_VERSION + suite + label + info
    return _hkdf_expand(prk, labeled_info, length)


def _header_bytes() -> bytes:
    return (
        bytes([KEY_CONFIG_ID])
        + KEM_ID_X25519.to_bytes(2, "big")
        + KDF_ID_HKDF_SHA256.to_bytes(2, "big")
        + AEAD_ID_CHACHA20_POLY1305.to_bytes(2, "big")
    )


def _extract_and_expand(dh: bytes, kem_context: bytes) -> bytes:
    kem_suite = _kem_suite_id()
    eae_prk = _labeled_extract(kem_suite, b"", b"eae_prk", dh)
    return _labeled_expand(kem_suite, eae_prk, b"shared_secret", kem_context, NH)


def _key_schedule(shared_secret: bytes, info: bytes) -> Dict[str, bytes]:
    suite = _suite_id()
    psk_id_hash = _labeled_extract(suite, b"", b"psk_id_hash", b"")
    info_hash = _labeled_extract(suite, b"", b"info_hash", info)
    ksc = bytes([0]) + psk_id_hash + info_hash  # mode_base
    secret = _labeled_extract(suite, shared_secret, b"secret", b"")
    return {
        "key": _labeled_expand(suite, secret, b"key", ksc, NK),
        "base_nonce": _labeled_expand(suite, secret, b"base_nonce", ksc, NN),
        "exporter_secret": _labeled_expand(suite, secret, b"exp", ksc, NH),
    }


@dataclass
class _Encapsulated:
    wire: bytes
    enc: bytes
    response_secret: bytes
    chunked_response_secret: bytes


def _encapsulate_request(recipient_public_key: bytes, plaintext: bytes) -> _Encapsulated:
    sk_e = x25519.X25519PrivateKey.generate()
    enc = sk_e.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    peer = x25519.X25519PublicKey.from_public_bytes(recipient_public_key)
    dh = sk_e.exchange(peer)
    kem_context = enc + recipient_public_key
    shared_secret = _extract_and_expand(dh, kem_context)

    header = _header_bytes()
    info = LABEL_REQUEST + bytes([0]) + header
    ctx = _key_schedule(shared_secret, info)

    ciphertext = ChaCha20Poly1305(ctx["key"]).encrypt(ctx["base_nonce"], plaintext, b"")
    wire = header + enc + ciphertext

    suite = _suite_id()
    response_secret = _labeled_expand(suite, ctx["exporter_secret"], b"sec", LABEL_RESPONSE, NK)
    chunked_response_secret = _labeled_expand(
        suite, ctx["exporter_secret"], b"sec", LABEL_CHUNKED_RESPONSE, NK
    )
    return _Encapsulated(wire, enc, response_secret, chunked_response_secret)


def _derive_response_keys(response_secret: bytes, enc: bytes, response_nonce: bytes) -> Tuple[bytes, bytes]:
    salt = enc + response_nonce
    prk = _hkdf_extract(salt, response_secret)
    key = _hkdf_expand(prk, b"key", NK)
    nonce = _hkdf_expand(prk, b"nonce", NN)
    return key, nonce


def _decrypt_response(response_secret: bytes, enc: bytes, sealed: bytes) -> Dict[str, Any]:
    if len(sealed) <= NK:
        raise OhttpError("Malformed OHTTP response")
    response_nonce = sealed[:NK]
    ciphertext = sealed[NK:]
    key, nonce = _derive_response_keys(response_secret, enc, response_nonce)
    plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, b"")
    return _normalize_inner_response(json.loads(plaintext.decode("utf-8")))


def _normalize_inner_response(decoded: Any) -> Dict[str, Any]:
    if not isinstance(decoded, dict):
        raise OhttpError("Malformed OHTTP response")
    if isinstance(decoded.get("status"), int) and isinstance(decoded.get("body"), dict):
        return {"status": decoded["status"], "body": decoded["body"]}
    return {"status": 200, "body": decoded}


class ChunkedResponseDecrypter:
    """Streaming decrypter for `message/ohttp-chunked-res` bodies."""

    def __init__(self, response_secret: bytes, enc: bytes):
        self._response_secret = response_secret
        self._enc = enc
        self._buffer = b""
        self._key: Optional[bytes] = None
        self._base_nonce: Optional[bytes] = None
        self._counter = 0
        self._saw_final = False

    def push(self, chunk: Optional[bytes], done: bool) -> List[bytes]:
        if chunk:
            self._buffer += chunk

        if self._key is None or self._base_nonce is None:
            if len(self._buffer) < NK:
                if done:
                    raise OhttpError("Malformed chunked OHTTP response")
                return []
            response_nonce = self._buffer[:NK]
            self._key, self._base_nonce = _derive_response_keys(
                self._response_secret, self._enc, response_nonce
            )
            self._buffer = self._buffer[NK:]

        out: List[bytes] = []
        while self._buffer:
            frame = _decode_varint(self._buffer, 0)
            if frame is None:
                if done:
                    raise OhttpError("Malformed chunked OHTTP response")
                break
            sealed_length, offset = frame
            if sealed_length == 0:
                if not done:
                    break
                out.append(self._decrypt_chunk(self._buffer[offset:], is_final=True))
                self._buffer = b""
                self._saw_final = True
                break
            if len(self._buffer) < offset + sealed_length:
                if done:
                    raise OhttpError("Truncated chunked OHTTP response")
                break
            ciphertext = self._buffer[offset : offset + sealed_length]
            out.append(self._decrypt_chunk(ciphertext, is_final=False))
            self._buffer = self._buffer[offset + sealed_length :]

        if done and not self._saw_final:
            raise OhttpError("Chunked OHTTP response missing final marker")
        return out

    def _decrypt_chunk(self, ciphertext: bytes, is_final: bool) -> bytes:
        assert self._key is not None and self._base_nonce is not None
        chunk_nonce = _xor_bytes(self._base_nonce, self._counter.to_bytes(NN, "big"))
        aad = LABEL_FINAL if is_final else b""
        plaintext = ChaCha20Poly1305(self._key).decrypt(chunk_nonce, ciphertext, aad)
        self._counter += 1
        return plaintext


def _decode_varint(data: bytes, offset: int) -> Optional[Tuple[int, int]]:
    if offset >= len(data):
        return None
    first = data[offset]
    length = 1 << (first >> 6)
    if offset + length > len(data):
        return None
    value = first & 0x3F
    for i in range(1, length):
        value = (value << 8) | data[offset + i]
    return value, offset + length


def _xor_bytes(left: bytes, right: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(left, right))


# ── TEE response verification (request/output hash + RSA-PSS signature) ───────


def _keccac_hex(data: bytes) -> str:
    return Web3.keccak(data).hex().removeprefix("0x")


def _canonical_json(value: Any) -> str:
    # Matches the gateway's Python json.dumps(sort_keys=True): comma+space and
    # colon+space separators, ASCII-escaped, recursively key-sorted.
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(", ", ": "))


def _canonical_user_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                out.append({"type": "text", "text": part.get("text", "")})
            elif isinstance(part, dict):
                entry: Dict[str, Any] = {"type": part.get("type")}
                if part.get("type") == "file":
                    filename = (part.get("file") or {}).get("filename")
                    if filename:
                        entry["filename"] = filename
                out.append(entry)
        return out
    return content


def _canonicalize_request_for_hash(inner: Dict[str, Any]) -> Dict[str, Any]:
    messages = []
    for m in inner.get("messages", []):
        if isinstance(m, dict) and m.get("role") == "user":
            messages.append({**m, "content": _canonical_user_content(m.get("content"))})
        else:
            messages.append(m)
    return {**inner, "messages": messages}


def _extract_assistant_content(body: Dict[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return content if isinstance(content, str) else ""


def _response_content_for_hash(body: Dict[str, Any]) -> str:
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    if choice.get("finish_reason") == "tool_calls" and isinstance(message.get("tool_calls"), list):
        return _canonical_json(message["tool_calls"])
    return _extract_assistant_content(body)


def verify_tee_response(
    inner_request: Dict[str, Any],
    response_body: Dict[str, Any],
    signing_public_key_der: bytes,
    response_content: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify the TEE request/output hashes and RSA-PSS signature.

    Returns a dict describing the verification outcome. Raises OhttpError on a
    concrete mismatch (tampering) when all signed fields are present.
    """
    request_hash = _keccac_hex(_canonical_json(_canonicalize_request_for_hash(inner_request)).encode("utf-8"))
    tee_request_hash = response_body.get("tee_request_hash")
    if isinstance(tee_request_hash, str) and tee_request_hash != request_hash:
        raise OhttpError("TEE request hash verification failed")

    output_content = response_content if response_content is not None else _response_content_for_hash(response_body)
    output_hash = _keccac_hex(output_content.encode("utf-8"))
    tee_output_hash = response_body.get("tee_output_hash")
    if isinstance(tee_output_hash, str) and tee_output_hash != output_hash:
        raise OhttpError("TEE output hash verification failed")

    signature_b64 = response_body.get("tee_signature")
    timestamp = response_body.get("tee_timestamp")
    if not isinstance(signature_b64, str) or timestamp is None:
        return {"verified": False, "reason": "unsigned"}

    import base64

    ts_bytes = int(timestamp).to_bytes(32, "big")
    msg_hash = Web3.keccak(bytes.fromhex(request_hash) + bytes.fromhex(output_hash) + ts_bytes)
    pub = load_der_public_key(signing_public_key_der)
    if not isinstance(pub, RSAPublicKey):
        raise OhttpError("TEE signing key is not RSA")
    # The signed message is keccak256(request_hash || output_hash || timestamp)
    # -- already a 32-byte digest, but RSA-PSS hashes it again with SHA-256,
    # matching the browser's crypto.subtle.verify({name:"RSA-PSS"}, ..., msgHash).
    pub.verify(
        base64.b64decode(signature_b64),
        bytes(msg_hash),
        padding.PSS(mgf=padding.MGF1(SHA256()), salt_length=32),
        SHA256(),
    )
    return {
        "verified": True,
        "request_hash": request_hash,
        "output_hash": output_hash,
        "timestamp": str(timestamp),
    }


def signing_key_pem(der: bytes) -> str:
    """DER SPKI -> PEM (handy for logging / debugging the registry signing key)."""
    pub = load_der_public_key(der)
    return pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()


# ── Public client ─────────────────────────────────────────────────────────────


def encapsulate(config: OhttpConfig, inner_request: Dict[str, Any]) -> _Encapsulated:
    """HPKE-encapsulate an inner OpenAI request for the given TEE config."""
    plaintext = json.dumps(inner_request).encode("utf-8")
    return _encapsulate_request(config.hpke_public_key, plaintext)


def decrypt_single(enc: _Encapsulated, sealed: bytes) -> Dict[str, Any]:
    """Decrypt a non-streaming `message/ohttp-res` response body."""
    return _decrypt_response(enc.response_secret, enc.enc, sealed)
