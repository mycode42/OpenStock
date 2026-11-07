from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-extension.py"
SPEC = spec_from_file_location("build_extension", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BUILD_EXTENSION = module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD_EXTENSION)


def test_extension_copy_replaces_destination_only_after_copy_is_complete(tmp_path, monkeypatch):
    source = tmp_path / "built.so"
    destination = tmp_path / "installed.so"
    source.write_bytes(b"new extension bytes")
    destination.write_bytes(b"old mapped extension")
    original_replace = BUILD_EXTENSION.os.replace
    observations = []

    def verify_then_replace(temporary, target):
        observations.append((Path(target).read_bytes(), Path(temporary).read_bytes()))
        original_replace(temporary, target)

    monkeypatch.setattr(BUILD_EXTENSION.os, "replace", verify_then_replace)
    BUILD_EXTENSION._copy_extension_atomically(source, destination)

    assert observations == [(b"old mapped extension", b"new extension bytes")]
    assert destination.read_bytes() == b"new extension bytes"
    assert not list(tmp_path.glob(".installed.so.*.tmp"))
