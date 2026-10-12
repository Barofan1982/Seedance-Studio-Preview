"""Package an already-built Windows bundle with notices and offline validation."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from importlib.metadata import distribution
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    # Build into staging: rebuilding must never delete users' portable data.
    folder = root / 'build' / 'portable-output' / 'Seedance Studio'
    executable = folder / 'Seedance Studio.exe'
    if not executable.is_file():
        raise SystemExit('Build SeedanceStudio.spec before packaging.')
    media_dir = Path(os.environ.get('SEEDANCE_FFMPEG_DIR') or Path(shutil.which('ffmpeg') or '').parent)
    media_root = media_dir.parent
    licenses = folder / 'licenses'; licenses.mkdir(exist_ok=True)
    for name in ('LICENSE', 'README.txt'):
        source = media_root / name
        if not source.is_file():
            raise SystemExit('Missing FFmpeg redistribution notice: ' + name)
        shutil.copy2(source, licenses / ('FFmpeg-' + name))
    shutil.copy2(Path(sys.base_prefix) / 'LICENSE.txt', licenses / 'Python-LICENSE.txt')
    for name in ('PySide6-Essentials', 'PySide6-Addons', 'shiboken6', 'cryptography', 'cffi', 'pyinstaller'):
        metadata = distribution(name)
        target = licenses / name; target.mkdir(exist_ok=True)
        (target / 'metadata.json').write_text(json.dumps({'name': metadata.metadata['Name'], 'version': metadata.version,
                   'license': metadata.metadata.get('License-Expression') or metadata.metadata.get('License')}, indent=2), encoding='utf-8')
        for file in metadata.files or []:
            if file.name.lower().startswith(('license', 'copying')):
                source = Path(metadata.locate_file(file))
                if source.is_file(): shutil.copy2(source, target / file.name)
    for name in ('GPL-3.0.txt', 'LGPL-3.0.txt'):
        source = root / 'resources' / 'licenses' / name
        if not source.is_file(): raise SystemExit('Missing third-party license text: ' + name)
        shutil.copy2(source, licenses / name)
    (folder / '第三方组件说明.md').write_text('''# 第三方组件

本分发包包含 Python、PySide6/Qt、cryptography、CFFI、PyInstaller 运行引导组件以及 FFmpeg/FFprobe。
对应许可文本与组件版本保存在 licenses 目录。Qt 库以独立 DLL 形式分发。

- Qt/PySide6 采用其开源 LGPL/GPL 许可选项；源码与构建资料：https://code.qt.io/ 和 https://download.qt.io/official_releases/
- FFmpeg 是独立进程使用的第三方媒体工具，当前构建许可与精确源码位置见 licenses/FFmpeg-README.txt，随包保留原构建配置与 GPL 许可。
- Python：https://www.python.org/；cryptography：https://github.com/pyca/cryptography；PyInstaller：https://github.com/pyinstaller/pyinstaller。

第三方许可不改变各服务账号、API 余额及实际调用的计费规则。
''', encoding='utf-8')
    (folder / '使用说明.txt').write_text('''Seedance Studio · Windows 64 位便携版

1. 将整个 ZIP 解压到一个文件夹。
2. 双击 Seedance Studio.exe 启动。请保留 _internal 和 licenses 文件夹。
3. 不需要安装 Python、Qt 或 FFmpeg，不使用 py/pyw 启动。
4. 首次使用，请在设置中填写自己的 Seedance 接口密钥和上传账号。
5. 分析平台：ChatGPT 使用账号登录；DeepSeek 填写自己的 API Key 后保存并校验。
6. DeepSeek 分析和视频生成分别按对应接口用量计费。
7. 首次启动自动创建 EXE 旁的 data 目录；配置、密钥、历史、队列和素材库保存在其中，日志位于 data\\logs。
8. 视频默认自动保存到 EXE 旁的“成片”目录，可在设置中修改。更新时请保留自己的 data 和“成片”目录。
9. 分发包不包含制作人的账号、密钥、草稿、素材库和生成历史。
10. 不知道即梦 API Token 或中转站登录账号，请联系吴仕敏索取文档和密钥。
11. 请解压到可写入的目录，例如桌面，不要直接在 ZIP 中打开或放在受保护的 Program Files 中。
12. 密钥由当前 Windows 账号加密，更换电脑或 Windows 账号时需重新配置自己的密钥。
''', encoding='utf-8')
    report = root / 'reports' / 'portable-self-test.json'
    environment = dict(os.environ)
    environment['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
    environment.pop('PYTHONHOME', None); environment.pop('PYTHONPATH', None)
    result = subprocess.run([str(executable), '--self-test', str(report)], env=environment,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=120)
    if result.returncode != 0 or not report.is_file():
        raise SystemExit('Executable validation failed; inspect reports/portable-self-test.json.')
    checks = json.loads(report.read_text(encoding='utf-8'))
    if not checks.get('success'): raise SystemExit('Offline checks did not pass.')
    diagnostics = root / 'diagnostics'; diagnostics.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='portable-startup-', dir=diagnostics) as testing:
        copied = Path(testing) / '同学测试' / 'Seedance Studio'
        def link_or_copy(source, destination):
            try: os.link(source, destination)
            except OSError: shutil.copy2(source, destination)
        shutil.copytree(folder, copied, copy_function=link_or_copy)
        # An unrelated legacy profile must not influence portable startup.
        environment['APPDATA'] = str(Path(testing) / 'old-profile')
        legacy = Path(environment['APPDATA']) / 'SeedanceVideoStudioPreview'
        legacy.mkdir(parents=True)
        (legacy / 'error.log').mkdir()
        startup_report = Path(testing) / 'startup.json'
        launched = subprocess.run([str(copied / executable.name), '--startup-check', str(startup_report)],
                                  env=environment, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        if startup_report.is_file():
            shutil.copy2(startup_report, root / 'reports' / 'portable-startup-check.json')
        if launched.returncode != 0 or not startup_report.is_file():
            raise SystemExit('Normal portable startup check failed.')
        startup = json.loads(startup_report.read_text(encoding='utf-8'))
        if not startup.get('success'): raise SystemExit('Portable profile checks failed: ' + str(startup['checks']))
        checks['checks'].update(startup['checks'])
        report.write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    forbidden = [str(file.relative_to(folder)) for file in folder.rglob('*') if file.is_file() and
                 (file.suffix.lower() in {'.sqlite3', '.db', '.lnk', '.log', '.mp4', '.png', '.jpg', '.pyw'} or
                  file.name in {'state.json', 'api_key.bin', 'deepseek_api_key.bin', 'chatgpt_accounts.bin', 'video_upload_account.bin'})]
    if forbidden: raise SystemExit('Unexpected personal/runtime files: ' + ', '.join(forbidden))
    shutil.copy2(report, folder / '便携版校验.json')
    (root / 'dist').mkdir(exist_ok=True)
    archive = root / 'dist' / 'Seedance-Studio-Preview-Windows-x64.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for file in sorted(folder.rglob('*')):
            if file.is_file(): package.write(file, file.relative_to(folder.parent).as_posix())
    with archive.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    archive.with_suffix('.zip.sha256').write_text(checksum + '  ' + archive.name + '\n', encoding='ascii')
    shutil.copytree(folder, root / 'dist' / folder.name, dirs_exist_ok=True)
    print(json.dumps({'archive': str(archive), 'size_bytes': archive.stat().st_size, 'sha256': checksum,
                      'offline_checks': checks['checks']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
