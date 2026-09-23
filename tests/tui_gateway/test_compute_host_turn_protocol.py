"""Turn-protocol coverage for the compute host's REAL (non-seeded) frame path.

``turn.start`` → ``turn.started`` → streamed ``rpc`` event frames (``message.delta``) →
``turn.end`` carrying the session's ``history_version`` / ``message_count`` — the loop at
``ComputeHost._run_real_turn``. The Phase-0 ``session.seed`` / SpikeAgent surface is gone
(no production sender), so this drives the path the dashboard supervisor actually uses:
a ``server._sessions`` entry whose agent runs on the turn worker.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
import types

import pytest

from tests.tui_gateway._compute_host_frames import FrameSink, start_test_work
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    """Neutralize the turn pipeline's environment-heavy side paths (same set as the
    prompt.submit tests) so the frame protocol is what's under test. Threads stay REAL:
    ``_run_real_turn`` joins ``session["_run_thread"]`` itself before emitting ``turn.end``."""
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_apply_pending_model_switch", lambda *a, **k: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_sync_agent_compression_with_config", lambda *a, **k: None)
    monkeypatch.setattr(server, "_sync_agent_fallback_with_config", lambda *a, **k: None)
    monkeypatch.setattr(server, "_sync_bot_capabilities", lambda *a, **k: None)
    monkeypatch.setattr(server, "_adopt_out_of_band_turns", lambda *a, **k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: True)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda session: None)
    monkeypatch.setattr(server, "_routing_provenance_db", lambda session: contextlib.nullcontext(None))
    monkeypatch.setattr(server, "_reopen_routed_session_row", lambda *a, **k: None)
    monkeypatch.setattr(server, "_record_turn_marker", lambda *a, **k: "marker")
    monkeypatch.setattr(server, "_retire_turn_marker", lambda *a, **k: None)
    monkeypatch.setattr(server, "_start_session_work", start_test_work)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})
    monkeypatch.setattr(server, "_start_turn_voice", lambda: (None, False))
    monkeypatch.setattr(server, "_start_usage_ticker", lambda *a, **k: (threading.Event(), start_test_work(lambda: None, name="usage")))
    monkeypatch.setitem(sys.modules, "hermes_undo", types.SimpleNamespace(on_user_message_appended=lambda key: None))


def _agent(deltas: list[str], *, delay_s: float = 0.0, interrupt: threading.Event | None = None):
    def run_conversation(prompt, *, conversation_history=None, stream_callback=None, **_kw):
        chunks = []
        for chunk in deltas:
            if interrupt is not None and interrupt.is_set():
                break
            chunks.append(chunk)
            if stream_callback is not None:
                stream_callback(chunk)
            if delay_s:
                time.sleep(delay_s)
        final = "".join(chunks)
        messages = [*(conversation_history or []), {"role": "user", "content": prompt},
                    {"role": "assistant", "content": final}]
        return {"final_response": final, "messages": messages}

    return types.SimpleNamespace(
        session_id="s1-key", run_conversation=run_conversation, clear_interrupt=lambda: None,
        hard_interrupt=lambda *a, **k: interrupt is not None and interrupt.set())


def _session(agent) -> dict:
    return {
        "agent": agent, "session_key": "s1-key", "history": [], "history_lock": threading.Lock(),
        "history_version": 0, "running": False, "attached_images": [], "image_counter": 0,
        "cols": 80, "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all",
        "inflight_turn": None, "active_session_lease": object(),
    }


def test_turn_start_streams_deltas_then_turn_end_with_history_identity(turn_env):
    out = FrameSink()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    server._sessions[sid] = _session(_agent(["a ", "b ", "c "]))
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "turn", "prompt": "hello"})
        end = out.wait_for(lambda f: f["type"] == "turn.end")
    finally:
        server._sessions.pop(sid, None)
        host.close()

    frames = out.frames()
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "turn.started"
    assert kinds[-1] == "turn.end"
    started = frames[0]
    assert started["sid"] == sid and started["request_id"] == "turn" and "started_ns" in started

    # Streamed output rides the host transport as ``rpc`` frames tagged with the sid, each an
    # ``event`` JSON-RPC notification the parent forwards to the client verbatim.
    deltas = [f for f in frames if f["type"] == "rpc"
              and f["message"]["method"] == "event" and f["message"]["params"]["type"] == "message.delta"]
    assert [d["message"]["params"]["payload"]["text"] for d in deltas] == ["a ", "b ", "c "]
    assert {d["sid"] for d in deltas} == {sid}
    assert any(f["type"] == "rpc" and f["message"]["params"]["type"] == "message.complete" for f in frames)

    # turn.end carries the transcript identity the parent uses to reconcile its mirror.
    assert end["sid"] == sid and end["request_id"] == "turn"
    assert end["session_key"] == "s1-key"
    assert end["history_version"] == 1
    assert end["message_count"] == 2
    assert end["interrupted"] is False
    assert end["session_info_emitted"] is True and isinstance(end["session_info"], dict)
    assert "ended_ns" in end


def test_turn_start_without_sid_is_a_turn_error(turn_env):
    out = FrameSink()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        host.handle_frame({"type": "turn.start", "request_id": "nosid", "prompt": "x"})
        err = out.wait_for(lambda f: f["type"] == "turn.error")
    finally:
        host.close()
    assert err["request_id"] == "nosid" and err["message"] == "sid required"


def test_second_turn_start_while_running_is_session_busy(turn_env):
    out = FrameSink()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    session = _session(_agent(["x"]))
    session["running"] = True
    server._sessions[sid] = session
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "t2", "prompt": "hi"})
        err = out.wait_for(lambda f: f["type"] == "turn.error")
    finally:
        server._sessions.pop(sid, None)
        host.close()
    assert err["request_id"] == "t2" and err["message"] == "session busy"


def test_stale_queued_prompt_generation_ends_turn_as_interrupted(turn_env):
    """A queued prompt whose generation was bumped by an interrupt must not run."""
    out = FrameSink()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    session = _session(_agent(["x"]))
    session["_queued_prompt_generation"] = 3
    server._sessions[sid] = session
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "q", "prompt": "hi",
                           "queued_prompt_generation": 2})
        end = out.wait_for(lambda f: f["type"] == "turn.end")
    finally:
        server._sessions.pop(sid, None)
        host.close()
    assert end["interrupted"] is True and end["request_id"] == "q"
    assert not any(f["type"] == "turn.started" for f in out.frames())


def test_interrupt_frame_acks_and_marks_turn_interrupted(turn_env):
    """The turn runs on the host worker while ``interrupt`` arrives on the control path."""
    out = FrameSink()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    stop = threading.Event()
    server._sessions[sid] = _session(_agent([f"{i:03d} " for i in range(200)], delay_s=0.01, interrupt=stop))
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "turn", "prompt": "go"})
        out.wait_for(lambda f: f["type"] == "rpc" and f["message"]["params"]["type"] == "message.delta")
        host.handle_frame({"type": "interrupt", "sid": sid, "request_id": "stop"})
        ack = out.wait_for(lambda f: f["type"] == "interrupt.ack")
        end = out.wait_for(lambda f: f["type"] == "turn.end")
    finally:
        stop.set()
        server._sessions.pop(sid, None)
        host.close()

    assert ack["applied"] is True and ack["request_id"] == "stop" and "applied_ns" in ack
    assert end["interrupted"] is True
    deltas = sum(1 for f in out.frames() if f["type"] == "rpc" and f["message"]["params"]["type"] == "message.delta")
    assert 0 < deltas < 200


def test_unknown_frame_type_is_an_error():
    out = FrameSink()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        host.handle_frame({"type": "bogus", "request_id": "b"})
    finally:
        host.close()
    assert out.frames() == [{"type": "error", "request_id": "b", "message": "unknown frame type: bogus",
                             "host_ns": out.frames()[0]["host_ns"]}]


@pytest.mark.parametrize("kind", ["legacy", "hard-only", "dynamic-getattr"])
def test_compute_host_interrupt_uses_explicit_stop_compatibility(monkeypatch, kind):
    """Ported from the seeded-session version: the ``interrupt`` frame reaches the live
    agent through ``server._interrupt_session_turn`` → ``request_hard_interrupt``, which must
    prefer a real ``hard_interrupt`` but never trust one fabricated by ``__getattr__``."""
    calls = []

    class _Legacy:
        def interrupt(self):
            calls.append("legacy")

    class _HardOnly:
        def hard_interrupt(self):
            calls.append("hard")

    class _Dynamic:
        def interrupt(self):
            calls.append("legacy")

        def __getattr__(self, name):
            if name == "hard_interrupt":
                return lambda: calls.append("fabricated-hard")
            raise AttributeError(name)

    agent = {"legacy": _Legacy(), "hard-only": _HardOnly(), "dynamic-getattr": _Dynamic()}[kind]
    # The child never routes back to a supervisor (HERMES_COMPUTE_HOST_CHILD=1 in production).
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    out = FrameSink()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    session = _session(agent)
    session["running"] = True
    server._sessions[sid] = session
    try:
        host._handle_interrupt({"sid": sid, "request_id": "stop"})
    finally:
        server._sessions.pop(sid, None)
        host.close()

    assert calls == ["hard" if kind == "hard-only" else "legacy"]
    ack = out.frames()[-1]
    assert ack["type"] == "interrupt.ack" and ack["applied"] is True
    assert session["_turn_cancel_requested"] is True


def test_host_builds_the_session_agent_with_the_frame_login(monkeypatch):
    """The host process has no record for a first turn, and its pipe names no login, so the agent is built
    from the login the frame carries and the new record keeps it for later rebuilds."""
    host = ComputeHost(stdout=FrameSink(), heartbeat_secs=0)
    captured = {}

    def fake_make_agent(sid, key, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(session_id=key)

    def fake_init_session(sid, key, agent, history, **kwargs):
        monkeypatch.setitem(server._sessions, sid, {"agent": agent, "session_key": key})

    monkeypatch.setattr(server, "_make_agent", fake_make_agent)
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda agent, db: False)
    monkeypatch.setattr(server, "_init_session", fake_init_session)
    server._sessions.pop("s-login", None)

    session = host._build_server_session(
        server, {"sid": "s-login", "session_key": "login-key", "history": [], "auth_user_id": "basic:alice"},
        "s-login")

    assert captured["auth_user_id"] == "basic:alice"
    assert session["auth_user_id"] == "basic:alice"
    assert server._session_auth_user_id(session) == "basic:alice"
