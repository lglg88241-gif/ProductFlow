# Changelog

All notable changes for ProductFlow are recorded here.

## Unreleased

### Added

- **Backup, restore and recovery drill (P1)**: `scripts/backup.sh` snapshots Postgres
  (`pg_dump -Fc`), the media volume, and the Redis RDB into `backups/<timestamp>/` with a
  sha256 manifest and retention pruning; `scripts/restore.sh` restores DB and/or storage
  with checksum verification and an explicit confirmation gate; `scripts/backup_drill.sh`
  proves restorability by restoring into a throwaway Postgres container (validating table
  count, migration revision against the manifest, and key row counts) without touching
  production. Wired as `just backup` / `just backup-drill` / `just restore <dir>`; procedure
  and migration-rollback guidance in `docs/BACKUP_RESTORE.md`.
- **Upload → style-replication loop**: `POST /api/agent/assets` accepts an optional
  `agent_session_id`; when present the backend vision-tags the upload (best-effort) and
  appends a context note to that session carrying the asset id, so the agent knows about a
  freshly uploaded template on the next turn instead of the user having to describe it.
  The workbench passes the active session id and refreshes the transcript.

### Changed

- **Single agent-turn implementation**: `run_agent_turn` (returns a result object) is now a
  thin adapter over the same core generator that powers `run_agent_turn_events` (SSE frames).
  Previously the two were near-duplicate code and had already drifted apart (`loop.py` is
  ~50 lines shorter). A regression test asserts both paths produce identical tools and stage
  for the same script.
- Legacy flaky SSE heartbeat test replaced with a deterministic one (it asserted on the
  first frame, which may legitimately be a heartbeat comment when the pump thread starts
  slower than the heartbeat interval).

- **Multi-candidate generation (A)**: the agent's `generate_image` accepts `count` (1–4,
  default 2) and returns `candidates` (asset id / url / label) with `primary_url`; when the
  durable queue is still working it reports `pending` + `expected_candidates`. The workbench
  renders a side-by-side candidate card with download and "continue from this one" actions.
- **Copy report persistence (E)**: `write_copy_report` now strips code fences, tolerates
  `content`/`sections` shapes, persists a `CopyReport` row (migration `20260914_0034`) and
  returns `report_id` + `download_url`; new `GET /api/agent/copy-reports/{id}/download`
  serves the markdown as an attachment (RFC 5987 UTF-8 filenames). The workbench gains a
  report card with preview and download.
- **Localized text re-render (B)**: new `rerender_poster_copy` agent tool rebuilds
  `PosterGenerationInput` from the stored product + latest copy set, applies partial copy
  overrides, re-renders locally with the PIL poster renderer (no image-model quota), and
  stores a new poster variant with a download URL. Prompt guidance routes text-only edits
  here and visual edits to `edit_image` / `generate_image` (result-feedback editing).
- **Token usage observability**: agent LLM responses carry parsed `usage`; assistant
  messages persist `prompt_tokens` / `completion_tokens` / `total_tokens` (migration
  `20260914_0033`); new `GET /api/metrics/summary` aggregates totals and a 7-day daily
  breakdown. LLM calls log model + latency; fallback switchovers log a warning
  (with the primary failure reason, secrets scrubbed).
- **Storage lifecycle**: new `storage_cleanup` module finds DB-unreferenced orphan files
  and stale exports (never deleting referenced files; dry-run by default). The worker runs
  it on a daemon thread (initial delay 10 min, default every 24 h via
  `MEDIA_CLEANUP_INTERVAL_SECONDS`).
- **Queue reconciliation loop**: the worker re-runs unfinished-task recovery every 30 min
  (env `RECONCILE_INTERVAL_SECONDS`) so messages stuck mid-run no longer wait for a restart.
- **SSE heartbeat**: the agent stream emits `: ping` comment frames via a producer thread
  while tools run; nginx reads timeout raised to 300 s with buffering off, and the browser
  parser skips comment frames.

- **Product pipeline as agent tools (P2-10)**: the frozen product workflow is now
  reachable conversationally — `run_product_pipeline` creates a product from a
  library image and submits the full workflow (understanding → copy → image),
  and `check_pipeline_status` reports run status, posters with download URLs,
  and failure reasons. The workflow internals remain untouched (freeze upheld);
  the agent is just a new entry point.
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
- Remote image downloads are funneled through an SSRF-guarded fetcher: http/https only,
  per-hop DNS resolution rejecting private/loopback/link-local/reserved/multicast targets
  (IPv4+IPv6), manual redirect re-validation, 30 MB streaming cap (`REMOTE_FETCH_ALLOW_PRIVATE_NETWORK=1`
  escape hatch for local dev).
- Production startup fails fast when `ADMIN_ACCESS_REQUIRED=false` (development keeps the
  warning-only behavior). `/api/auth/session` rate-limits failed key attempts per IP
  (6th within 15 min → 429 + Retry-After). `/healthz` provider summary no longer exposes
  relay host/base URLs (keeps kind / model / has_key / fallback presence).
- Docker Compose publishes the backend port on 127.0.0.1 only (nginx is the same-origin
  entrypoint) and the worker now receives the full `AGENT_*` set.

### Fixed

- **SSE behind nginx**: agent streams were cut at nginx's default 60 s idle timeout during
  long tool executions (Docker deployments only); heartbeat frames + `proxy_read_timeout 300s`
  keep the stream alive for the full turn.
- **Orphan tool_calls poisoning sessions**: a client disconnect between the two history
  commits could leave an assistant tool_calls message without its tool results, making every
  subsequent request fail with a 400 forever. Message building now skips incomplete
  tool-call groups (stateless self-heal).
- **Unbounded image generation waits**: image provider clients now carry explicit
  httpx timeouts and the background poll enforces an overall deadline
  (`IMAGE_GENERATION_PROVIDER_TIMEOUT_SECONDS`, default 300 s); the worker failsafe
  time limit dropped from 24 h to 1 h (env-overridable).
- Docker Compose default drift: worker `IMAGE_GENERATE_MODEL` default unified to
  `gpt-image-2`.

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
