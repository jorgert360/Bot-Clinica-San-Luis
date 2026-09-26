# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for Bot-San-Francisco (Fase 1F).

Windowed (no console) build of the Tk GUI, entry point
``scripts/run_gui.py``. Bundles ``src/clinica_rpa/gui/assets/`` (logo PNG
+ generated .ico) so the packaged exe finds them via
``clinica_rpa.gui.app._assets_base_dir()`` (frozen-aware, checks
``sys._MEIPASS``).

Build with: scripts\\build_exe.ps1
or directly: pyinstaller Bot-San-Francisco.spec --noconfirm
"""

from pathlib import Path

block_cipher = None

_ROOT = Path(SPECPATH)
_SRC = _ROOT / "src"
_ASSETS = _SRC / "clinica_rpa" / "gui" / "assets"
_ICON = _ASSETS / "bot_san_francisco.ico"

a = Analysis(
    [str(_ROOT / "scripts" / "run_gui.py")],
    pathex=[str(_SRC)],
    binaries=[],
    datas=[
        (str(_ASSETS), "clinica_rpa/gui/assets"),
    ],
    hiddenimports=[
        "clinica_rpa.gui.app",
        "clinica_rpa.gui.batch_worker",
        "clinica_rpa.gui.worker",
        "clinica_rpa.gui.state",
        "clinica_rpa.gui.theme",
        "clinica_rpa.batch.excel_loader",
        "clinica_rpa.services.batch_invoice_service",
        "clinica_rpa.services.invoice_download_service",
        "win32com.shell",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Bot-San-Francisco",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(_ICON) if _ICON.exists() else None,
)
