"""A lab test run applies its chosen parameters for its own process only."""

import json
from pathlib import Path

from strategies import registry


def test_lab_override_changes_the_effective_values_for_one_process_only(monkeypatch, tmp_path):
    strategy = "swing_momentum"
    registry.refresh_active_parameter_profile()
    live = dict(registry.ALGO_DEFAULT_PARAMETERS[strategy])
    overrides = tmp_path / "overrides.json"
    overrides.write_text(json.dumps({strategy: {"max_position_pct": round(live["max_position_pct"] * 0.5, 4)}}))
    monkeypatch.setenv("ALGO_LAB_PARAMETER_OVERRIDES", str(overrides))
    registry.refresh_active_parameter_profile()
    assert registry.ALGO_DEFAULT_PARAMETERS[strategy]["max_position_pct"] == round(live["max_position_pct"] * 0.5, 4)
    monkeypatch.delenv("ALGO_LAB_PARAMETER_OVERRIDES")
    registry.refresh_active_parameter_profile()
    assert registry.ALGO_DEFAULT_PARAMETERS[strategy]["max_position_pct"] == live["max_position_pct"]


def test_lab_check_script_reports_out_of_range_values_only():
    import subprocess
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "lab_check_profile.py"

    def check(payload):
        result = subprocess.run([sys.executable, str(script)], input=json.dumps(payload), capture_output=True, text=True, check=True)
        return json.loads(result.stdout)["errors"]

    assert check({"swing_momentum": {"max_position_pct": 0.04}}) == {}
    assert "swing_momentum" in check({"swing_momentum": {"max_position_pct": 0.5}})
