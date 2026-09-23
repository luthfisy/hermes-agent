"""A fatal stage under -Stage -Json yields exactly one failure frame carrying the reason."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.platforms("windows")
INSTALLER = Path(__file__).resolve().parents[3] / "scripts" / "install.ps1"


def test_fail_inside_a_stage_emits_one_json_frame_with_the_reason(tmp_path):
    powershell = shutil.which("powershell")
    assert powershell
    # A clone from a path that does not exist makes the repository stage hit
    # the installer's own Fail helper (not a thrown exception).
    env = dict(os.environ, HERMES_REPO_URL=str(tmp_path / "no-such-repo"),
               HERMES_INSTALL_DIR=str(tmp_path / "install"), HERMES_HOME=str(tmp_path / "home"))
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALLER), "-Stage", "repository", "-Json"],
        env=env, capture_output=True, text=True, timeout=120)
    frames = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert result.returncode == 1
    assert len(frames) == 1, result.stdout
    assert frames[0]["ok"] is False and frames[0]["stage"] == "repository"
    assert "git clone failed" in frames[0]["reason"]
