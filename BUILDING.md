# Building Edit Aja

The GitHub Actions workflow is the reference reproducible Windows build.

Build chain:

1. Clone Kdenlive at `c3d8a38c04470f6726b21485fc488f2cd2921654`.
2. Decode and apply `patches/phase5.patch.gz.b64` with patch depth 2.
3. Run `scripts/apply_branding.py`.
4. Build/package with the custom `craft/editaja` KDE Craft blueprint.
5. Upload the installer/package and the exact corresponding-source archive.

The fork deliberately keeps several Kdenlive internal names and file extensions
for compatibility with existing projects, effects, translations, QML modules,
and the Phase 5 MCP/API namespace.
