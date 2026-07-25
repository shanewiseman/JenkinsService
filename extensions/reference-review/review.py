from __future__ import annotations

import json
import sys
from typing import Any


def review(payload: dict[str, Any]) -> dict[str, Any]:
    inputs = payload.get("inputs", {})
    findings = []
    if inputs.get("changed_files", 0) > 100:
        findings.append(
            {
                "severity": "warning",
                "code": "large-change",
                "message": "Review this large change in smaller logical units where practical.",
            }
        )
    return {"findings": findings, "requested_github_actions": []}


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action != "review":
        raise SystemExit(f"unsupported action: {action}")
    payload = json.load(sys.stdin)
    json.dump(review(payload), sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
