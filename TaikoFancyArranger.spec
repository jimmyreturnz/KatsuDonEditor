# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


root = Path(SPECPATH)

assets = root / "assets"
translations = root / "translations"
version_file = root / "VERSION"

data_entries = []

if assets.exists():
    data_entries.append((str(assets), "assets"))

if version_file.exists():
    data_entries.append((str(version_file), "."))

translation_files = sorted(translations.glob("*.qm"))

if not translation_files:
    raise FileNotFoundError(
        "No compiled translation files were found in translations/. "
        "Run tools\\compile_translations.bat before building."
    )

data_entries.extend(
    (str(translation_file), "translations")
    for translation_file in translation_files
)


analysis = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=data_entries,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="TaikoFancyArranger",
    icon=str(
        root
        / "assets"
        / "icons"
        / "FancyTaikoEditor_Logo.ico"
    ),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TaikoFancyArranger",
)