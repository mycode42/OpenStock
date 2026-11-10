import os
from pathlib import Path

from options.research.dataset import prune_stale_chain_caches


def _touch(path: Path, mtime: float) -> Path:
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


def test_prune_keeps_only_the_new_file_for_its_underlying(tmp_path):
    old_spy = _touch(tmp_path / "SPY-aaaa.pkl", 1_000)
    new_spy = _touch(tmp_path / "SPY-bbbb.pkl", 2_000)
    brk = _touch(tmp_path / "BRK.B-cccc.pkl", 1_000)
    spy_like = _touch(tmp_path / "SPYX-dddd.pkl", 1_000)

    removed = prune_stale_chain_caches(tmp_path, new_spy)

    assert removed == 1
    assert not old_spy.exists()
    assert new_spy.exists()
    assert brk.exists(), "other underlyings are not touched"
    assert spy_like.exists(), "a ticker whose name starts with the same letters is not touched"
