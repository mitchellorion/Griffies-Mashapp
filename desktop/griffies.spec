# PyInstaller spec for Griffies. Build from the repo root:
#   pyinstaller desktop/griffies.spec --noconfirm
import sys
from pathlib import Path

root = Path(SPECPATH).parent
is_mac = sys.platform == "darwin"
is_win = sys.platform.startswith("win")
icon = str(root / "desktop" / ("icon.icns" if is_mac else "icon.ico"))

hidden = ["server"]
try:
    import wreq  # optional: Grindr sign-in
    hidden += ["wreq", "wreq.blocking", "wreq.redirect"]
except Exception:
    pass

import json, os
version = os.environ.get("GRIFFIES_VERSION", "").strip().lstrip("vV") or (root / "VERSION").read_text().strip()
(root / "desktop" / "VERSION").write_text(version)
datas = [(str(root / "static"), "static"), (str(root / "desktop" / "VERSION"), ".")]
print(f"Building Griffies {version}")
map_key = os.environ.get("GRIFFIES_AWS_MAP_KEY", "").strip()
if map_key:
    bundled = root / "desktop" / "_bundled-config.json"
    bundled.write_text(json.dumps({
        "awsMapKey": map_key,
        "awsMapRegion": os.environ.get("GRIFFIES_AWS_MAP_REGION", "us-east-1"),
    }))
    target = root / "desktop" / "bundled-config.json"
    target.write_text(bundled.read_text())
    bundled.unlink()
    datas.append((str(target), "."))
    print("Bundling map key from GRIFFIES_AWS_MAP_KEY")

a = Analysis(
    [str(root / "desktop" / "app.py")],
    pathex=[str(root)],
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)

if is_mac:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Griffies", console=False, icon=icon)
    coll = COLLECT(exe, a.binaries, a.datas, name="Griffies")
    app = BUNDLE(
        coll,
        name="Griffies.app",
        icon=icon,
        bundle_identifier="com.ctrlaltorion.griffies",
        info_plist={
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSLocationWhenInUseUsageDescription": "Griffies uses your location only when you tap Use my location on the map.",
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="Griffies", console=False, icon=icon, upx=False,
    )
