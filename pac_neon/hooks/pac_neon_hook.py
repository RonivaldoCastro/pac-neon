#!/usr/bin/env python3
"""
Non-blocking telemetry hook for Pac-Neon.
IMPORTANT: This hook MUST NEVER block, delay, or deny agent execution in any terminal or IDE.
It forwards the event asynchronously in the background and exits 0 immediately.
"""
from __future__ import annotations

import os
import subprocess
import sys

PAC_NEON_URL = os.environ.get("PAC_NEON_URL", "http://127.0.0.1:8765")


def main() -> int:
    try:
        raw_stdin = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
        if raw_stdin:
            subprocess.Popen(
                [
                    "curl",
                    "-sX",
                    "POST",
                    f"{PAC_NEON_URL}/api/hooks/agent-event",
                    "--connect-timeout",
                    "0.3",
                    "--max-time",
                    "0.6",
                    "-H",
                    "Content-Type: application/json",
                    "-d",
                    raw_stdin,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(0 if main() is None else 0)
