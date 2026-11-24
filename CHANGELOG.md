# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [2.2.1] - 2026-11-24

### Fixed
- **The plugin can be enabled in several Hermes profiles at once.** Hermes builds one shared uv workspace for every
  profile's plugins and keeps a buildable plugin's declared name, so the same plugin in two profiles failed `uv lock`
  ("two workspace members are both named `hermes-otel-plugin`") and Hermes disabled it in every profile, while a
  plugin with no build system is renamed per profile. `pyproject.toml` no longer declares `[build-system]`.
  `pip install git+https://github.com/Mandark-droid/hermes-otel-plugin.git` and `python -m build` still work: they
  fall back to setuptools (PEP 517), and the wheel keeps the `hermes_agent.plugins` entry point.

## [2.2.0] - 2026-11-24

### Added
- **Gateway bot runs are told apart from CLI runs.** Two resource attributes, on
  every span and every log record of a run, sub-agents included:
  `hermes.gateway.platform` (`slack`, `telegram`, `discord`, ...) is present only
  when the messaging gateway drives the run, and `hermes.bot.profile` is the Hermes
  profile name whenever it is known. A CLI session never carries a platform, even
  on the same profile. Both are read from Hermes itself (the gateway's
  `HERMES_SESSION_PLATFORM` / `HERMES_SESSION_PROFILE` session variables, then the
  profile derived from the Hermes home), not from a process name.
- Only a human chat channel counts as a gateway run. A turn on a non-messaging surface (`local`, `api_server`,
  `webhook`, `desktop`, `tui`, `cli`, ...) carries no `hermes.gateway.platform`, even though the gateway process
  serves it: a `hermes chat` turn is not a bot conversation. The set is Hermes' own
  `NON_MESSAGING_SESSION_SURFACES`, with a copy as fallback.
- A sub-agent takes its delegating run's identity, read on the delegating thread,
  because Hermes runs sub-agents on worker threads that do not carry the gateway's
  context.

### Changed
- The plugin builds one tracer and logger provider per run identity (gateway
  platform, profile), on first use, because a resource belongs to a provider. A CLI
  process still builds one pair; a gateway serving two platforms builds two.
- A run that carries a gateway platform uses the plain OpenTelemetry SDK provider
  even when `genai-otel-instrument` is installed, since that library owns one
  process-wide resource. A profile alone still goes through it.
- `provider.build_tracer` / `build_logger` accept `extra_resource`.

## [2.1.0] - 2026-11-18

### Added
- **Sub-agents are linked to the run that delegated them.** A Hermes sub-agent
  started by `delegate_task` runs as its own session, and until now its spans
  formed a separate trace with nothing pointing back at the parent: the
  `delegate_task` span was only created after the tool returned, and the plugin
  did not listen to Hermes's `subagent_start` hook. The plugin now opens the
  `delegate_task` `execute_tool` span on `pre_tool_call` and closes it on
  `post_tool_call`, and each sub-agent's `invoke_agent` span is its child, so the
  parent and its sub-agents form one trace. The parent is set explicitly, so the
  link holds on Hermes's worker threads.
- Link attributes, ids only and emitted whether or not content capture is on:
  `hermes.parent_session_id` and `hermes.delegation.task_index` on every span of
  a sub-agent, and `hermes.delegation.child_session_ids` on the parent's
  `delegate_task` span. The task index is matched on the task's goal, so it stays
  right when Hermes skips a refused task. A sub-agent that `subagent_start` never
  announced still carries `hermes.parent_session_id`, read from `pre_llm_call`.
- New hooks: `pre_tool_call` (a read-only observer that acts only on
  `delegate_task` and always returns `None`) and `subagent_start`.
  7 new tests (57 total): parallel sub-agents, a failed sub-agent, content
  capture on and off, an unannounced sub-agent, other tools unaffected, and a
  `delegate_task` span closed when its session ends without `post_tool_call`.

## [2.0.1] - 2026-11-06

### Fixed
- **`chat` and `execute_tool` spans nest under the turn again.** They relied on the
  implicit current OTel span, but Hermes can fire `post_api_request` / `post_tool_call`
  on a different thread or context from `pre_llm_call`, where the turn's
  `invoke_agent` span was opened. There the context is empty, so every per-call
  span became its own one-span trace and no waterfall formed (seen on Hermes
  v0.21.6 with a local Jaeger: 43 traces of 1 span each from 5 turns). Per-call
  spans are now parented explicitly on the session's open `invoke_agent` span.
  Regression test fires the hooks from a separate thread (50 tests).

## [2.0.0] - 2026-10-23

### Changed
- **Renamed the Hermes plugin key** from `otel` to `traceverse_hermes_otel` to avoid collisions with other Hermes plugins. Existing users should replace `otel` with `traceverse_hermes_otel` in `plugins.enabled` after upgrading.

### Added
- `HERMES_OTEL_LOGS_ENABLED` — set `false` to skip the dashboard log pipeline
  entirely (no LoggerProvider, no exporter, no records) when the OTLP backend
  is traces-only and answers `/v1/logs` with 404. Spans are unaffected; unset
  or truthy keeps today's behavior. 2 new adapter tests (49 total).

## [1.1.1] - 2026-07-25

### Fixed
- **One-shot runs no longer lose spans.** Hermes's `-z` CLI exits via
  `os._exit()`, which skips `atexit` and interpreter finalization — the batch
  processors never got their shutdown flush, so the `invoke_agent` span and
  any late-turn `chat`/`execute_tool` spans of a one-shot run were silently
  dropped from the export queue (validated live against Hermes v0.19.0). The
  plugin now force-flushes both pipelines at every turn close
  (`transform_llm_output`) and session end — bounded at 3s per pipeline,
  best-effort, fail-open.

## [1.1.0] - 2026-07-25

### Added
- **Real span durations.** `chat` and `execute_tool` spans are backdated by the
  wall-clock duration Hermes measures for each call (`api_duration` on
  `post_api_request`, `duration_ms` on `post_tool_call`), so trace waterfalls
  show the true call windows instead of zero-length ticks at the completion
  instant. All three span types now carry real durations.
- `README`: hero branding, real Jaeger waterfall + span-detail screenshots,
  machine-readable **For AI agents** contract section.
- `llms.txt` for AI-agent discovery.
- 4 new duration tests (43 total).

## [1.0.0] - 2026-07-15

### Added
- Initial standalone release, extracted from the in-tree
  `observability/otel` plugin (hermes-agent#48184) per Hermes's
  plugin-contribution policy.
- GenAI-convention spans: `invoke_agent` / `chat` / `execute_tool`.
- Dashboard-shaped OTLP log records: `user_prompt` / `session_start` /
  `api_response` / `tool_result`.
- Local-model cost estimation (model size → price tier).
- Privacy-gated content capture (`HERMES_OTEL_CAPTURE_CONTENT`, off by default).
- Fail-open provider bootstrap (`genai_otel` preferred, plain OTel SDK fallback).
