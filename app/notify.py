from __future__ import annotations

import json
import logging
import subprocess
import sys

log = logging.getLogger(__name__)


def notify(title: str, message: str, sound: bool = False) -> None:
    if sys.platform != "darwin":
        return
    script = f"display notification {json.dumps(message[:220])} with title {json.dumps(title[:80])}"
    if sound:
        script += ' sound name "Glass"'
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        log.debug("notification failed: %s", exc)
