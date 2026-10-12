# Build with: python -m PyInstaller --noconfirm --clean SeedanceStudio.spec
import os
import shutil
from pathlib import Path
from PySide6.QtCore import QLibraryInfo

root = Path(SPECPATH)
media_dir = Path(os.environ.get('SEEDANCE_FFMPEG_DIR') or Path(shutil.which('ffmpeg') or '').parent)
for name in ('ffmpeg.exe', 'ffprobe.exe'):
    if not (media_dir / name).is_file():
        raise SystemExit('Set SEEDANCE_FFMPEG_DIR to the folder containing FFmpeg and FFprobe.')
media_binaries = [(str(media_dir / name), 'media') for name in ('ffmpeg.exe', 'ffprobe.exe')]
media_binaries.extend((str(file), 'media') for file in sorted(media_dir.glob('*.dll')))
translation_dir = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath))
data = [(str(root / 'resources'), 'resources')]
for name in ('qtbase_zh_CN.qm', 'qtmultimedia_zh_CN.qm'):
    source = translation_dir / name
    if not source.is_file():
        raise SystemExit('Missing Simplified Chinese Qt catalog: ' + name)
    data.append((str(source), 'PySide6/translations'))

a = Analysis(
    [str(root / 'portable_entry.py')],
    pathex=[str(root)], binaries=media_binaries, datas=data,
    hiddenimports=['PySide6.QtSvg'],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'tkinter', 'matplotlib', 'numpy', 'pandas', 'pytest'],
    noarchive=False,
)
# Qt uses the ICU interface supplied by Windows. An unrelated ICU DLL from a
# developer's PATH can shadow it and lacks Windows' unversioned entry points.
a.binaries = [entry for entry in a.binaries if
              Path(entry[0]).name.lower() != 'icuuc.dll' and
              not Path(entry[0]).name.lower().startswith('icudt') and
              not Path(entry[0]).name.lower().startswith('api-ms-win-') and
              Path(entry[0]).name.lower() != 'ucrtbase.dll']
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Seedance Studio',
          debug=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Seedance Studio')
