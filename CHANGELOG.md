# Changelog

All notable changes for ProductFlow are recorded here.

## Unreleased

### Added

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
