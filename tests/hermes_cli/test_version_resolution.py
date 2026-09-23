"""The shipped version comes from a generated module, never from the tree.

A checkout carries no version of its own: the release stamps ``hermes_cli/_version.py``
into the build tree, and a tree without that file reports the placeholder.
"""
import subprocess
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _version_of(tree: Path) -> str:
    env = {"PYTHONPATH": str(tree)}
    script = "import hermes_cli; print(hermes_cli.__version__)"
    out = subprocess.run([sys.executable, "-c", script], cwd=tree, env=env,
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def test_generated_module_wins(tmp_path):
    cli = tmp_path / "hermes_cli"
    shutil.copytree(REPO / "hermes_cli", cli,
                    ignore=shutil.ignore_patterns("__pycache__", "_version.py"))
    (cli / "_version.py").write_text('__version__ = "0.21.5"\n')

    assert _version_of(tmp_path) == "0.21.5"


def test_absent_generated_module_is_the_placeholder(tmp_path):
    cli = tmp_path / "hermes_cli"
    shutil.copytree(REPO / "hermes_cli", cli,
                    ignore=shutil.ignore_patterns("__pycache__", "_version.py"))

    assert _version_of(tmp_path) == "0.0.0"
