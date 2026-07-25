# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

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
