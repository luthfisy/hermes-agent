"""Aux compression idle-unload: config parsing, probe verdicts, timer semantics.

Contracts (agent/aux_compression_unload.py):
  * Off unless auxiliary.compression.unload_after_seconds > 0 (the TTL is the switch).
    The unload target resolves per endpoint: literal unload_url override, else the
    provider entry's unload_url, else LM Studio auto-detection; unresolved = off.
    The pre-summary probe records loaded-state, and scheduling arms an unload ONLY when
    the model had to load for the summary (was_offline True) or a timer is already armed
    (idle window counts from the LAST compression), and never for the main conversation
    route.
  * A newer compression resets the idle timer instead of stacking one per run.
  * Firing re-probes: a model that is verifiably not loaded is left alone; the
    configured unload POST fires with {model}/{instance_id} substituted.
  * A summary in flight on that (endpoint, model) blocks eviction: the timer re-arms
    once, and a second in-flight hit skips (the next compression re-arms anyway).
"""

import threading
import time

import pytest

import agent.aux_compression_unload as aux

KEY = ("http://localhost:8642/v1", "intermediate")


# ---- probe verdicts -------------------------------------------------------

LLAMA_SWAP = {"models": [
    {"id": "intermediate", "status": {"value": "loaded"}},
    {"id": "advanced", "status": {"value": "not loaded"}},
]}
LM_STUDIO_NATIVE = {"models": [
    {"key": "m-loaded", "loaded_instances": [{"id": 7}]},
    {"key": "m-off", "loaded_instances": []},
]}
LM_STUDIO_V1 = {"data": [{"id": "m-loaded", "object": "model"}, {"id": "m-off", "object": "model"}]}


def test_probe_verdicts_loaded_offline_absent_unknown():
    assert aux._verdict_from(LLAMA_SWAP, "intermediate") == (True, None)
    assert aux._verdict_from(LLAMA_SWAP, "advanced") == (False, None)
    assert aux._verdict_from(LLAMA_SWAP, "nope") == (False, None)  # absent from served list
    assert aux._verdict_from(LM_STUDIO_NATIVE, "m-loaded") == (True, "7")
    assert aux._verdict_from(LM_STUDIO_NATIVE, "m-off") == (False, None)
    assert aux._verdict_from(LM_STUDIO_V1, "m-loaded") == (None, None)  # no state field
    assert aux._verdict_from({}, "x") == (None, None)  # undecidable


def test_state_falls_back_to_native_when_v1_undecidable(monkeypatch):
    monkeypatch.setattr(aux, "_fetch_models", lambda base, key="": LM_STUDIO_V1)
    monkeypatch.setattr(aux, "_fetch_native_models", lambda base, key="": LM_STUDIO_NATIVE)
    assert aux.probe_aux_state("http://localhost:1234/v1", "m-loaded") == (True, "7")
    # native also undecidable -> unknown, never a guess
    monkeypatch.setattr(aux, "_fetch_native_models", lambda base, key="": LM_STUDIO_V1)
    assert aux.probe_aux_state("http://localhost:1234/v1", "m-loaded") == (None, None)


def test_served_root_strips_v1_suffix():
    assert aux._served_root("http://localhost:8642/v1") == "http://localhost:8642"
    assert aux._served_root("http://127.0.0.1:1234/") == "http://127.0.0.1:1234"


# ---- config parsing -------------------------------------------------------

def _cfg(url="", body="", delay=300.0):  # noqa: ANN001
    return {"auxiliary.compression": {"unload_url": url, "unload_body": body, "unload_after_seconds": delay}}


def test_config_off_without_ttl():
    # the TTL is the switch: unset/0/negative = off regardless of unload_url
    assert aux._unload_config(_cfg(url="http://h/u/{model}", delay=0))[2] == 0.0
    assert aux._unload_config({"auxiliary.compression": {"unload_url": "http://h/u/{model}"}})[2] == aux._DEFAULT_UNLOAD_AFTER_SECONDS
    assert aux._unload_config({}) == ("", None, 0.0)


def test_config_delay_defaults_and_clamps():
    _, _, delay = aux._unload_config(_cfg(url="http://h/x", delay="bogus"))
    assert delay == aux._DEFAULT_UNLOAD_AFTER_SECONDS
    assert aux._unload_config(_cfg(url="http://h/x", delay=-5))[2] == 0.0
    # empty-string unload_body (the shipped default) means no body, not POST ""
    assert aux._unload_config(_cfg(url="http://h/x"))[1] is None


def test_template_fill_recurses():
    filled = aux._fill_model({"instance_id": "{instance_id}", "list": ["{model}"]}, "intermediate", "7")
    assert filled == {"instance_id": "7", "list": ["intermediate"]}
    # no probe verdict: {instance_id} falls back to the model key (LM Studio accepts it)
    assert aux._fill_model('{"instance_id": "{instance_id}"}', "m", None) == '{"instance_id": "m"}'


# ---- target resolution cascade --------------------------------------------

def test_auto_detects_lmstudio_only(monkeypatch):
    monkeypatch.setattr(aux, "_fetch_native_models", lambda base, key="": LM_STUDIO_NATIVE)
    url, body = aux._detect_endpoint_unload("http://localhost:1234/v1")
    assert url == "http://localhost:1234/api/v1/models/unload"
    assert body == {"instance_id": "{instance_id}"}
    # an endpoint whose native list lacks loaded_instances is NOT a known server: stays off
    monkeypatch.setattr(aux, "_fetch_native_models", lambda base, key="": {"models": [{"id": "x"}]})
    assert aux._detect_endpoint_unload("http://localhost:9999/v1") == (None, None)


def test_resolve_cascade_override_then_entry_then_auto(monkeypatch):
    # only :1234 answers the LM Studio native probe; everything else is unknown
    monkeypatch.setattr(aux, "_fetch_native_models",
                        lambda base, key="": LM_STUDIO_NATIVE if "1234" in base else None)
    entry_cfg = {"custom_providers": [
        {"name": "swap", "base_url": "http://localhost:8642/v1",
         "unload_url": "http://localhost:8642/api/models/unload/{model}"},
        {"name": "other", "base_url": "http://localhost:7000/v1"},
    ]}
    # 1. literal override wins, no probing
    assert aux.resolve_unload_target("http://h/u/{model}", "b", "http://h/v1", cfg=entry_cfg) == ("http://h/u/{model}", "b")
    # 2. provider entry for THIS endpoint (matched on /v1-stripped root)
    assert aux.resolve_unload_target("", None, "http://localhost:8642/v1", cfg=entry_cfg) == (
        "http://localhost:8642/api/models/unload/{model}", None)
    # entry exists but for another endpoint: no match -> falls to auto
    assert aux.resolve_unload_target("", None, "http://localhost:1234/v1", cfg=entry_cfg)[0] == (
        "http://localhost:1234/api/v1/models/unload")
    # 3. no override, no entry, not LM Studio -> off
    assert aux.resolve_unload_target("", None, "http://localhost:9999/v1", cfg=entry_cfg) == (None, None)


def test_ttl_without_target_disables_scheduling(monkeypatch, fired):
    """TTL on, but endpoint resolves to nothing: schedule nothing (pre-summary guard)."""
    monkeypatch.setattr(aux, "_load_config", lambda: _cfg(delay=0.05))
    _patch_route(monkeypatch)
    monkeypatch.setattr(aux, "_detect_endpoint_unload", lambda base, key="": (None, None))
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (False, None))
    agent = FakeAgent()
    aux.note_aux_state_before_summary(agent)
    assert agent._aux_compression_was_offline is None  # unknown endpoint: never probed/armed
    aux.schedule_aux_unload_after_compression(agent)
    assert aux._manager._timers == {}


def test_ttl_zero_disables_everything(monkeypatch, fired):
    """The TTL is the switch: delay 0 must not probe or schedule even with a valid URL."""
    monkeypatch.setattr(aux, "_load_config", lambda: _cfg(url="http://h/u/{model}", delay=0))
    _patch_route(monkeypatch)
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (False, None))
    agent = FakeAgent()
    aux.note_aux_state_before_summary(agent)
    assert agent._aux_compression_was_offline is None
    aux.schedule_aux_unload_after_compression(agent)
    assert aux._manager._timers == {}


# ---- timer manager semantics ---------------------------------------------

@pytest.fixture
def fired(monkeypatch):
    calls = []
    monkeypatch.setattr(aux._AuxUnloadTimerManager, "_post_unload",
                        staticmethod(lambda url, body, model, iid, key: calls.append((url, model, iid))))
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (False, None))  # False->skip
    yield calls
    for key in list(aux._manager._timers):
        timer = aux._manager._timers.pop(key)
        aux._manager._tokens.pop(key, None)
        timer.cancel()
    with aux._in_flight_lock:
        aux._in_flight.clear()


def _fire_now(key, token, url, body, iid=None):
    aux._manager._fire(key, token, 0.0, url, body, "")


def test_reprobe_skips_when_already_unloaded(fired):
    aux._manager._tokens[KEY] = (token := object())
    _fire_now(KEY, token, "http://h/unload/{model}", "")
    assert fired == []  # probe said not loaded -> nothing posted


def test_reprobe_true_fires_post(fired, monkeypatch):
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (True, "7"))
    aux._manager._tokens[KEY] = (token := object())
    _fire_now(KEY, token, "http://h/unload/{model}", "")
    # _post_unload (stubbed here) receives the raw template and fills placeholders itself;
    # template filling is covered by test_template_fill_recurses.
    assert fired == [("http://h/unload/{model}", "intermediate", "7")]


def test_in_flight_summary_rearms_instead_of_evicting(fired):
    with aux._in_flight_lock:
        aux._in_flight[KEY] = aux._in_flight.get(KEY, 0) + 1
    aux._manager._tokens[KEY] = (token := object())
    _fire_now(KEY, token, "http://h/unload/{model}", "")
    assert fired == []  # mid-summary: never evict
    assert aux._manager.is_armed(*KEY)  # re-armed once
    # second hit while still in flight: skip (the next compression re-arms anyway)
    _fire_now(KEY, aux._manager._tokens[KEY], "http://h/unload/{model}", "")
    assert fired == []


def test_reset_cancels_previous_timer(fired):
    aux._manager.reset("http://h/v1", "m", 60, "http://h/u", None)
    first = aux._manager._timers[("http://h/v1", "m")]
    aux._manager.reset("http://h/v1", "m", 0.05, "http://h/u", '{"m":"{model}"}')
    second = aux._manager._timers[("http://h/v1", "m")]
    first.join(2)  # cancel() is async: the cancelled thread exits within moments
    assert first.is_alive() is False and second is not first
    second.join(2)
    assert fired == []  # probe False path skips the post; timer ran clean
    assert not aux._manager.is_armed("http://h/v1", "m")


def test_stale_fire_does_not_clear_new_owner(fired):
    aux._manager.reset("http://h/v1", "m", 60, "http://h/u", None)
    stale_token = object()
    aux._manager._fire(("http://h/v1", "m"), stale_token, 60, "http://h/u", None, "")
    # token mismatch returns early; the live timer's bookkeeping survives
    assert aux._manager.is_armed("http://h/v1", "m")


# ---- scheduling guards ----------------------------------------------------

class FakeAgent:
    base_url = "http://localhost:9999/v1"
    _aux_compression_was_offline = None
    _aux_compression_unload_key = None
    model = "main-model"
    session_id = "test"

    def _current_main_runtime(self):
        return {"provider": "custom", "model": "main-model", "base_url": self.base_url}


def _patch_route(monkeypatch, base_url="http://localhost:8642/v1", model="intermediate"):
    monkeypatch.setattr(aux, "_resolve_aux_route",
                        lambda agent: (base_url, model, ""))


def test_nothing_scheduled_when_feature_off(monkeypatch, fired):
    monkeypatch.setattr(aux, "_load_config", lambda: {})
    _patch_route(monkeypatch)
    agent = FakeAgent()
    aux.note_aux_state_before_summary(agent)
    assert agent._aux_compression_was_offline is None
    aux.schedule_aux_unload_after_compression(agent)
    assert aux._manager._timers == {}


def test_schedules_only_after_a_cold_load(monkeypatch, fired):
    monkeypatch.setattr(aux, "_load_config", lambda: _cfg(url="http://h/u/{model}", delay=0.05))
    _patch_route(monkeypatch)
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (False, None))
    agent = FakeAgent()
    aux.note_aux_state_before_summary(agent)
    assert agent._aux_compression_was_offline is True  # was not loaded before the summary
    aux.schedule_aux_unload_after_compression(agent)
    assert aux._manager.is_armed(*KEY)
    # the summary finished; timer fires after the idle delay
    aux.clear_aux_compression_in_flight(agent)
    deadline = time.monotonic() + 2
    while aux._manager.is_armed(*KEY) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not aux._manager.is_armed(*KEY)


def test_warm_model_never_arms_and_main_route_guarded(monkeypatch, fired):
    monkeypatch.setattr(aux, "_load_config", lambda: _cfg(url="http://h/u/{model}", delay=0.05))
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (True, None))
    _patch_route(monkeypatch)
    agent = FakeAgent()
    aux.note_aux_state_before_summary(agent)
    aux.schedule_aux_unload_after_compression(agent)
    assert aux._manager._timers == {}  # was already loaded -> not ours to evict

    # aux on the SAME route as the main model: never armed, even when "offline"
    _patch_route(monkeypatch, base_url=FakeAgent.base_url, model="main-model")
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (False, None))
    agent2 = FakeAgent()
    aux.note_aux_state_before_summary(agent2)
    aux.schedule_aux_unload_after_compression(agent2)
    assert aux._manager._timers == {}
    assert getattr(agent2, "_aux_compression_was_offline") in (None, False)


def test_warm_compression_extends_existing_timer(monkeypatch, fired):
    """Idle window counts from the LAST compression, not from the cold-load one."""
    monkeypatch.setattr(aux, "_load_config", lambda: _cfg(url="http://h/u/{model}", delay=60))
    _patch_route(monkeypatch)
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (False, None))
    agent = FakeAgent()
    aux.note_aux_state_before_summary(agent)
    aux.schedule_aux_unload_after_compression(agent)
    assert aux._manager.is_armed(*KEY)
    aux.clear_aux_compression_in_flight(agent)
    # second compression: model already warm (probe True), timer still re-armed
    monkeypatch.setattr(aux, "probe_aux_state", lambda base, model, key="": (True, None))
    agent2 = FakeAgent()
    aux.note_aux_state_before_summary(agent2)
    aux.schedule_aux_unload_after_compression(agent2)
    assert aux._manager.is_armed(*KEY)
    aux.clear_aux_compression_in_flight(agent2)
