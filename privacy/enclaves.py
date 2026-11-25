"""Which enclaves a request may be sealed to: the ones the on-chain TEE registry lists.

The chain it lives on has been down (25 November 2026), and a network that inspects TLS can't be believed about
it. When it can't be read, ghost uses the enclaves it listed the last time, and the ones below; a registry that
answers is always obeyed, also when it lists none. Both are fixed on this machine, as the registry's address is, so
cutting ghost off from the registry can't point it at an impostor. An enclave gets new keys whenever it is deployed,
so those below go stale: CI says when (tests/check_enclaves.py), and prints what to put here.
"""
import json
import os
import random
import threading
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from eth_hash.auto import keccak
from opengradient_ohttp import DEFAULT_RPC_URL as REGISTRY, RegistryError, TeeTarget
from opengradient_ohttp._registry import TeeResolver, parse_active_tees

REMEMBERED = Path(os.environ.get("GHOST_HOME") or Path.home() / ".ghost") / "privacy" / "enclaves.json"
KEEP = 14 * 86400
AVOID = 600.0   # seconds an enclave that just failed is left alone, while there is another


def enclave(endpoint, signing_key_pem, hpke_public_key):
    der = serialization.load_pem_public_key(signing_key_pem.encode()).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return TeeTarget(tee_id="0x" + keccak(der).hex(), endpoint=endpoint, signing_key_pem=signing_key_pem,
                     ohttp_public_key=bytes.fromhex(hpke_public_key), key_id=1)


# As the registry listed them on 25 November 2026, each attested by AWS Nitro.
BUILT_IN = (
    enclave("https://3.15.214.21", """\
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0R5wLaOc7+mNhbvq6Dng
rmn3guc4Ay70cOhOKU32ZiJDnRC5TZH2SOvGKlX/J/Xm7jCA5jE3KsMd0Z1QszUQ
8ZIc37OrXco2POvP4MWsG5aqSxJFVW8UPqVfzQsO0a+BPO3tp5hoREPxdcx3T34M
b3LGByKey7SBQEq1V6Eh/1q/zE69/+oQN7QcTr3peC9GM95RfqPV+koC74DrMFgc
TaP39hI7dZBAAG8R1hdau2EQSujwgaAhlkpnhncDhBS+ZTG2ArU8FfE6oT+qGlvE
lJ17ioC1ps1+RDwLx9yUbZXTtksbu/fX6+kCghyFacf4Ps/0FMSo4TRpFM62IMci
3QIDAQAB
-----END PUBLIC KEY-----
""", "c16340240ac449452a874eb46c3e20e50afc7a7a32f3014465f664bb00464d20"),
    enclave("https://13.59.207.188", """\
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA6exo/OGkZaGjciAS1W/F
Sy7gsxBclp9A/wQaL3TSmTTvORpgxh2dXt2XmcGJeo5TfTJdM2KZOgD6rv47EqI/
2IU9XKJpQ/WHujKkr8CRTlCnoJgqUqmD4KuQlvWB0GEFXU4VioVbAazIlpWFeYhb
N4kDJKwPYOm8vcAhtQpZ+8Hlp1RUyAG2DoLajJsCOBHLTcbIG6WiVt9TPE/1chLN
zrfOB0K2qJ9/IzZE09LMDzlqEzrN6rsccSaaQH10f14FFO2rvC521Q+kiBBCHpSc
44oj44pafjUxBFuEPsgFX1w9i2NtLvTy9gjGpJFWL82uUIud+EMztqIq/YZ6BUKh
xwIDAQAB
-----END PUBLIC KEY-----
""", "592b5bc353f6114ef947771f1e0dd36fc21ef8f5279fe841369161a551697577"),
    enclave("https://3.147.79.53", """\
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArkOOrPjjE+3RKnYRuisw
a+E6eY9d8A7/HCQ99gd3sobXx/6jVJ7e79CP5RsUc7jC5q6T2Nph3YiuyCjW4gBt
vHcTH5n3kZjRhPbHiAY93a79a3X1tRxH7A9NPvnSVBu7YRC66wA1GidRHDhfa4CP
gVUGCHTGX3zGfd/EZ5PeafODkgZtTo6y/37N6atwsBTNnDZsW252ea8YmZk0Y/E/
KGW4VCYC53vleRJIM9IXRnbIlg5UvXNcyijWrV3hdwjDzTXk0qGz/bZ0MngoLRhf
wMcYIRxmp6tva63zdVqgGb9GsfhFnijd7RtEKoLep1nP1apyftK1DhaItyI+gjYG
6QIDAQAB
-----END PUBLIC KEY-----
""", "127ec8c4a543d98450d37e4c0a164e80d71d4f305e21e380b1ec07b919a0b563"),
)


def remember(found):
    row = [[t.endpoint, t.signing_key_pem, t.ohttp_public_key.hex()] for t in found if t.key_id == 1]
    try:
        fd = os.open(REMEMBERED.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"at": time.time(), "enclaves": row}, fh)
        os.replace(REMEMBERED.with_suffix(".tmp"), REMEMBERED)
    except OSError:
        pass


def remembered():
    try:
        data = json.loads(REMEMBERED.read_text(encoding="utf-8"))
        return [enclave(*row) for row in data["enclaves"]] if time.time() - data["at"] < KEEP else []
    except (OSError, ValueError, KeyError, TypeError):
        return []


class Enclaves(TeeResolver):
    """Picks one, and keeps it for as long as a registry read is kept."""

    def __init__(self, transport=None, log=lambda message: None):
        super().__init__(transport=transport)
        self._log, self._avoid, self._guard = log, {}, threading.Lock()

    def get(self):
        with self._lock:
            if not self._cache.get():
                try:
                    found = self.listed()
                    remember(found)
                except RegistryError as e:
                    known = {t.tee_id: t for t in (*BUILT_IN, *remembered())}
                    found = list(known.values())
                    self._log(f"the TEE registry can't be read, using one of {len(found)} enclaves known from before: {e}")
                if not found:
                    raise RegistryError("the TEE registry lists no active enclave, so there is nothing to seal this to; try again later")
                with self._guard:
                    fresh = [t for t in found if time.monotonic() - self._avoid.get(t.tee_id, -AVOID) >= AVOID]
                self._cache.put(random.choice(fresh or found))
            return self._cache.get()

    def invalidate(self, tee_id=None):
        """The relay said it's gone, or unwell."""
        if tee_id:
            with self._guard:
                self._avoid[tee_id] = time.monotonic()
        super().invalidate(tee_id)

    def listed(self):
        """The enclaves the registry lists. Raises RegistryError when it can't be read."""
        return parse_active_tees(self._fetch(), self._excluded)
