"""Tests for TUI gateway slash_worker profile_home propagation (#40677)."""

from unittest.mock import MagicMock, patch


def test_slash_worker_accepts_profile_home(tmp_path, monkeypatch):
    """_SlashWorker.__init__ accepts profile_home and pins the child's HERMES_HOME to it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_test"))
    from tui_gateway.server import _SlashWorker

    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value.stdout = MagicMock()
        mock_popen.return_value.stderr = MagicMock()

        _SlashWorker(
            session_key="test_key",
            model="test-model",
            profile_home="/home/luke/.hermes/profiles/work",
        )

        assert mock_popen.called
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["env"]["HERMES_HOME"] == "/home/luke/.hermes/profiles/work"
