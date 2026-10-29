"""User-scope unit templates render to runnable units with no root settings."""

import sys
from pathlib import Path

from scripts.install_units import ROOT, UNITS, install


def test_rendered_units_have_no_placeholders_or_root_settings(tmp_path):
    written = install(target=tmp_path, python="/venv/bin/python")
    assert {path.name for path in written} == set(UNITS)
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert "{ROOT}" not in text and "{ENV_FILE}" not in text and "{PYTHON}" not in text
        assert "User=" not in text and "Group=" not in text
        assert "WantedBy=default.target" in text
        assert f"WorkingDirectory={ROOT}" in text
        assert f"EnvironmentFile={ROOT.parent / '.env'}" in text
        exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        assert exec_line.startswith("ExecStart=/venv/bin/python -m ")


def test_default_python_is_the_running_interpreter(tmp_path):
    text = install(target=tmp_path)[0].read_text(encoding="utf-8")
    assert f"ExecStart={sys.executable} " in text
