"""Which enclaves a request may be sealed to: the ones the on-chain TEE registry lists.

The chain it lives on has been down (25 November 2026), and a network that inspects TLS can't be believed about
it. When it can't be read, ghost uses the enclaves below instead. Their keys are fixed here, as the registry's
address is, so cutting ghost off from the registry can't point it at an impostor. DreamFort's apps fall back to
the same list (chat-app's lib/api/backupTees.ts, chat-api's src/core/backup_tees.py): keep the three in step.
"""
import random

from cryptography.hazmat.primitives import serialization
from eth_hash.auto import keccak
from opengradient_ohttp import DEFAULT_RPC_URL as REGISTRY, RegistryError, TeeTarget
from opengradient_ohttp._registry import TeeResolver, parse_active_tees


def _enclave(endpoint, signing_key_pem, hpke_public_key):
    der = serialization.load_pem_public_key(signing_key_pem.encode()).public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return TeeTarget(tee_id="0x" + keccak(der).hex(), endpoint=endpoint, signing_key_pem=signing_key_pem,
                     ohttp_public_key=bytes.fromhex(hpke_public_key), key_id=1)


BUILT_IN = (
    _enclave("https://3.147.79.53", """\
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxTFaCi3JXAOp1LxpjZxT
KZPGMGNzjtRIaC73KnMZOIm/rJoM6x+kqLrocQKU2ATfGi7tuiB06AirJaM31CiQ
yBBc9/qCkQj6i+zudQZCFkPIDgn34hpgL6nc2jXJAlhV7zHdpg2ieSjtBh8D+tSH
MYmrg32WY4SGxPiRPo8FnYITkOl8ZWjDi85Yi21ztWgY70Ck5j+UfzNa96ceObR/
YGxPGLwMuon/N1LmZ2q+H6sPbHI6Sxeb5b33Kpe4wRwqkUkRw49Do/1iwY5pPJIc
OzkyDBXXQXl9EOZAhP5ug423H2dcwobkq3QEV+fW2EIHouDNR1lei0RwNCPlWPnj
LwIDAQAB
-----END PUBLIC KEY-----
""", "ba5c847114ffc09e387b2b9946b1ab8e91528a308137fdb160f59ea9e1717112"),
)


class Enclaves(TeeResolver):
    """Picks one, and keeps it for as long as a registry read is kept."""

    def __init__(self, transport=None, log=lambda message: None):
        super().__init__(transport=transport)
        self._log = log

    def get(self):
        try:
            return super().get()
        except RegistryError as e:
            self._log(f"the TEE registry can't be read, using a built-in enclave: {e}")
            with self._lock:
                self._cache.put(random.choice(BUILT_IN))
                return self._cache.get()

    def listed(self):
        """How many enclaves the registry lists. Raises RegistryError when it can't be read."""
        found = len(parse_active_tees(self._fetch(), self._excluded))
        if not found:
            raise RegistryError("the registry lists no enclave")
        return found
