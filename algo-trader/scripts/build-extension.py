"""Build the optional Cython Algo performance extensions."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from Cython.Build import cythonize
from setuptools import Distribution, Extension
from setuptools.command.build_ext import build_ext

ROOT = Path(__file__).resolve().parents[1]
EXTENSIONS = (
    (
        "options._pricing_cy",
        ROOT / "options/_pricing_cy.pyx",
    ),
    (
        "strategies.multi_factor._features_cy",
        ROOT / "strategies/multi_factor/_features_cy.pyx",
    ),
    (
        "equity_research.backtest._execution_cy",
        ROOT / "equity_research/backtest/_execution_cy.pyx",
    ),
    (
        "equity_research.backtest._signals_cy",
        ROOT / "equity_research/backtest/signals.py",
    ),
    (
        "strategies._swing_common_cy",
        ROOT / "strategies/_swing_common_cy.pyx",
    ),
    (
        "strategies._indicators_cy",
        ROOT / "strategies/_indicators_cy.pyx",
    ),
    (
        "market._bars_cy",
        ROOT / "market/_bars_cy.pyx",
    ),
)


def _copy_extension_atomically(source: Path, destination: Path) -> None:
    """Install a complete extension without truncating a mapped live library."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build() -> None:
    """Compile the extension and copy it into the package tree for Poetry."""
    build_root = ROOT / "build/cython-extension"
    build_lib = build_root / "lib"
    build_temp = build_root / "temp"
    build_lib.mkdir(parents=True, exist_ok=True)
    build_temp.mkdir(parents=True, exist_ok=True)

    extensions = cythonize(
        [Extension(module, [str(source)]) for module, source in EXTENSIONS],
        build_dir=str(build_root / "generated"),
        compiler_directives={"language_level": 3, "annotation_typing": False},
        force=True,
    )
    distribution = Distribution({"name": "algo-trader", "ext_modules": extensions})
    command = build_ext(distribution)
    command.build_lib = str(build_lib)
    command.build_temp = str(build_temp)
    command.ensure_finalized()
    command.run()

    for output_path in build_lib.rglob("*"):
        if not output_path.is_file() or output_path.suffix not in {".so", ".pyd"}:
            continue
        relative_output = output_path.relative_to(build_lib)
        destination = ROOT / relative_output
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_extension_atomically(output_path, destination)


if __name__ == "__main__":
    build()
