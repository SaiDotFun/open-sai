"""Release source compiled only into the loopback rehearsal wheel."""

import os
import re


def release_source() -> tuple[str, str]:
    fixture = os.environ.get("SAI_NATIVE_RELEASE_BASE", "")
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", fixture)
    if match is None or int(match[1]) > 65535 or not os.environ.get("SAI_HOME"):
        raise RuntimeError(
            "SAI_NATIVE_RELEASE_BASE requires a loopback origin and an explicit SAI_HOME"
        )
    return fixture, f"shim-loopback-{match[1]}"
