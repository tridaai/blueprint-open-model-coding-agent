"""Patches: built from two trees, applied to a third with context
verified — a patch that does not match the pristine tree raises."""

from __future__ import annotations

import pytest

from open_model_coding_agent.patch import PatchError, apply_patch, build_patch


def _tree(root, files):
    for rel, content in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)


def test_round_trip(tmp_path):
    pristine, workspace, verify = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _tree(pristine, {"pkg/m.py": "one\ntwo\nthree\n"})
    _tree(workspace, {"pkg/m.py": "one\nTWO\nthree\n"})
    _tree(verify, {"pkg/m.py": "one\ntwo\nthree\n"})
    patch, changed = build_patch(pristine, workspace)
    assert changed == ["pkg/m.py"]
    assert "-two" in patch and "+TWO" in patch
    touched = apply_patch(verify, patch)
    assert touched == ["pkg/m.py"]
    assert (verify / "pkg/m.py").read_text() == "one\nTWO\nthree\n"


def test_no_changes_no_patch(tmp_path):
    _tree(tmp_path / "a", {"m.py": "x\n"})
    _tree(tmp_path / "b", {"m.py": "x\n"})
    patch, changed = build_patch(tmp_path / "a", tmp_path / "b")
    assert patch == "" and changed == []
    assert apply_patch(tmp_path / "a", "") == []


def test_context_mismatch_raises(tmp_path):
    _tree(tmp_path, {"m.py": "alpha\nbeta\n"})
    patch = "--- a/m.py\n+++ b/m.py\n@@ -1,2 +1,2 @@\n alpha\n-gamma\n+delta\n"
    with pytest.raises(PatchError):
        apply_patch(tmp_path, patch)
