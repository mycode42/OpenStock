"""Render ops/systemd unit templates into the systemd user directory.

The units run as the invoking user under the user manager, so no root is
needed. This only writes the files. Starting them is a separate step
(``systemctl --user enable --now equity options``), so setup never
starts live trading.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "ops" / "systemd"
UNITS = ("equity.service", "options.service")


def user_unit_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user"


def render(text: str, root: Path, env_file: Path, python: Path) -> str:
    return (text.replace("{ROOT}", str(root))
                .replace("{ENV_FILE}", str(env_file))
                .replace("{PYTHON}", str(python)))


def install(target: Path | None = None, python: str | None = None) -> list[Path]:
    """Write rendered units to ``target`` and return the written paths."""
    target = target or user_unit_dir()
    python_path = Path(python or sys.executable)
    env_file = ROOT.parent / ".env"
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name in UNITS:
        out = target / name
        out.write_text(render((TEMPLATES / name).read_text(encoding="utf-8"), ROOT, env_file, python_path),
                       encoding="utf-8")
        written.append(out)
    return written


def main() -> int:
    written = install()
    for path in written:
        print(f"wrote {path}")
    env_file = ROOT.parent / ".env"
    if not env_file.exists():
        print(f"warning: {env_file} is missing; the units will not start until it exists", file=sys.stderr)
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("start with: systemctl --user enable --now equity options")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
