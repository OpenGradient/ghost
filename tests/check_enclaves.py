"""Are the enclaves built into ghost (privacy/enclaves.py) still the registry's? Fails when none of them is.

    python tests/check_enclaves.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "privacy"))
import enclaves  # noqa: E402


def main():
    try:
        listed = enclaves.Enclaves().listed()
    except enclaves.RegistryError as e:
        print(f"the registry can't be read, so there is nothing to compare: {e}")
        return 0
    ours, theirs = {t.tee_id for t in enclaves.BUILT_IN}, {t.tee_id for t in listed}
    print(f"built in: {len(ours)}, listed: {len(theirs)}, in both: {len(ours & theirs)}")
    if ours == theirs:
        return 0
    print("\nThe registry's, for privacy/enclaves.py:\n")
    for t in listed:
        print(f'    enclave("{t.endpoint}", """\\\n{t.signing_key_pem}""", "{t.ohttp_public_key.hex()}"),')
    return 0 if ours & theirs or not theirs else 1


if __name__ == "__main__":
    sys.exit(main())
