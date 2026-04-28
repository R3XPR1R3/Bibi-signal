"""Read/write .env preserving comments and unrelated lines.

We don't use python-dotenv's mutators because they re-flow the file. The
TUI flow is: load → modify a few keys → write back, comments intact.
"""
from __future__ import annotations

from pathlib import Path


def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip("'").strip('"')
    return out


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Update existing keys in place; append any new ones at the end.

    Preserves comments and blank lines.
    """
    lines: list[str] = []
    seen_keys: set[str] = set()

    if path.exists():
        original = path.read_text().splitlines()
    else:
        original = []

    for raw in original:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(raw)
            continue
        k = stripped.split("=", 1)[0].strip()
        if k in updates:
            lines.append(f"{k}={updates[k]}")
            seen_keys.add(k)
        else:
            lines.append(raw)

    # Append any new keys we didn't see.
    new_keys = [k for k in updates if k not in seen_keys]
    if new_keys:
        if lines and lines[-1].strip():
            lines.append("")
        for k in new_keys:
            lines.append(f"{k}={updates[k]}")

    path.write_text("\n".join(lines) + "\n")
