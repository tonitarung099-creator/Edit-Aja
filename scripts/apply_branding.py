#!/usr/bin/env python3
"""Apply Edit Aja branding on top of the Phase 5 Kdenlive-based source tree.

This deliberately keeps Kdenlive's internal technical identifiers and file format
compatibility where changing them would risk breaking the editor. User-facing
branding, window/about text and Windows icons are changed to Edit Aja.
"""
from __future__ import annotations

import argparse
import base64
import re
import struct
from pathlib import Path

APP_NAME = "Edit Aja"
REPO_URL = "https://github.com/tonitarung099-creator/Edit-Aja"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def replace(path: Path, old: str, new: str, *, required: bool = True) -> None:
    text = read_text(path)
    if old not in text:
        if required:
            raise RuntimeError(f"Expected text not found in {path}: {old[:120]!r}")
        return
    write_text(path, text.replace(old, new))


def replace_regex(path: Path, pattern: str, repl: str, *, flags: int = 0) -> None:
    text = read_text(path)
    updated, count = re.subn(pattern, repl, text, flags=flags)
    if count == 0:
        raise RuntimeError(f"Pattern did not match in {path}: {pattern}")
    write_text(path, updated)


def decode_logo(asset_dir: Path) -> bytes:
    return base64.b64decode((asset_dir / "editaja-logo.png.b64").read_text(encoding="ascii"))


def png_to_ico(png: bytes, width: int = 64, height: int = 64) -> bytes:
    # ICO supports PNG-compressed image payloads. A single 64x64 entry keeps the
    # public branding asset compact and requires no non-stdlib Python package.
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", width if width < 256 else 0, height if height < 256 else 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def apply(root: Path, asset_dir: Path) -> None:
    root = root.resolve()
    asset_dir = asset_dir.resolve()

    # Windows/app runtime icons. Keep the historical filenames because several
    # Kdenlive build/packaging paths refer to them internally.
    logo_png = decode_logo(asset_dir)
    (root / "data/icons/256-apps-kdenlive.png").write_bytes(logo_png)
    (root / "data/icons/kdenlive.ico").write_bytes(png_to_ico(logo_png))
    (root / "data/pics/kdenlive-logo.png").write_bytes(logo_png)

    # Use our 256 px icon for the embedded Qt resource shown in the main window.
    replace(root / "src/icons.qrc", "../data/icons/48-apps-kdenlive.png", "../data/icons/256-apps-kdenlive.png")

    main = root / "src/main.cpp"
    replace(
        main,
        'QString otherText = i18n("Please report bugs to <a href=\\"%1\\">%2</a>", QStringLiteral("https://bugs.kde.org/enter_bug.cgi?product=kdenlive"),\n                             QStringLiteral("https://bugs.kde.org/"));',
        'QString otherText = i18n("Edit Aja is an open-source fork based on Kdenlive. Source and project issues: <a href=\\"%1\\">%1</a>",\n                             QStringLiteral("https://github.com/tonitarung099-creator/Edit-Aja"));',
    )
    replace(
        main,
        'KAboutData aboutData(QByteArray("kdenlive"), i18n("Kdenlive"), KDENLIVE_VERSION, i18n("An open source video editor."), KAboutLicense::GPL_V3,\n                         i18n("Copyright © 2007–2025 Kdenlive authors"), otherText, QStringLiteral("https://kdenlive.org"));',
        'KAboutData aboutData(QByteArray("kdenlive"), i18n("Edit Aja"), KDENLIVE_VERSION, i18n("Open-source AI-assisted video editor based on Kdenlive."), KAboutLicense::GPL_V3,\n                         i18n("Kdenlive core © 2007–2026 Kdenlive authors; Edit Aja modifications © 2026 Edit Aja contributors"), otherText,\n                         QStringLiteral("https://github.com/tonitarung099-creator/Edit-Aja"));',
    )
    # Preserve upstream author/credit history and add fork attribution.
    marker = '    aboutData.addCredit(i18n("Massimo Stella"), i18n("Core team member, expert advice, testing"));\n'
    insertion = marker + '    aboutData.addCredit(i18n("Edit Aja contributors"), i18n("AI-agent integration, familiar workspace, fork branding"));\n'
    replace(main, marker, insertion)

    # Splash/user-facing messages. Technical/internal kdenlive_* API tool names stay unchanged.
    for rel in ("src/dialogs/Splash.qml", "src/dialogs/Simplesplash.qml"):
        path = root / rel
        text = read_text(path)
        text = text.replace('KI18n.i18n("Kdenlive")', 'KI18n.i18n("Edit Aja")')
        text = text.replace("Kdenlive crashed on last start.", "Edit Aja crashed on last start.")
        text = text.replace("Kdenlive was upgraded.", "Edit Aja was upgraded.")
        write_text(path, text)

    # AI panel: display Edit Aja while retaining kdenlive_* tool identifiers for
    # backwards compatibility with Phase 5 MCP/API clients.
    ai = root / "src/aiassistant/aiassistantwidget.cpp"
    text = read_text(ai)
    text = text.replace("control Kdenlive through MCP", "control Edit Aja through MCP")
    text = text.replace("all Kdenlive actions", "all Edit Aja actions")
    text = text.replace("exposed by Kdenlive", "exposed by Edit Aja")
    text = text.replace("List Kdenlive actions available to the agent", "List Edit Aja actions available to the agent")
    text = text.replace("Trigger any registered Kdenlive QAction", "Trigger any registered Edit Aja QAction")
    text = text.replace("Open a Kdenlive dock panel", "Open an Edit Aja dock panel")
    write_text(ai, text)

    agent = root / "src/aiassistant/openaicompatibleagent.cpp"
    text = read_text(agent)
    text = text.replace("AI video-editing agent embedded in Kdenlive", "AI video-editing agent embedded in Edit Aja")
    text = text.replace("Timeline frame exported by Kdenlive", "Timeline frame exported by Edit Aja")
    write_text(agent, text)

    # Desktop entry names (mainly Linux; harmless for Windows builds).
    desktop = root / "data/org.kde.kdenlive.desktop"
    text = read_text(desktop)
    text = re.sub(r"^Name(?:\[[^\]]+\])?=.*$", lambda m: m.group(0).split("=", 1)[0] + "=Edit Aja", text, flags=re.MULTILINE)
    write_text(desktop, text)

    # AppStream metadata. Keep the upstream application ID to avoid changing
    # deep integration identifiers in this first branded build.
    appdata = root / "data/org.kde.kdenlive.appdata.xml"
    text = read_text(appdata)
    text = re.sub(r"<name(?:\s+xml:lang=\"[^\"]+\")?>.*?</name>", lambda m: re.sub(r">.*?</", ">Edit Aja</", m.group(0)), text)
    text = text.replace("<developer_name translate=\"no\">KDE</developer_name>", "<developer_name translate=\"no\">Edit Aja contributors</developer_name>")
    text = text.replace(
        "<p>Kdenlive is a video editing application with support for many audio and video formats. It offers advanced editing features, a variety of effects and transitions, color correction, audio post-production and subtitling tools. Additionally, it provides the flexibility to render into practically any format of your choice.</p>",
        "<p>Edit Aja is an open-source AI-assisted video editor based on Kdenlive. It keeps Kdenlive's mature editing engine while adding a familiar workspace and provider-neutral AI agent access through API, REST and MCP.</p>",
    )
    write_text(appdata, text)

    # macOS bundle display names are also made consistent even though Phase 5
    # packaging is currently focused on Windows.
    cmake = root / "src/CMakeLists.txt"
    text = read_text(cmake)
    text = text.replace('MACOSX_BUNDLE_DISPLAY_NAME "Kdenlive"', 'MACOSX_BUNDLE_DISPLAY_NAME "Edit Aja"')
    text = text.replace('MACOSX_BUNDLE_BUNDLE_NAME "Kdenlive"', 'MACOSX_BUNDLE_BUNDLE_NAME "Edit Aja"')
    text = text.replace('MACOSX_BUNDLE_LONG_VERSION_STRING "Kdenlive ${KDENLIVE_VERSION}"', 'MACOSX_BUNDLE_LONG_VERSION_STRING "Edit Aja ${KDENLIVE_VERSION}"')
    write_text(cmake, text)

    # Fork README: clear attribution and open-source/reproducible source chain.
    readme = f"""# Edit Aja\n\nEdit Aja is an open-source AI-assisted desktop video editor based on **Kdenlive**.\nThe project keeps Kdenlive/MLT as the editing foundation and adds the Phase 1–5\nworkspace and agent work developed for this fork.\n\n## AI agent\n\nThe Phase 5 agent exposes a shared editing tool registry to:\n\n- the built-in OpenAI-compatible API client,\n- external MCP hosts, and\n- a localhost REST/JSON API.\n\nThe tool identifiers intentionally keep the `kdenlive_*` prefix for compatibility\nwith existing Phase 5 clients. This is an internal API namespace; the application\nbrand shown to users is **Edit Aja**.\n\n## Open source and upstream\n\nEdit Aja is distributed under the GPL terms inherited from Kdenlive. The original\nKdenlive copyright and license notices remain in the source. Kdenlive's upstream\nproject is maintained by the KDE community.\n\nThis fork is reproducible from a pinned upstream Kdenlive commit plus the public\nPhase 5 patch and branding script in:\n\n{REPO_URL}\n\nDo not remove upstream copyright/license notices when redistributing modified\nbuilds. See `COPYING` and the SPDX headers throughout the source tree.\n\n## Build\n\nThe public GitHub repository contains a Windows build workflow. Each build also\nproduces a corresponding-source archive so the exact modified source used for the\nbinary remains available.\n"""
    write_text(root / "README.md", readme)

    branding_note = """# Edit Aja branding\n\nEdit Aja is a GPL-licensed fork based on Kdenlive. Internal identifiers are kept\nwhere changing them would risk compatibility. User-facing names and Windows icon\nassets are replaced by the public branding script at build time.\n\nThe Edit Aja logo asset in the fork repository is intended for this open-source\nproject. Upstream Kdenlive copyright and SPDX license notices remain intact.\n"""
    write_text(root / "EDIT_AJA_BRANDING.md", branding_note)

    print(f"Applied Edit Aja branding to {root}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("source", type=Path)
    p.add_argument("--assets", type=Path, default=Path(__file__).resolve().parents[1] / "branding")
    args = p.parse_args()
    apply(args.source, args.assets)


if __name__ == "__main__":
    main()
