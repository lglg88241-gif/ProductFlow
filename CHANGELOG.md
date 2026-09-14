# Changelog

All notable changes for ProductFlow are recorded here.

## Unreleased

### Added

- **Template-style generation (P2)**: `generate_image` accepts `template_asset_id`;
  the analyzed template profile (layout, palette, typography, copy slots, mood) is
  injected into the generation prompt so a selected/uploaded template can be replicated
  in style without copying its text or brand marks.
- **Moments grid export (P2)**: slice any asset into WeChat Moments tiles
  (3x3 / 2x2 / 3x1 / 1x3) and download as an ordered zip; exposed as a REST endpoint
  and an agent tool (`export_moments_grid`) with an inline download card in the workbench.
- **Streaming designer agent (P0)**: new SSE endpoint
  `POST /api/agent/sessions/{id}/messages/stream` emits `stage` / `message` /
  `tool_start` / `tool_result` / `error` / `done` frames; the workbench renders
  them live (stage badge, inline copy proposals and image cards, per-message
  stream) instead of a silent wait. LLM context now carries only the last 20
  messages (truncated at the first complete tool-call group) to cut latency and
  token cost. Tool-first guard: when the model replies without calling any tool
  in the first round, a one-shot system nudge retries until tools are used.
- **Env-first provider configuration**: `.env` is now the authoritative source for
  self-provided APIs — precedence is `.env` (`AGENT_*` / `IMAGE_*`) > UI bindings > mock.
  Relay-friendly: no capability gates, same-profile fallback allowed, defaults
  `grok-4.6` + `gemini-3.8-flash` fallback and `gpt-image-2` image model.
- **Login gate toggle**: `ADMIN_ACCESS_REQUIRED=false` in `.env` disables the login
  gate entirely (internal-tool mode); default stays secure.
- **Provider purpose classification**: new `agent` provider binding purpose, independent
  from text (copy) and image (gpt-image-2) bindings. Agent bindings target
  OpenAI-compatible chat providers (e.g. xAI grok-4.6) with an optional fallback
  provider + model (e.g. gemini-3.8-flash) that takes over automatically when the
  primary fails. Settings page gains an Agent section with fallback configuration.
- **Designer Agent (M3)**: `recommend_designs` produces 2-3 recommendation cards
  (retrieved templates + per-candidate rationale, selectable in the workbench);
  `write_copy_report` delivers a full copy report (headline, Moments caption, selling
  points, hashtags, publishing tips); generated assets are auto-tagged by the vision
  model on save; the full novice-user acceptance script (clarify → recommend → copy →
  generate → archive) is now a deterministic replay test.
- **Designer Agent (M2)**: asset library — new `asset_library` table (migration 0032) with
  kinds (template/reference/output/brand), vision auto-annotation, and session traceability;
  template image upload with structured visual analysis (layout/palette/copy slots/mood);
  lightweight tag-based semantic search; three new agent tools (`search_assets`,
  `analyze_template`, `save_asset`); the three built-in moments templates seed the library
  idempotently at startup; workbench gains template upload, asset-library panel, and
  match/profile cards.
- **Designer Agent (M1)**: conversational design assistant — an LLM tool-calling loop that
  talks users through creating images without any prompt engineering. Guided dialogue
  stages (clarify → recommend → produce → review), `generate_image` / `edit_image` /
  `write_copy` tools reusing the durable generation queue, and a new Design Workbench page
  (`/workbench`) with chat UI, inline copy proposals, generated-image previews, and
  linked image-session polling. Backed by new `agent_sessions` / `agent_messages` tables
  (migration 0031); requires an OpenAI-compatible text provider.
- Product requirements & architecture spec (`docs/superpowers/specs/2026-09-06-agentic-designer-design.md`).
- Moments poster templates, poster prompt context, and copy inputs (text, file, and image OCR extraction).
- Image session messages persistence (migration 0030) with branching-friendly history.
- Dedicated product-create page and image-chat composer components.
- End-to-end operator journey smoke test covering login, product creation, workflow runs, deliverable downloads, gallery, runtime config, deletion, and logout.
- GitHub Actions CI with coverage gate (`--cov-fail-under=88`, baseline 90%), ruff, vitest, build, and pip-audit / pnpm audit dependency-vulnerability gates.

### Changed

- Runtime settings reads are cached for 3 seconds; ORM writes to app settings invalidate the cache automatically.
- Settings export redacts provider API keys (`__redacted__<last4>`); re-import keeps existing keys for matching profile ids and stores none for unknown profiles.
- `SESSION_COOKIE_SECURE` now defaults by environment when unset: enabled in production, disabled in development.
- Product screenshots and hero image compressed to 1600px/256-color without changing references.

### Security

- Production environments (`APP_ENV=production`) reject disabling the admin access key through runtime config or settings import.
- `/healthz` now reports `admin_access_required`; startup logs warn when auth is disabled or secure cookies are off.
- Docker Compose publishes Postgres/Redis host ports on 127.0.0.1 only.

### Fixed

- **Relay resilience (P1)**: image provider retries empty payloads (HTTP 200 with
  neither `b64_json` nor `url`) and retries remote image downloads; the designer
  agent retries transient gateway failures (5xx / connection errors) once before
  failing over, while 4xx input errors and timeouts are never retried.
- Image provider now accepts relay stations that return image URLs instead of
  `b64_json` (downloads the remote image transparently).
- Thumbnail variant generation silently falling back to originals on Windows when long asset stems pushed temp-file paths past MAX_PATH.

## 0.1.0 - 2026-05-02

Initial public self-hosted release for ProductFlow. This entry is the durable release record for `v0.1.0`.

### Added

- Single-admin, self-hosted product creative workspace with access-key login and Cookie-session API access.
- Product list, product creation, product detail workbench, source/reference image upload, and controlled download routes.
- ProductFlow workbench DAG for product context, reference images, copy generation, and image generation.
- Persistent workflow nodes, edges, runs, node-run state, failure reasons, startup recovery, and lightweight workflow status polling.
- Copy generation, editable copy fields, copy confirmation, product history, template poster output, and remote image-provider poster output.
- Reference-image single-slot semantics: manual uploads or generated-image fills replace the current slot image while older assets remain in product history/assets.
- Standalone iterative image sessions with durable generation tasks, queue position, retry/failure state, multiple generated candidates, and product attachment.
- Generated image gallery at `/gallery` for saved iterative-image results with source, product, prompt, size, model, and download metadata.
- Runtime settings page for provider/model selection, image sizes, upload limits, retry/concurrency controls, prompt templates, login gate, business deletion switch, and secrets that are not echoed back.
- Docker Compose self-hosting path for PostgreSQL, Redis, FastAPI backend, Dramatiq worker, and nginx-served Web build.
- Release helpers: `just release-dry-run` for safe validation and `just release` for Compose rebuild/start plus health checks.
- Chinese and English public docs for README, PRD, architecture, roadmap, and user guide.

### Release Boundaries

- ProductFlow 0.1.0 is not a hosted SaaS, public registration system, multi-tenant platform, or team-permission product.
- No hosted model accounts, billing, store authorization, automatic ad/listing pipeline, or video-generation workflow is included.
- No published container image, Helm chart, Kubernetes manifest, or cloud deployment package is included in `v0.1.0`.
- Docker volumes are not deleted by release helpers; `docker compose down -v` is only a manual reset command.

### Verification

Release preparation for `v0.1.0` used the lightweight documentation and build gates:

- `just release-dry-run`
- `just backend-test`
- `just web-build`
- `git diff --check`

The production update entrypoint remains `just release`, which should only be run intentionally on the deployment host.
