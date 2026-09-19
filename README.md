# Edit Aja

**Edit Aja** is an open-source AI-assisted video editor fork built on Kdenlive.
This repository is intentionally small and reproducible: it pins the exact
upstream Kdenlive revision, applies the public Phase 5 agent patch, then applies
Edit Aja branding and packages the result.

## What is included

- Phase 1–5 AI-agent modifications.
- Built-in OpenAI-compatible API agent.
- MCP and localhost REST/JSON bridges.
- Shared native editing tool registry.
- Local `editaja.film-context` JSON summary for project/timeline/media structure, exposed as `kdenlive_get_film_context` without uploading the movie.
- Edit Aja branding and Windows icon.
- Reproducible Windows build workflow.

The internal `kdenlive_*` agent tool names are kept for compatibility. They do
not mean the user-facing product is still branded as Kdenlive.

## Upstream and license

Edit Aja is based on Kdenlive and preserves its GPL licensing and upstream
copyright notices. The build is pinned to upstream Kdenlive commit:

`c3d8a38c04470f6726b21485fc488f2cd2921654`

Upstream: https://github.com/KDE/kdenlive

The Phase 5 modifications are stored in compressed/base64 form under `patches/`
so they remain reviewable, portable, and easy to reapply. GitHub Actions decodes
the patch during the build. A corresponding-source archive is produced alongside
the Windows installer artifact.

## Repository layout

- `patches/phase5.patch.bz2.b64.*` — chunked, compressed Phase 1–5 source changes.
- `patches/film-context.patch` — optional local-first project/timeline/media JSON context layer for AI agents.
- `scripts/apply_branding.py` — Edit Aja user-facing branding.
- `branding/` — Edit Aja Windows icon assets, stored as base64 text.
- `craft/editaja/editaja.py` — custom KDE Craft package blueprint.
- `.github/workflows/build-windows.yml` — Windows build/package workflow.

## Build status

Windows builds run through GitHub Actions. Before compiling, the workflow reassembles the Phase 5 chunks and verifies the exact patch SHA-256 (`682be7bcb5afd875eb5f347ecd8f644be5fa1273e175c027ddd1d67491ac9a65`). It then uses KDE Craft and publishes the Windows package plus corresponding source as build artifacts.
