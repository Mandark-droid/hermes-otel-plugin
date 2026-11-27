# SPDX-License-Identifier: MIT
"""traceverse_hermes_otel — Hermes plugin for OpenTelemetry (OTLP) observability.

Exports Hermes's built-in observability events over **OpenTelemetry** using the
vendor-neutral **GenAI semantic conventions** (``gen_ai.*``). It slots in beside
the bundled ``observability/langfuse`` and ``observability/nemo_relay`` plugins
and makes **no changes to Hermes core** — Hermes already produces the right
events (a turn, per-LLM-API-call usage/cost, per-tool-call results); this plugin
adds an OTLP sink for them so the telemetry flows to *any* OTel backend (an
OpenTelemetry Collector, Jaeger, Grafana Tempo, Honeycomb, …) with no lock-in.

Activation is handled by the Hermes plugin system — standalone plugins only load
when enabled (``hermes plugins enable traceverse_hermes_otel``). At runtime the
plugin also requires the OpenTelemetry SDK; if it is missing the hooks no-op
(fail-open), exactly like the other observability plugins.

Required env vars: none (it works out of the box against a local collector).

Optional env vars (set via ``hermes tools`` or ``~/.hermes/.env``):
  HERMES_OTEL_SERVICE_NAME    - service.name resource attr (default: agent.coding.hermes)
  HERMES_OTEL_ENDPOINT        - OTLP HTTP endpoint (default: http://localhost:4318)
  HERMES_OTEL_AGENT_NAME      - gen_ai.agent.name value (default: hermes)
  HERMES_OTEL_CAPTURE_CONTENT - "true" to attach prompt/response text (default: off, privacy-gated)
  HERMES_OTEL_USER_ID         - stamp user.id on the resource

Standard OTel env vars are honoured as fallbacks: OTEL_SERVICE_NAME,
OTEL_EXPORTER_OTLP_ENDPOINT.

Span model (GenAI conventions):

    invoke_agent      per Hermes turn   (gen_ai.conversation.id = session id)
      ├── chat        per LLM API call  (tokens, cost, finish reason, TTFT)
      └── execute_tool  per tool call   (gen_ai.tool.name, gen_ai.tool.type)
            └── invoke_agent  a delegated sub-agent's turn (delegate_task only)
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # imported lazily at call time, like the other submodules
    from .identity import RunIdentity

logger = logging.getLogger(__name__)

# Sentinel: "_get_emitter() tried and failed" — short-circuits every subsequent
# hook call without re-importing the SDK. Mirrors the langfuse/nemo_relay gate.
_INIT_FAILED = object()
_LOCK = threading.RLock()

# Span emitter and parallel OTel *log-record* emitter (populates the agent-coding
# dashboard, which is built from log events, not spans), one pair per run identity.
# Identity (gateway platform, profile) is a RESOURCE attribute and a resource belongs
# to a provider, so each distinct identity gets its own provider pair, built on first
# use. A gateway serving Slack and Telegram holds two; a CLI holds one. The log
# emitter may be a no-op when the logs SDK/endpoint is unavailable. A value of
# ``_INIT_FAILED`` means building that identity's emitters failed.
_EMITTERS: dict[RunIdentity, Any | object] = {}
_LOG_EMITTERS: dict[RunIdentity, Any | object] = {}

# session id -> the identity its run was first seen with. Hooks of one run can fire
# on threads that do not carry the gateway's context (delegated sub-agents run on
# worker threads), so identity is remembered per session instead of re-read.
_SESSION_IDENTITIES: dict[str, RunIdentity] = {}
_MAX_SESSION_IDENTITIES = 1024

# One open invoke_agent context per active session, keyed by session id.
_OPEN_TURNS_LOCK = threading.Lock()
_OPEN_TURNS: dict[str, Any] = {}
# The invoke_agent span object for each open turn (same key + lock). Held so a
# later hook (pre_llm_call) can stamp the prompt onto the still-open span.
_OPEN_TURN_SPANS: dict[str, Any] = {}

# Sub-agent linkage. delegate_task's execute_tool span is opened on pre_tool_call
# (not after the fact) so each sub-agent's turn can be its child.
#   _DELEGATIONS        (session_id, tool_call_id) -> open _Delegation
#   _LATEST_DELEGATION  session_id -> its newest open _Delegation (subagent_start
#                       names the parent session but not the tool call)
#   _CHILD_LINKS        child session_id -> _ChildLink, until the child session ends
_LINKS_LOCK = threading.Lock()
_DELEGATIONS: dict[tuple[str, str], Any] = {}
_LATEST_DELEGATION: dict[str, Any] = {}
_CHILD_LINKS: dict[str, Any] = {}
# A child whose session never ends (crash, os._exit) must not leak its link forever.
_MAX_CHILD_LINKS = 1024
DELEGATE_TOOL = "delegate_task"


class _Delegation:
    """One running ``delegate_task`` call: its open span and the tasks it was given."""

    def __init__(self, span: Any, session_id: str, goals: list[Any]) -> None:
        self.span = span
        self.session_id = session_id
        self.goals = goals
        self.claimed: set[int] = set()
        self.child_session_ids: list[str] = []

    def claim_task_index(self, goal: Any) -> int | None:
        """Task index of a child with this goal: the first unclaimed task with the
        same goal, else the next unclaimed task in order. Hermes builds children in
        task order but skips a refused task, so order alone can be off by one; the
        goal match is what makes it exact."""
        for i, task_goal in enumerate(self.goals):
            if i not in self.claimed and goal is not None and task_goal == goal:
                self.claimed.add(i)
                return i
        for i in range(len(self.goals)):
            if i not in self.claimed:
                self.claimed.add(i)
                return i
        return None


class _ChildLink:
    """What a sub-agent's spans need to know about the run that delegated it."""

    def __init__(
        self,
        parent_span: Any,
        parent_session_id: str,
        task_index: int | None,
        identity: RunIdentity | None = None,
    ) -> None:
        self.parent_span = parent_span
        self.parent_session_id = parent_session_id
        self.task_index = task_index
        # The delegating run's identity, read where Hermes' context is still bound.
        self.identity = identity

    def attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"hermes.parent_session_id": self.parent_session_id}
        if self.task_index is not None:
            attrs["hermes.delegation.task_index"] = self.task_index
        return attrs


# Last session id we opened a turn for, used as a fallback when a per-call hook
# (post_api_request / post_tool_call) does not carry the session id in kwargs.
_ACTIVE_SESSION_LOCK = threading.Lock()
_ACTIVE_SESSION_ID: str = "default"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str) -> bool:
    return _env(name).lower() in {"1", "true", "yes", "on"}


def _run_identity(kwargs: dict[str, Any] | None) -> RunIdentity:
    """The identity of the run this hook belongs to.

    A sub-agent takes its delegating run's identity (its worker thread does not
    carry the gateway's context). Any other session is read from Hermes on the
    hook's own thread and remembered once a gateway platform is seen, so the rest
    of the run stays on one resource even if a later hook fires elsewhere.
    """
    from .identity import detect_run_identity

    kwargs = kwargs or {}
    session_id = _session_id_or_active(kwargs)
    with _LINKS_LOCK:
        known = _SESSION_IDENTITIES.get(session_id)
        link = _CHILD_LINKS.get(session_id)
        parent_identity = None
        if kwargs.get("parent_session_id"):
            parent_identity = _SESSION_IDENTITIES.get(str(kwargs["parent_session_id"]))
    if known is not None:
        return known
    inherited = (link.identity if link is not None else None) or parent_identity
    identity = inherited if inherited is not None else detect_run_identity()
    if inherited is not None or identity.platform:
        with _LINKS_LOCK:
            _SESSION_IDENTITIES[session_id] = identity
            while len(_SESSION_IDENTITIES) > _MAX_SESSION_IDENTITIES:
                _SESSION_IDENTITIES.pop(next(iter(_SESSION_IDENTITIES)))
    return identity


def _get_emitter(kwargs: dict[str, Any] | None = None) -> Any | None:
    """Return the cached :class:`OtelGenAIEmitter` for this run, or ``None``.

    Activation of this plugin is controlled by the Hermes plugin system — this
    function only handles the runtime-availability gate (OTel SDK importable).
    Emitters are built once per run identity, then returned (or fast-``None`` on
    failure) on every subsequent call. Thread-safe via double-checked locking
    so concurrent agent sessions don't race to build two providers.
    """
    identity = _run_identity(kwargs)
    emitter = _EMITTERS.get(identity)
    if emitter is _INIT_FAILED:
        return None
    if emitter is not None:
        return emitter
    with _LOCK:
        emitter = _EMITTERS.get(identity)
        if emitter is _INIT_FAILED:
            return None
        if emitter is not None:
            return emitter
        try:
            from .emitter import OtelGenAIEmitter
            from .log_emitter import OtelLogEmitter
            from .provider import build_logger, build_tracer

            service_name = _env("HERMES_OTEL_SERVICE_NAME") or None
            otlp_endpoint = _env("HERMES_OTEL_ENDPOINT") or None
            user_id = _env("HERMES_OTEL_USER_ID") or None
            capture = _env_bool("HERMES_OTEL_CAPTURE_CONTENT")
            agent_name = _env("HERMES_OTEL_AGENT_NAME") or "hermes"
            extra_resource = identity.resource_attributes()

            tracer = build_tracer(
                service_name=service_name,
                otlp_endpoint=otlp_endpoint,
                user_id=user_id,
                capture_content=capture,
                extra_resource=extra_resource,
            )
            emitter = OtelGenAIEmitter(
                tracer,
                agent_name=agent_name,
                capture_content=capture,
            )
            # Build the parallel dashboard log emitter. Failure here must NOT
            # disable spans — log emission is best-effort; build_logger() returns
            # None on any problem and OtelLogEmitter then no-ops. An operator
            # pointing the plugin at a spans-only OTLP receiver (one without
            # /v1/logs) can set HERMES_OTEL_LOGS_ENABLED=false to skip the
            # pipeline entirely: no provider, no exporter, no doomed POSTs.
            if _env("HERMES_OTEL_LOGS_ENABLED").lower() in {"0", "false", "no", "off"}:
                _LOG_EMITTERS[identity] = OtelLogEmitter(None)
            else:
                try:
                    otel_logger = build_logger(
                        service_name=service_name,
                        otlp_endpoint=otlp_endpoint,
                        user_id=user_id,
                        extra_resource=extra_resource,
                    )
                    _LOG_EMITTERS[identity] = OtelLogEmitter(
                        otel_logger,
                        agent_name=agent_name,
                        capture_content=capture,
                    )
                except Exception:  # pragma: no cover - fail-open
                    logger.warning(
                        "traceverse_hermes_otel: dashboard log emitter init failed; "
                        "spans still emit",
                        exc_info=True,
                    )
                    _LOG_EMITTERS[identity] = OtelLogEmitter(None)
            _EMITTERS[identity] = emitter
        except Exception as exc:  # pragma: no cover - fail-open
            # Init failure is unexpected (bad endpoint, missing SDK piece): warn
            # so a misconfiguration is visible, but stay fail-open per the
            # observability contract — never raise out of a hook.
            logger.warning("traceverse_hermes_otel disabled: init failed: %s", exc, exc_info=True)
            _EMITTERS[identity] = _INIT_FAILED
            return None
        return emitter


def _get_log_emitter(kwargs: dict[str, Any] | None = None) -> Any | None:
    """Return this run's dashboard log emitter (or ``None``).

    Ensures the span emitter (and thus the log emitter) is built first; the log
    emitter is constructed inside ``_get_emitter``.
    """
    if _get_emitter(kwargs) is None:
        return None
    le = _LOG_EMITTERS.get(_run_identity(kwargs))
    if le is None or le is _INIT_FAILED:
        return None
    return le


def _session_id(kwargs: dict[str, Any]) -> str:
    return str(
        kwargs.get("session_id")
        or kwargs.get("task_id")
        or kwargs.get("parent_session_id")
        or "default"
    )


def _session_id_or_active(kwargs: dict[str, Any]) -> str:
    """Session id from kwargs, falling back to the last-opened turn's session.

    Per-call hooks (post_api_request / post_tool_call) don't always carry the
    session id; the log records still need it for resource.session.id grouping,
    so we fall back to the session whose ``invoke_agent`` turn is currently open.
    """
    explicit = (
        kwargs.get("session_id")
        or kwargs.get("task_id")
        or kwargs.get("parent_session_id")
    )
    if explicit:
        return str(explicit)
    with _ACTIVE_SESSION_LOCK:
        return _ACTIVE_SESSION_ID


def _open_turn_span(kwargs: dict[str, Any]) -> Any | None:
    """The session's open ``invoke_agent`` span, to parent per-call spans on explicitly."""
    session_id = _session_id_or_active(kwargs)
    with _OPEN_TURNS_LOCK:
        return _OPEN_TURN_SPANS.get(session_id)


def _delegate_goals(args: Any) -> list[Any]:
    """The goal of each task in delegate_task's arguments (a ``tasks`` list, or one ``goal``)."""
    if not isinstance(args, dict):
        return []
    tasks = args.get("tasks")
    if isinstance(tasks, list) and tasks:
        return [t.get("goal") if isinstance(t, dict) else None for t in tasks]
    if args.get("goal") is not None:
        return [args.get("goal")]
    return []


def _child_link(session_id: str) -> Any | None:
    with _LINKS_LOCK:
        return _CHILD_LINKS.get(session_id)


def _link_attributes(kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Sub-agent linkage attributes for a span of this hook's session, if it is a sub-agent."""
    link = _child_link(_session_id_or_active(kwargs))
    return link.attributes() if link is not None else None


def _remember_child(child_session_id: str, link: Any) -> None:
    with _LINKS_LOCK:
        _CHILD_LINKS[child_session_id] = link
        while len(_CHILD_LINKS) > _MAX_CHILD_LINKS:
            _CHILD_LINKS.pop(next(iter(_CHILD_LINKS)))


def _as_list(value: Any) -> list[Any] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _response_text(kwargs: Mapping[str, Any]) -> str | None:
    """Assistant text for a ``post_api_request`` call.

    Older hooks passed ``assistant_response`` (a string). Hermes v0.22 passes
    ``assistant_message`` (the message object, or a dict) instead, whose
    ``content`` is a string or a list of content blocks.
    """
    text = kwargs.get("assistant_response")
    if isinstance(text, str) and text:
        return text
    message = kwargs.get("assistant_message")
    if message is None:
        return None
    content = (
        message.get("content") if isinstance(message, Mapping) else getattr(message, "content", None)
    )
    if isinstance(content, str):
        return content or None
    if isinstance(content, Sequence):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, Mapping):
                if block.get("type", "text") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            elif isinstance(getattr(block, "text", None), str):
                parts.append(block.text)
        return "".join(parts) or None
    return None


def _usage_field(usage: Any, *names: str) -> Any:
    """Read the first present field from a usage dict or object."""
    for name in names:
        if isinstance(usage, dict):
            if usage.get(name) is not None:
                return usage[name]
        elif usage is not None:
            value = getattr(usage, name, None)
            if value is not None:
                return value
    return None


# ---------------------------------------------------------------------------
# Lifecycle hooks. All callbacks accept **kwargs for forward compatibility and
# never raise — observability must not break the agent (see CONTRIBUTING and the
# Build-a-Plugin guide). The body is gated on _get_emitter() being available.
# ---------------------------------------------------------------------------


def on_session_start(**kwargs: Any) -> None:
    """Track the active session and emit the ``session_start`` dashboard log.

    The ``invoke_agent`` span is **not** opened here. It is opened lazily on the
    first ``pre_llm_call`` of each turn and closed on ``transform_llm_output``,
    so it is **one span per turn** that exports at turn end (matching the
    documented span model). Opening it at ``on_session_start`` — which fires once
    per session — would make it session-scoped, so per-turn eval would only
    export on a clean session end and be lost on an abrupt shutdown.
    """
    emitter = _get_emitter(kwargs)
    if emitter is None:
        return
    session_id = _session_id(kwargs)
    with _ACTIVE_SESSION_LOCK:
        global _ACTIVE_SESSION_ID
        _ACTIVE_SESSION_ID = session_id
    # Dashboard log record (best-effort, never raises).
    log_emitter = _get_log_emitter(kwargs)
    if log_emitter is not None:
        try:
            log_emitter.session_start(
                session_id=session_id,
                model=kwargs.get("model"),
                user_prompt=kwargs.get("user_message"),
            )
        except Exception:
            logger.debug("on_session_start: failed to emit session_start log", exc_info=True)


def _end_turn(**kwargs: Any) -> None:
    """Close the ``invoke_agent`` span for a Hermes session, if open."""
    if _get_emitter(kwargs) is None:
        return
    session_id = _session_id(kwargs)
    with _OPEN_TURNS_LOCK:
        cm = _OPEN_TURNS.pop(session_id, None)
        _OPEN_TURN_SPANS.pop(session_id, None)
    with _LINKS_LOCK:
        _CHILD_LINKS.pop(session_id, None)
    _close_open_delegations(session_id)
    _close_cm(cm)
    _flush_turn()


def _close_open_delegations(session_id: str) -> None:
    """End any delegate_task span of this session that never saw its ``post_tool_call``."""
    with _LINKS_LOCK:
        keys = [k for k in _DELEGATIONS if k[0] == session_id]
        stale = [_DELEGATIONS.pop(k) for k in keys]
        if _LATEST_DELEGATION.get(session_id) in stale:
            _LATEST_DELEGATION.pop(session_id, None)
    for delegation in stale:
        try:
            delegation.span.end()
        except Exception:
            logger.debug("failed to end an unfinished delegate_task span", exc_info=True)


# on_session_end / on_session_finalize / on_session_reset all close the turn.
on_session_end = _end_turn
on_session_finalize = _end_turn
on_session_reset = _end_turn


def on_pre_llm_call(**kwargs: Any) -> None:
    """Open the per-turn ``invoke_agent`` span and stamp the prompt.

    Hermes fires ``pre_llm_call`` before each LLM call, carrying the turn's
    ``user_message`` (``on_session_start`` does not). The **first**
    ``pre_llm_call`` of a turn opens the turn span (stamping ``gen_ai.prompt``);
    later calls within the same turn (a tool loop fires it once per LLM call)
    reuse the already-open span. ``transform_llm_output`` closes the span at turn
    end, so it exports per turn — making prompt-side eval (PII / prompt-injection
    / restricted-topics on the developer's input) run and export every turn,
    resilient to an abrupt shutdown. No-op / never raises (observability must
    not break the agent).
    """
    emitter = _get_emitter(kwargs)
    if emitter is None:
        return
    session_id = _session_id_or_active(kwargs)
    with _OPEN_TURNS_LOCK:
        if session_id in _OPEN_TURNS:
            return  # same turn (tool-loop continuation) — keep the open span
    link = _child_link(session_id)
    if link is None and kwargs.get("parent_session_id"):
        # A sub-agent that subagent_start never announced (for example, its
        # delegate_task pre_tool_call did not fire): Hermes still names the
        # parent session here, so the ids are stamped even without a parent span.
        link = _ChildLink(None, str(kwargs["parent_session_id"]), None)
        _remember_child(session_id, link)
    try:
        cm = emitter.turn_span(
            session_id=session_id,
            model=kwargs.get("model"),
            user_prompt=kwargs.get("user_message"),
            extra=link.attributes() if link is not None else None,
            parent=link.parent_span if link is not None else None,
        )
        span = cm.__enter__()
        with _OPEN_TURNS_LOCK:
            _OPEN_TURNS[session_id] = cm
            _OPEN_TURN_SPANS[session_id] = span
    except Exception:
        logger.debug("on_pre_llm_call: failed to open turn span", exc_info=True)
    # Dashboard log record for the prompt (best-effort, never raises). The span
    # carries the prompt as gen_ai.prompt, but the /agent-coding dashboard is
    # built from log records — without a `user_prompt` record a Hermes session
    # shows tool calls and API responses but no prompts. Emitted once per turn:
    # the tool-loop-continuation guard above early-returns on later pre_llm_calls
    # of the same turn, so this line is only reached on the turn's first call.
    log_emitter = _get_log_emitter(kwargs)
    if log_emitter is not None:
        try:
            log_emitter.user_prompt(
                session_id=session_id,
                prompt=kwargs.get("user_message"),
                model=kwargs.get("model"),
            )
        except Exception:
            logger.debug("on_pre_llm_call: failed to emit user_prompt log", exc_info=True)


def on_transform_llm_output(**kwargs: Any) -> None:
    """Stamp the model's response onto the open ``invoke_agent`` span.

    Hermes delivers the final response text on ``transform_llm_output`` (fired
    once per turn after the tool loop, before the turn closes); the per-call
    ``post_api_request`` hook carries only token/finish metadata, not the text.
    Stamping it on the open turn span enables response-side evaluation (toxicity
    / hallucination / output PII) alongside the prompt-side eval from
    ``pre_llm_call``.

    This is a **read-only observer**: it returns ``None`` so the output text is
    never altered (the transform contract is "first non-empty string wins";
    returning None leaves it unchanged). It then **closes the turn span**, which
    exports it now — so prompt+response eval runs every turn and survives an
    abrupt shutdown. No-op when no turn is open. Never raises.
    """
    emitter = _get_emitter(kwargs)
    if emitter is None:
        return
    session_id = _session_id_or_active(kwargs)
    with _OPEN_TURNS_LOCK:
        cm = _OPEN_TURNS.pop(session_id, None)
        span = _OPEN_TURN_SPANS.pop(session_id, None)
    if span is None:
        return
    response_text = kwargs.get("response_text")
    try:
        if response_text:
            emitter.stamp_completion(span, response_text)
    except Exception:
        logger.debug("on_transform_llm_output: failed to stamp response", exc_info=True)
    # End the turn span -> it exports immediately and the eval enricher runs on
    # this turn's prompt + response.
    _close_cm(cm)
    _flush_turn()


def _close_cm(cm: Any) -> None:
    if cm is None:
        return
    try:
        cm.__exit__(None, None, None)
    except Exception:
        logger.debug("failed to close turn span", exc_info=True)


def _flush_turn() -> None:
    """Put the finished turn's spans + log records on the wire NOW.

    Hermes's one-shot CLI (``hermes -z``) exits via ``os._exit()``, which
    skips atexit and interpreter finalization — the batch processors never
    get their shutdown flush, so any span still queued at exit is silently
    lost (observed: ``invoke_agent`` and late-turn ``chat`` spans missing
    from one-shot runs). Called after every turn close; bounded (3s max per
    pipeline) and best-effort, never raises.
    """
    try:
        from . import provider as _provider

        _provider.flush_all()
    except Exception:
        logger.debug("turn-end flush failed", exc_info=True)


def on_post_api_request(**kwargs: Any) -> None:
    """Emit a ``chat`` span for a completed LLM API call.

    ``post_api_request`` fires once per provider/API call and carries the usage
    summary (CanonicalUsage dict) and finish reason — exactly the per-LLM-call
    enrichment fields the GenAI ``chat`` span wants.
    """
    emitter = _get_emitter(kwargs)
    if emitter is None:
        return
    try:
        usage = kwargs.get("usage")
        duration = kwargs.get("api_duration") or kwargs.get("duration_ms")
        call_duration_ms = None
        if duration is not None:
            try:
                # api_duration is seconds (langfuse rounds it as such); a
                # duration_ms is already ms. Normalise api_duration to ms.
                call_duration_ms = (
                    float(duration) * 1000.0
                    if kwargs.get("api_duration") is not None
                    else float(duration)
                )
            except (TypeError, ValueError):
                call_duration_ms = None
        # The same measured wall-clock value serves two purposes: it backdates
        # the chat span start (real span duration in waterfalls) and fills the
        # shared TTFT field (for non-streaming calls TTFT == full latency).
        ttft_ms = call_duration_ms
        input_tokens = _usage_field(usage, "input_tokens", "prompt_tokens")
        output_tokens = _usage_field(usage, "output_tokens", "completion_tokens")
        # Hermes' normalized usage reports prompt-cache buckets separately from
        # input_tokens; raw Anthropic usage uses the *_input_tokens names.
        cache_read_tokens = _usage_field(
            usage, "cache_read_tokens", "cache_read_input_tokens"
        )
        cache_creation_tokens = _usage_field(
            usage, "cache_write_tokens", "cache_creation_input_tokens"
        )
        cost_usd = kwargs.get("cost_usd") or kwargs.get("cost")
        # Local backends (Ollama/HF/vLLM) report no price. Mirror
        # genai-otel-instrument: estimate cost from model size + tokens so the
        # dashboard shows real cost, not $0 — only when the agent gave us none.
        if cost_usd is None:
            try:
                from .cost import estimate_cost_usd

                cost_usd = estimate_cost_usd(
                    kwargs.get("response_model") or kwargs.get("model"),
                    input_tokens,
                    output_tokens,
                )
            except Exception:
                logger.debug("cost estimation failed", exc_info=True)
        finish = _as_list(kwargs.get("finish_reason"))
        response_text = _response_text(kwargs)
        emitter.record_llm_call(
            request_model=kwargs.get("model"),
            response_model=kwargs.get("response_model"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            finish_reasons=finish,
            ttft_ms=ttft_ms,
            duration_ms=call_duration_ms,
            response_text=response_text,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            extra=_link_attributes(kwargs),
            parent=_open_turn_span(kwargs),
        )
    except Exception:
        logger.debug("on_post_api_request: failed to emit chat span", exc_info=True)
        return
    # Dashboard log record (best-effort).
    log_emitter = _get_log_emitter(kwargs)
    if log_emitter is not None:
        try:
            log_emitter.api_response(
                session_id=_session_id_or_active(kwargs),
                request_model=kwargs.get("model"),
                response_model=kwargs.get("response_model"),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                finish_reasons=finish,
                ttft_ms=ttft_ms,
                response_text=response_text,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
            )
        except Exception:
            logger.debug("on_post_api_request: failed to emit api_response log", exc_info=True)


def on_post_tool_call(**kwargs: Any) -> None:
    """Emit an ``execute_tool`` span for a completed tool call."""
    emitter = _get_emitter(kwargs)
    if emitter is None:
        return
    try:
        status = kwargs.get("status")
        error = kwargs.get("error")
        if error is None and isinstance(status, str) and status.lower() in {"error", "failed"}:
            error = status
        tool_name = str(kwargs.get("tool_name") or "unknown")
        tool_type = str(kwargs.get("tool_type") or "function")
        delegation = _pop_delegation(kwargs) if tool_name == DELEGATE_TOOL else None
        if delegation is not None:
            emitter.finish_tool_span(
                delegation.span,
                arguments=kwargs.get("args"),
                result=kwargs.get("result"),
                error=error,
                extra=_link_attributes(kwargs),
            )
        else:
            emitter.record_tool_call(
                tool_name=tool_name,
                tool_type=tool_type,
                arguments=kwargs.get("args"),
                result=kwargs.get("result"),
                error=error,
                duration_ms=kwargs.get("duration_ms"),
                extra=_link_attributes(kwargs),
                parent=_open_turn_span(kwargs),
            )
    except Exception:
        logger.debug("on_post_tool_call: failed to emit execute_tool span", exc_info=True)
        return
    # Dashboard log record (best-effort).
    log_emitter = _get_log_emitter(kwargs)
    if log_emitter is not None:
        try:
            log_emitter.tool_result(
                session_id=_session_id_or_active(kwargs),
                tool_name=tool_name,
                tool_type=tool_type,
                arguments=kwargs.get("args"),
                result=kwargs.get("result"),
                error=error,
            )
        except Exception:
            logger.debug("on_post_tool_call: failed to emit tool_result log", exc_info=True)


def _pop_delegation(kwargs: dict[str, Any]) -> Any | None:
    """Take the open delegate_task span that this ``post_tool_call`` closes."""
    session_id = _session_id_or_active(kwargs)
    with _LINKS_LOCK:
        delegation = _DELEGATIONS.pop((session_id, str(kwargs.get("tool_call_id") or "")), None)
        if delegation is None:
            # No tool_call_id match: take this session's newest open delegation.
            delegation = _LATEST_DELEGATION.get(session_id)
            if delegation is not None:
                for key, value in list(_DELEGATIONS.items()):
                    if value is delegation:
                        _DELEGATIONS.pop(key)
        if delegation is not None and _LATEST_DELEGATION.get(session_id) is delegation:
            _LATEST_DELEGATION.pop(session_id, None)
    return delegation


def on_pre_tool_call(**kwargs: Any) -> None:
    """Open delegate_task's ``execute_tool`` span before the tool runs.

    Every other tool span is emitted after the fact on ``post_tool_call``. A
    delegate_task call starts sub-agents while it runs, and their turns can only
    be children of its span if the span already exists, so it is opened here and
    closed on ``post_tool_call``. Read-only observer: always returns ``None``
    (``pre_tool_call`` is Hermes's policy hook; a return value could block or
    rewrite the call).
    """
    if kwargs.get("tool_name") != DELEGATE_TOOL:
        return
    emitter = _get_emitter(kwargs)
    if emitter is None:
        return
    try:
        session_id = _session_id_or_active(kwargs)
        span = emitter.start_tool_span(
            tool_name=DELEGATE_TOOL,
            tool_type=str(kwargs.get("tool_type") or "function"),
            parent=_open_turn_span(kwargs),
        )
        delegation = _Delegation(span, session_id, _delegate_goals(kwargs.get("args")))
        with _LINKS_LOCK:
            _DELEGATIONS[(session_id, str(kwargs.get("tool_call_id") or ""))] = delegation
            _LATEST_DELEGATION[session_id] = delegation
    except Exception:
        logger.debug("on_pre_tool_call: failed to open delegate_task span", exc_info=True)


def on_subagent_start(**kwargs: Any) -> None:
    """Link a starting sub-agent to the delegate_task call that started it.

    Hermes fires ``subagent_start`` once per child, on the delegating thread,
    inside the delegate_task call. Records the child's parent span, parent
    session and task index (used when the child's turn opens) and adds the
    child's session id to the parent's delegate_task span. Ids only, so this
    runs whether or not content capture is on.
    """
    if _get_emitter(kwargs) is None:
        return
    try:
        child_session_id = kwargs.get("child_session_id")
        if not child_session_id:
            return
        child_session_id = str(child_session_id)
        parent_session_id = str(kwargs.get("parent_session_id") or "")
        identity = _run_identity({"session_id": parent_session_id} if parent_session_id else None)
        child_ids: list[str] = []
        task_index = None
        with _LINKS_LOCK:
            delegation = _LATEST_DELEGATION.get(parent_session_id)
            if delegation is not None:
                task_index = delegation.claim_task_index(kwargs.get("child_goal"))
                delegation.child_session_ids.append(child_session_id)
                child_ids = list(delegation.child_session_ids)
        if delegation is not None:
            delegation.span.set_attribute("hermes.delegation.child_session_ids", child_ids)
            link = _ChildLink(delegation.span, parent_session_id, task_index, identity)
        else:
            # No open delegate_task span (another plugin vetoed the call, or the
            # child was spawned outside the tool): parent on the delegating turn.
            link = _ChildLink(
                _open_turn_span({"session_id": parent_session_id}), parent_session_id, None, identity
            )
        _remember_child(child_session_id, link)
    except Exception:
        logger.debug("on_subagent_start: failed to link sub-agent", exc_info=True)


def register(ctx) -> None:
    """Register the plugin's lifecycle hooks with Hermes.

    Called exactly once at startup. Mirrors the hook set used by the bundled
    ``observability/langfuse`` and ``observability/nemo_relay`` plugins:

    * session lifecycle    → ``invoke_agent`` span open/close
    * pre_llm_call         → stamp the user prompt onto the open turn span +
                             emit a ``user_prompt`` dashboard log record
    * transform_llm_output → stamp the model response onto the open turn span
    * post_api_request     → ``chat`` span (tokens, cost, finish reason, TTFT)
    * post_tool_call       → ``execute_tool`` span (tool name/type, errors)
    * pre_tool_call        → opens delegate_task's span before the tool runs
    * subagent_start       → links each sub-agent's turn to that span

    ``pre_llm_call`` and ``transform_llm_output`` are read-only observers (the
    latter returns ``None`` and never alters the output); together they put the
    prompt and response on the turn span so a conventions-aware backend can run
    prompt- and response-side evaluation when content capture is enabled.
    """
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("on_session_reset", on_session_reset)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("transform_llm_output", on_transform_llm_output)
    ctx.register_hook("post_api_request", on_post_api_request)
    ctx.register_hook("post_tool_call", on_post_tool_call)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)
    ctx.register_hook("subagent_start", on_subagent_start)


def reset_for_tests() -> None:
    """Drop the cached emitter and any open turns (tests/teardown only)."""
    global _ACTIVE_SESSION_ID
    with _LOCK:
        _EMITTERS.clear()
        _LOG_EMITTERS.clear()
    with _OPEN_TURNS_LOCK:
        _OPEN_TURNS.clear()
        _OPEN_TURN_SPANS.clear()
    with _LINKS_LOCK:
        _DELEGATIONS.clear()
        _LATEST_DELEGATION.clear()
        _CHILD_LINKS.clear()
        _SESSION_IDENTITIES.clear()
    with _ACTIVE_SESSION_LOCK:
        _ACTIVE_SESSION_ID = "default"
