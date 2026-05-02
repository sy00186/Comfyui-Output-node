"""仓库内单源：期望的 vates_core 语义化版本（与 pyproject / Cargo 发布版本一致）。"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def expected_vates_core_version() -> str:
    path = _ROOT / "pyproject.toml"
    if not path.is_file():
        return "0.1.1"
    in_project = False
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s == "[project]":
            in_project = True
            continue
        if in_project and s.startswith("[") and s.endswith("]"):
            break
        if in_project and s.startswith("version = "):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.1.1"
