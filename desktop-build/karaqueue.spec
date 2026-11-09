# PyInstaller spec for the desktop app: one folder with KaraQueue (the window) and yt-dlp (a
# console program the server runs), plus tools/ from fetch_tools.py. On macOS it becomes
# KaraQueue.app. Build: python desktop-build/fetch_tools.py && pyinstaller desktop-build/karaqueue.spec
import os
import sys

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
ICON = os.path.join(ROOT, "karaqueue", "static", "KaraQueue-Icon.png")  # PyInstaller converts it with Pillow
TOOLS = os.path.join(SPECPATH, "build", "tools")
MAC = sys.platform == "darwin"

datas = [(os.path.join(ROOT, "karaqueue", "static"), "karaqueue/static"),
         (os.path.join(ROOT, "karaqueue", "desktop.html"), "karaqueue"),
         *copy_metadata("karaqueue")]
if os.path.isdir(TOOLS):
    datas.append((TOOLS, "tools"))  # as data, so PyInstaller leaves the programs exactly as they are

app = Analysis([os.path.join(SPECPATH, "desktop_entry.py")], pathex=[ROOT], datas=datas,
               hiddenimports=[*collect_submodules("uvicorn"), *collect_submodules("karaqueue")],
               excludes=["gi", "PyQt5", "PySide2", "PySide6", "tkinter"])


# The Linux build's Qt brings QML and Quick plugins the web view never uses, and translations
# for Qt's own dialogs. Leaving them out saves a few hundred MB.
UNUSED = ("libQt6Quick3D", "libQt6QuickControls2", "libQt6QuickDialogs2", "libQt6QuickTemplates2",
          "libQt6QuickTimeline", "libQt6QuickParticles", "libQt6QuickShapes", "libQt6ShaderTools", "libQt6Pdf")


def needed(entry):
    dest = entry[0].replace("\\", "/")
    if dest.startswith("PyQt6/Qt6/qml/"):
        return False
    if dest.startswith("PyQt6/Qt6/translations/") and "/qtwebengine_locales/" not in dest:
        return False
    return not os.path.basename(dest).startswith(UNUSED)


if sys.platform.startswith("linux"):
    app.binaries = [e for e in app.binaries if needed(e)]
    app.datas = [e for e in app.datas if needed(e)]
ytdlp = Analysis([os.path.join(SPECPATH, "ytdlp_entry.py")], hiddenimports=collect_submodules("yt_dlp"))

app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="KaraQueue", console=False,
              icon=ICON, argv_emulation=False)
ytdlp_exe = EXE(PYZ(ytdlp.pure), ytdlp.scripts, [], exclude_binaries=True, name="yt-dlp", console=True)

folder = COLLECT(app_exe, app.binaries, app.datas, ytdlp_exe, ytdlp.binaries, ytdlp.datas, name="KaraQueue")

if MAC:
    BUNDLE(folder, name="KaraQueue.app", icon=ICON, bundle_identifier="io.github.mdbook.karaqueue",
           info_plist={"CFBundleDisplayName": "KaraQueue", "NSHighResolutionCapable": True,
                       "CFBundleShortVersionString": os.environ.get("KARAQUEUE_VERSION", "0"),
                       "NSLocalNetworkUsageDescription": "KaraQueue finds your router to open a port, and your TVs to cast to."})
