import subprocess
import sys
from pathlib import Path

import info
from Blueprints.CraftPackageObject import CraftPackageObject
from CraftCore import CraftCore
from Packager.NullsoftInstallerPackager import NullsoftInstallerPackager


UPSTREAM_COMMIT = "c3d8a38c04470f6726b21485fc488f2cd2921654"


class subinfo(info.infoclass):
    def setTargets(self):
        # url|branch|revision: an empty branch plus the full SHA pins the exact
        # upstream source used when Phase 5 was created.
        self.svnTargets["editaja"] = f"https://github.com/KDE/kdenlive.git||{UPSTREAM_COMMIT}"
        self.patchToApply["editaja"] = [("phase5.patch", 2)]
        self.defaultTarget = "editaja"
        self.description = "Open-source AI-assisted video editor based on Kdenlive"
        self.webpage = "https://github.com/tonitarung099-creator/Edit-Aja"
        self.displayName = "Edit Aja"

    def setDependencies(self):
        self.buildDependencies["kde/frameworks/extra-cmake-modules"] = None
        self.runtimeDependencies["libs/qt/qtbase"] = None
        self.runtimeDependencies["libs/qt/qtmultimedia"] = None
        self.runtimeDependencies["libs/qt/qtspeech"] = None
        self.runtimeDependencies["libs/qt/qtimageformats"] = None
        self.runtimeDependencies["libs/qt/qtdeclarative"] = None
        self.runtimeDependencies["libs/qt/qtnetworkauth"] = None
        self.runtimeDependencies["kde/frameworks/tier1/breeze-icons"] = None
        self.runtimeDependencies["kde/frameworks/tier1/karchive"] = None
        self.runtimeDependencies["kde/frameworks/tier1/kconfig"] = None
        self.runtimeDependencies["kde/frameworks/tier1/kcoreaddons"] = None
        self.runtimeDependencies["kde/frameworks/tier1/kguiaddons"] = None
        self.runtimeDependencies["kde/frameworks/tier1/ki18n"] = None
        self.runtimeDependencies["kde/frameworks/tier1/kitemviews"] = None
        self.runtimeDependencies["kde/frameworks/tier1/kplotting"] = None
        self.runtimeDependencies["kde/frameworks/tier1/kwidgetsaddons"] = None
        self.runtimeDependencies["kde/frameworks/tier1/kimageformats"] = None
        self.runtimeDependencies["kde/frameworks/tier2/kcompletion"] = None
        self.runtimeDependencies["kde/frameworks/tier2/kcrash"] = None
        self.runtimeDependencies["kde/frameworks/tier2/kjobwidgets"] = None
        self.runtimeDependencies["kde/frameworks/tier2/kfilemetadata"] = None
        self.runtimeDependencies["kde/frameworks/tier2/kcolorscheme"] = None
        self.runtimeDependencies["kde/frameworks/tier3/kdeclarative"] = None
        self.runtimeDependencies["kde/frameworks/tier3/kbookmarks"] = None
        self.runtimeDependencies["kde/frameworks/tier3/kconfigwidgets"] = None
        self.runtimeDependencies["kde/frameworks/tier3/kiconthemes"] = None
        self.runtimeDependencies["kde/frameworks/tier3/kio"] = None
        self.runtimeDependencies["kde/frameworks/tier3/knewstuff"] = None
        self.runtimeDependencies["kde/frameworks/tier3/knotifications"] = None
        self.runtimeDependencies["kde/frameworks/tier3/knotifyconfig"] = None
        self.runtimeDependencies["kde/frameworks/tier3/kservice"] = None
        self.runtimeDependencies["kde/frameworks/tier3/ktextwidgets"] = None
        self.runtimeDependencies["kde/frameworks/tier3/kxmlgui"] = None
        self.runtimeDependencies["kde/frameworks/tier3/purpose"] = None
        self.runtimeDependencies["kde/frameworks/tier4/frameworkintegration"] = None
        self.runtimeDependencies["kde/kdenetwork/kio-extras"] = None
        self.runtimeDependencies["qt-libs/kddockwidgets"] = None
        self.runtimeDependencies["kde/kdemultimedia/ffmpegthumbs"] = None
        self.runtimeDependencies["libs/ffmpeg"] = None
        self.runtimeDependencies["libs/mlt"] = None
        self.runtimeDependencies["libs/opentimelineio"] = None
        self.runtimeDependencies["kde/plasma/breeze"] = None
        self.runtimeDependencies["data/rustedbronze-theme"] = None
        if not CraftCore.compiler.isMacOS:
            self.runtimeDependencies["libs/frei0r-bigsh0t"] = None


class Package(CraftPackageObject.get("kde").pattern):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.subinfo.options.configure.args += [
            f"-DFETCH_OTIO={CraftCore.compiler.isMacOS.asOnOff}",
            f"-DUSE_DBUS={CraftCore.compiler.isLinux.asOnOff}",
            "-DRELEASE_BUILD=OFF",
        ]

    def configure(self):
        # Craft has already fetched the pinned source and applied Phase 5 here.
        marker = self.sourceDir() / "EDIT_AJA_BRANDING.md"
        if not marker.exists():
            script = self.blueprintDir() / "apply_branding.py"
            assets = self.blueprintDir()
            result = subprocess.run(
                [sys.executable, str(script), str(self.sourceDir()), "--assets", str(assets)],
                check=False,
            )
            if result.returncode != 0:
                return False
        return super().configure()

    def createPackage(self):
        # Reuse Kdenlive's packaging exclusion rules while branding the package
        # and Windows shortcut as Edit Aja. The executable remains kdenlive.exe
        # internally in this first build for maximum compatibility.
        upstream_blueprint = self.blueprintDir().parent / "kdenlive"
        self.blacklist_file.append(upstream_blueprint / "exclude.list")

        self.addExecutableFilter(r"bin/(?!(ff|kdenlive|kioworker|melt|update-mime-database|snoretoast|drmingw|data/kdenlive)).*")
        self.ignoredPackages.append("libs/llvm")
        self.ignoredPackages.append("data/hunspell-dictionaries")
        self.ignoredPackages.append("binary/mysql")

        self.defines["appname"] = "editaja"
        self.defines["icon"] = self.sourceDir() / "data/icons/kdenlive.ico"
        self.defines["icon_png"] = self.sourceDir() / "data/icons/256-apps-kdenlive.png"
        self.defines["shortcuts"] = [
            {"name": "Edit Aja", "target": "bin/kdenlive.exe", "description": self.subinfo.description}
        ]
        self.defines["file_types"] = [".kdenlive"]

        if isinstance(self, NullsoftInstallerPackager):
            self.defines["registry_hook"] = (
                'WriteRegStr SHCTX "Software\\Classes\\.kdenlive" "" "EditAja"\n'
                'WriteRegStr SHCTX "Software\\Classes\\EditAja" "" "Edit Aja project"\n'
                'WriteRegStr SHCTX "Software\\Classes\\EditAja\\DefaultIcon" "" "$INSTDIR\\kdenlive.ico"\n'
                'WriteRegStr SHCTX "Software\\Classes\\EditAja\\shell" "" "open"\n'
                'WriteRegStr SHCTX "Software\\Classes\\EditAja\\shell\\open\\command" "" \'"$INSTDIR\\bin\\kdenlive.exe" "%1"\'\n'
            )

        return super().createPackage()
