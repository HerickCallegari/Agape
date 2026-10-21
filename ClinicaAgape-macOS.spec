# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from agape_app.version import APP_VERSION


datas = [
    ('.env.example', '.'),
    ('agape_app/assets', 'agape_app/assets'),
]
public_env = Path(os.environ.get('AGAPE_BUNDLE_ENV', ''))
if not public_env.is_file():
    raise RuntimeError('Use build_macos.sh para gerar e fornecer a configuração pública saneada.')
datas.append((str(public_env), '.'))

minimum_macos = os.environ.get('AGAPE_MACOS_MIN_VERSION', '').strip()
if not minimum_macos:
    raise RuntimeError('AGAPE_MACOS_MIN_VERSION não foi definida pelo script de build.')

target_arch = os.environ.get('AGAPE_MACOS_TARGET_ARCH', '').strip()
if target_arch not in {'arm64', 'x86_64'}:
    raise RuntimeError('AGAPE_MACOS_TARGET_ARCH deve ser arm64 ou x86_64.')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ClinicaAgape',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=target_arch,
    codesign_identity=None,
    entitlements_file=None,
    icon=['agape_app/assets/app_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='ClinicaAgape',
)

app = BUNDLE(
    coll,
    name='ClinicaAgape.app',
    icon='agape_app/assets/app_icon.ico',
    bundle_identifier='com.clinicaagape.desktop',
    version=APP_VERSION,
    info_plist={
        'CFBundleDisplayName': 'Clínica Ágape',
        'CFBundleName': 'Clínica Ágape',
        'CFBundleShortVersionString': APP_VERSION,
        'CFBundleVersion': APP_VERSION,
        'LSMinimumSystemVersion': minimum_macos,
        'NSHighResolutionCapable': True,
        'NSPrincipalClass': 'NSApplication',
    },
)
