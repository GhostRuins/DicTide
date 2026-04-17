# -*- mode: python ; coding: utf-8 -*-
# Build: python -m PyInstaller DicTide.spec --clean --noconfirm
# Output: dist/DicTide/DicTide.exe (folder distribution; reliable for CUDA/CPU DLLs)

from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = []
binaries = []
hiddenimports = [
    "faster_whisper",
    "ctranslate2",
    "huggingface_hub",
    "huggingface_hub.file_download",
    "numpy",
    "sounddevice",
    "pyperclip",
    "keyboard",
    "keyboard._keyboard_event",
    "pystray",
    "PIL",
    "PIL.Image",
    "src.single_instance",
    "src.logging_setup",
    "src.settings_store",
]

for pkg in ("customtkinter", "ctranslate2", "sounddevice", "faster_whisper", "keyboard", "pystray"):
    try:
        ds, bs, hi = collect_all(pkg)
        datas += ds
        binaries += bs
        hiddenimports += hi
    except Exception:
        pass

a = Analysis(
    ["run_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DicTide",
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

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="DicTide",
)
