"""Synthetic DPAPI and single-instance checks, run with the native Windows interpreter."""

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from doraymon.desktop.bridge import protect_token  # noqa: E402
from doraymon.desktop.windows import DesktopInstance  # noqa: E402


def main():
    if os.name != "nt":
        raise SystemExit("Use the Windows interpreter")
    value = b"synthetic-desktop-token-not-a-real-credential"
    encrypted = protect_token(value)
    assert value != encrypted
    assert protect_token(encrypted, decrypt=True) == value
    name = "synthetic-desktop-instance-" + str(uuid4())
    first, second = DesktopInstance(name), DesktopInstance(name)
    assert not first.existing and second.existing
    second.close()
    first.close()
    final = DesktopInstance(name)
    assert not final.existing
    final.close()
    print(json.dumps({"dpapi_roundtrip": True, "single_instance": True}))


if __name__ == "__main__":
    main()
