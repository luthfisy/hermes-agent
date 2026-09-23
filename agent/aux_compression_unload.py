"""Idle unload of a local auxiliary compression model after it was loaded for one summary.

On unified-memory or single-GPU boxes an auxiliary compression model that loads only to
summarise a compaction keeps its weights resident forever. When
``auxiliary.compression.unload_url`` is configured and the compression model runs on a
local endpoint, Hermes records whether that model was already loaded before the summary,
and if it had to load it for the summary, fires a templated HTTP unload call
``unload_after_seconds`` (default 300) after the last completed compression. A new
compression resets the timer; a re-probe right before firing skips the unload when the
model is verifiably not loaded, and an in-flight summary re-arms the timer instead of
evicting the model mid-summary.

Config (``auxiliary.compression.*``, all optional - feature is off unless
``unload_after_seconds`` is set > 0; that TTL is the on/off switch):
  unload_after_seconds:  idle delay before firing; 0/unset = feature off.
  unload_url:            optional override. Normally NOT set: the unload call resolves
                         per endpoint - first the ``unload_url`` on the provider entry
                         serving that endpoint (custom_providers / providers), else
                         auto-detection for LM Studio (native /api/v1/models with
                         loaded_instances -> its unload endpoint + instance-id body).
                         Servers Hermes cannot identify stay off; a literal URL here
                         targets them explicitly (``{model}``/``{instance_id}`` replaced).
  unload_body:           JSON body template (dict), only meaningful with a literal URL.

Probe contract: loaded-state checks read the served ``/v1/models`` list; when that
payload carries no state field for the model (LM Studio's OpenAI-compatible arm), the
native ``/api/v1/models`` list is consulted too - it exposes ``loaded_instances`` and the
instance id LM Studio's unload endpoint requires. Any probe failure reports "unknown",
and unknown is never treated as "was offline" - a failed unload can evict a model
serving the main conversation, so scheduling requires positive evidence it was not
loaded before.
"""

import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from agent.model_metadata import is_local_endpoint

logger = logging.getLogger(__name__)

_DEFAULT_UNLOAD_AFTER_SECONDS = 0.0  # feature is opt-in: the TTL itself is the switch
_PROBE_TIMEOUT_SECONDS = 5.0
_UNLOAD_TIMEOUT_SECONDS = 15.0
# {model} may not appear (a fixed admin route); validate the placeholder chars only.
_PLACEHOLDER_RE = re.compile(r"\{(model|instance_id)\}")

# Keys (base_url, model) whose compression summary is running right now, refcounted:
# two sessions may compress on the same aux model concurrently. A timer that fires
# while its key is in-flight re-arms instead of unloading - evicting the model
# mid-summary would abort the compaction it was loaded for.
_in_flight: Dict[Any, int] = {}
_in_flight_lock = threading.Lock()


def _unload_config(cfg: Dict[str, Any]) -> Tuple[str, Optional[Any], float]:
    """Parse auxiliary.compression unload keys. Returns (unload_url_override, unload_body, delay).

    The TTL is the feature switch: delay <= 0 means off. An empty url override means
    "resolve per endpoint" (provider entry, then auto-detection) - see
    :func:`resolve_unload_target`. A non-numeric delay falls back to the default;
    negatives clamp to 0 (off).
    """
    block = cfg.get("auxiliary.compression", {}) if isinstance(cfg.get("auxiliary.compression", {}), dict) else {}
    if not block:
        from hermes_cli.config import cfg_get

        block = cfg_get(cfg, "auxiliary", "compression", default={}) or {}
    if not isinstance(block, dict):
        return "", None, 0.0
    url = block.get("unload_url")
    url = url.strip() if isinstance(url, str) else ""
    body = block.get("unload_body")
    if not body:
        body = None
    elif isinstance(body, str):
        # A YAML string like '{"instance_id": "{instance_id}"}' is the natural way to write
        # the template; parse it so the POST carries a JSON object, not a quoted string.
        try:
            body = json.loads(body)
        except ValueError:
            logger.warning("aux compression unload: unload_body is not valid JSON; sending as raw string")
    raw_delay = block.get("unload_after_seconds", _DEFAULT_UNLOAD_AFTER_SECONDS)
    try:
        delay = float(raw_delay)
    except (TypeError, ValueError):
        delay = _DEFAULT_UNLOAD_AFTER_SECONDS
    return url, body, max(0.0, delay)


def _served_root(base_url: str) -> str:
    """Endpoint root that answers /v1/models, honouring a /v1 (or /v1/...) path suffix."""
    parsed = urllib.parse.urlsplit(str(base_url))
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _get_json(url: str, api_key: str = "") -> Optional[Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - probe failure means unknown, never loaded/unloaded
        logger.debug("aux compression unload: probe failed for %s: %s", url, exc)
        return None


def _fetch_models(base_url: str, api_key: str = "") -> Optional[Dict[str, Any]]:
    """GET /v1/models from the served endpoint; None on any failure (unknown)."""
    return _get_json(_served_root(base_url).rstrip("/") + "/v1/models", api_key)


def _fetch_native_models(base_url: str, api_key: str = "") -> Optional[Dict[str, Any]]:
    """GET the native /api/v1/models list (LM Studio); None when not served (llama-swap 404s)."""
    return _get_json(_served_root(base_url).rstrip("/") + "/api/v1/models", api_key)


def _detect_endpoint_unload(base_url: str, api_key: str = "") -> Tuple[Optional[str], Optional[Any]]:
    """Derive (unload_url_template, unload_body_template) for endpoints Hermes knows natively.

    Only LM Studio is auto-detected: its native ``/api/v1/models`` entries carry
    ``loaded_instances``, and its unload takes the loaded instance id, not the model key.
    Other servers (llama-swap et al.) have no standard unload route Hermes can infer -
    configure ``unload_url`` on the provider entry or in auxiliary.compression for those.
    """
    root = _served_root(base_url).rstrip("/")
    native = _fetch_native_models(base_url, api_key)
    models = native.get("models") if isinstance(native, dict) else None
    if isinstance(models, list) and any(isinstance(m, dict) and "loaded_instances" in m for m in models):
        return root + "/api/v1/models/unload", {"instance_id": "{instance_id}"}
    return None, None


def resolve_unload_target(cfg_url: str, cfg_body: Any, base_url: str, api_key: str = "",
                          cfg: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[Any]]:
    """Resolve the unload target for one endpoint, in order:

    1. literal ``unload_url`` from auxiliary.compression (explicit override, any server);
    2. the ``unload_url`` on the provider entry serving this endpoint
       (custom_providers / providers — how llama-swap users configure it once, per provider);
    3. auto-detection (LM Studio only).
    Unresolved means off: never POST into an endpoint whose unload route is unknown.
    """
    if cfg_url:
        return cfg_url, cfg_body
    entry_url = _provider_entry_unload(cfg, base_url)
    if entry_url:
        return entry_url, None
    return _detect_endpoint_unload(base_url, api_key)


def _provider_entry_unload(cfg: Optional[Dict[str, Any]], base_url: str) -> Optional[str]:
    """``unload_url`` from the custom_providers/providers entry whose base_url serves this endpoint."""
    if cfg is None:
        try:
            cfg = _load_config()
        except Exception:  # noqa: BLE001
            return None
    target = _served_root(base_url).rstrip("/").lower()
    try:
        from hermes_cli.config_providers import get_compatible_custom_providers

        for entry in get_compatible_custom_providers(cfg) or []:
            if not isinstance(entry, dict):
                continue
            url = entry.get("unload_url")
            if not (isinstance(url, str) and url.strip()):
                continue
            if _served_root(str(entry.get("base_url", ""))).rstrip("/").lower() == target:
                return url.strip()
    except Exception as exc:  # noqa: BLE001 - provider lookup is best-effort
        logger.debug("aux compression unload: provider entry lookup failed: %s", exc)
    return None


def _verdict_from(payload: Any, model: str) -> Tuple[Optional[bool], Optional[str]]:
    """(loaded verdict, instance id) from a models-list payload; None verdict = undecidable."""
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        # OpenAI-style list payload (LM Studio's /v1/models uses "data").
        models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None, None
    for entry in models:
        if not isinstance(entry, dict):
            continue
        name = entry.get("id") or entry.get("model") or entry.get("key")
        if name != model:
            continue
        status = entry.get("status")
        if isinstance(status, dict):
            value = status.get("value")
            loaded = bool(value) and str(value).lower() not in ("not loaded", "unloaded", "offloaded")
            return loaded, None
        if "loaded" in entry:
            return bool(entry.get("loaded")), None
        if "loaded_instances" in entry:
            instances = entry.get("loaded_instances") or []
            instance_id = None
            if instances and isinstance(instances[0], dict) and instances[0].get("id") is not None:
                instance_id = str(instances[0]["id"])
            return bool(instances), instance_id
        # Entry matched the model but carries no state field: undecidable.
        return None, None
    # Absent from the served list entirely: llama-swap lists all configured models,
    # LM Studio lists everything on disk - in both, absence means not loaded.
    return False, None


def probe_aux_state(base_url: str, model: str, api_key: str = "") -> Tuple[Optional[bool], Optional[str]]:
    """(loaded verdict, instance id) for a model on a local endpoint.

    /v1/models first; when it cannot decide (LM Studio's OpenAI arm has no state field),
    fall back to the native /api/v1/models which carries loaded_instances.
    """
    if not base_url:
        return None, None
    payload = _fetch_models(base_url, api_key)
    if payload is not None:
        verdict, instance_id = _verdict_from(payload, model)
        if verdict is not None:
            return verdict, instance_id
    native = _fetch_native_models(base_url, api_key)
    if native is not None:
        return _verdict_from(native, model)
    return None, None


def probe_aux_loaded(base_url: str, model: str, api_key: str = "") -> Optional[bool]:
    return probe_aux_state(base_url, model, api_key)[0]


def _fill_model(template: Any, model: str, instance_id: Optional[str] = None) -> Any:
    fallback = instance_id or model  # no probe verdict: LM Studio may still accept the key
    if isinstance(template, str):
        return _PLACEHOLDER_RE.sub(lambda m: fallback if m.group(1) == "instance_id" else model, template)
    if isinstance(template, dict):
        return {k: _fill_model(v, model, instance_id) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill_model(v, model, instance_id) for v in template]
    return template


class _AuxUnloadTimerManager:
    """One daemon timer per (base_url, model); a newer compression resets it."""

    def __init__(self) -> None:
        self._timers: Dict[Tuple[str, str], threading.Timer] = {}
        self._tokens: Dict[Tuple[str, str], object] = {}
        self._lock = threading.Lock()

    def is_armed(self, base_url: str, model: str) -> bool:
        with self._lock:
            return (str(base_url), str(model)) in self._timers

    def reset(self, base_url: str, model: str, delay: float, unload_url, unload_body, api_key: str = "",
              rearmed: bool = False) -> None:
        key = (str(base_url), str(model))
        with self._lock:
            old = self._timers.pop(key, None)
        if old is not None:
            old.cancel()
        timer = threading.Timer(
            delay, self._fire,
            args=(key, timer_token := object(), delay, unload_url, unload_body, api_key, rearmed))
        timer.daemon = True
        timer.name = "hermes-aux-compression-unload"
        with self._lock:
            self._timers[key] = timer
            self._tokens[key] = timer_token
        timer.start()

    def _done(self, key, token=None) -> None:
        with self._lock:
            # A stale fire (superseded by reset/re-arm) must not clear the NEW owner's
            # bookkeeping; only the token currently held may clear it.
            if token is not None and self._tokens.get(key) is not token:
                return
            self._timers.pop(key, None)
            self._tokens.pop(key, None)

    def _fire(self, key, token, delay, unload_url, unload_body, api_key, rearmed=False) -> None:
        base_url, model = key
        try:
            # A newer compression may have re-armed (or a cancel raced this firing);
            # only the timer still holding the current token may evict.
            with self._lock:
                if self._tokens.get(key) is not token:
                    return
            with _in_flight_lock:
                if _in_flight.get(key, 0) > 0 and not rearmed:
                    # A summary is running on this model right now; the post-commit
                    # schedule may not have landed yet. Re-arm once and let it decide -
                    # a second in-flight hit skips (the next compression re-arms anyway).
                    self.reset(base_url, model, delay, unload_url, unload_body, api_key, rearmed=True)
                    return
            verdict, instance_id = probe_aux_state(base_url, model, api_key)
            if verdict is None:
                logger.debug("aux compression unload: loaded-state unknown for %s; firing configured unload", model)
            if verdict is not False:  # False = already gone (e.g. llama-swap TTL): nothing to do
                self._post_unload(unload_url, unload_body, model, instance_id, api_key)
        except Exception as exc:  # noqa: BLE001 - never crash the timer thread
            logger.warning("aux compression unload failed for %s: %s", model, exc)
        finally:
            self._done(key, token)

    @staticmethod
    def _post_unload(unload_url, unload_body, model, instance_id, api_key) -> None:
        url = _fill_model(str(unload_url), model, instance_id)
        data = None
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if unload_body is not None:
            data = json.dumps(_fill_model(unload_body, model, instance_id)).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=_UNLOAD_TIMEOUT_SECONDS) as resp:
                resp.read()
            logger.info("aux compression unload: unloaded '%s' from %s", model, _served_root(url))
        except Exception as exc:  # noqa: BLE001 - unload is best-effort
            logger.warning("aux compression unload request failed for %s (%s): %s", model, url, exc)


_manager = _AuxUnloadTimerManager()


def _load_config() -> Dict[str, Any]:
    from hermes_cli.config import load_config_readonly

    return load_config_readonly() or {}


def note_aux_state_before_summary(agent: Any) -> None:
    """Record whether the aux compression model was already loaded, before a summary runs.

    Stores agent._aux_compression_was_offline = True/False/None (None = unknown/disabled)
    and marks the (endpoint, model) key in-flight so an armed timer cannot evict the
    model mid-summary.
    """
    agent._aux_compression_was_offline = None
    agent._aux_compression_unload_key = None
    try:
        cfg = _load_config()
        cfg_url, _body, delay = _unload_config(cfg)
        if delay <= 0:
            return
        aux_base_url, aux_model, aux_key = _resolve_aux_route(agent)
        if not aux_base_url or not aux_model or not is_local_endpoint(str(aux_base_url)):
            return
        if not resolve_unload_target(cfg_url, _body, str(aux_base_url), aux_key, cfg)[0]:
            return
        # Never arm against the model/route serving this very conversation.
        if _same_route(aux_base_url, aux_model, getattr(agent, "base_url", None), getattr(agent, "model", None)):
            return
        key = (str(aux_base_url), str(aux_model))
        with _in_flight_lock:
            _in_flight[key] = _in_flight.get(key, 0) + 1
        agent._aux_compression_unload_key = key
        agent._aux_compression_was_offline = probe_aux_loaded(str(aux_base_url), aux_model, aux_key) is False
    except Exception as exc:  # noqa: BLE001
        logger.debug("aux compression pre-summary probe skipped: %s", exc)


def clear_aux_compression_in_flight(agent: Any) -> None:
    """Drop the in-flight mark set by note_aux_state_before_summary (finally-safe)."""
    key = getattr(agent, "_aux_compression_unload_key", None)
    if key is not None:
        agent._aux_compression_unload_key = None
        with _in_flight_lock:
            remaining = _in_flight.get(key, 0) - 1
            if remaining > 0:
                _in_flight[key] = remaining
            else:
                _in_flight.pop(key, None)


def schedule_aux_unload_after_compression(agent: Any) -> None:
    """After a committed compression: if the model had to load for it, arm the idle unload.

    A new compression re-arms (resets) the timer instead of stacking one per run; a warm
    compression only extends an already-armed timer (the idle window counts from the
    LAST compression, not from the one that cold-loaded the model).
    """
    clear_aux_compression_in_flight(agent)
    try:
        cfg = _load_config()
        cfg_url, body, delay = _unload_config(cfg)
        if delay <= 0:
            return
        aux_base_url, aux_model, aux_key = _resolve_aux_route(agent)
        if not aux_base_url or not aux_model:
            return
        unload_url, body = resolve_unload_target(cfg_url, body, str(aux_base_url), aux_key, cfg)
        if not unload_url:
            return
        if _same_route(aux_base_url, aux_model, getattr(agent, "base_url", None), getattr(agent, "model", None)):
            return
        was_offline = getattr(agent, "_aux_compression_was_offline", None) is True
        if not was_offline and not _manager.is_armed(str(aux_base_url), aux_model):
            return
        _manager.reset(str(aux_base_url), aux_model, delay, unload_url, body, aux_key)
        logger.info("aux compression model '%s' will unload in %.0fs if it stays idle", aux_model, delay)
    except Exception as exc:  # noqa: BLE001
        logger.debug("aux compression unload scheduling skipped: %s", exc)


def _resolve_aux_route(agent: Any) -> Tuple[Optional[str], Optional[str], str]:
    """(base_url, model, api_key) of the aux compression lane, or (None, None, '')."""
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, aux_model = get_text_auxiliary_client("compression", main_runtime=agent._current_main_runtime())
        if client is None or not aux_model:
            return None, None, ""
        raw_key = getattr(client, "api_key", "")
        api_key = "" if (callable(raw_key) and not isinstance(raw_key, str)) else str(raw_key or "")
        return str(getattr(client, "base_url", "") or ""), str(aux_model), api_key
    except Exception:  # noqa: BLE001
        return None, None, ""


def _same_route(base_url_a: str, model_a: str, base_url_b: Any, model_b: Any) -> bool:
    if not base_url_b or not model_b or str(model_a) != str(model_b):
        return False
    try:
        from utils import base_url_hostname

        return base_url_hostname(str(base_url_a)) == base_url_hostname(str(base_url_b))
    except Exception:  # noqa: BLE001
        return False
