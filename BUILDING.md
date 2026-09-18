# Building Edit Aja

The GitHub Actions workflow is the reference reproducible Windows build.

Build chain:

1. Clone Kdenlive at `c3d8a38c04470f6726b21485fc488f2cd2921654`.
2. Concatenate `patches/phase5.patch.bz2.b64.*`, decode/decompress it, verify SHA-256 `682be7bcb5afd875eb5f347ecd8f644be5fa1273e175c027ddd1d67491ac9a65`, and apply the patch with depth 2.
3. Run `scripts/apply_branding.py`.
4. Build/package with the custom `craft/editaja` KDE Craft blueprint.
5. Upload the installer/package and the exact corresponding-source archive.

The fork deliberately keeps several Kdenlive internal names and file extensions
for compatibility with existing projects, effects, translations, QML modules,
and the Phase 5 MCP/API namespace.

<!-- CI trigger: verify Windows build after KDE Craft PATH sanitization. -->
