"""Create an external EXE update ZIP without source or runtime data."""
import hashlib
import json
import zipfile
from pathlib import Path

def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    root = Path(__file__).resolve().parent
    previous = json.loads((root / 'reports' / 'pre-reasoning-bundle.json').read_text(encoding='utf-8'))
    bundle = root / 'build' / 'portable-output' / 'Seedance Studio'
    validation = json.loads((bundle / '便携版校验.json').read_text(encoding='utf-8'))
    if not validation.get('success'):
        raise SystemExit('Portable checks must pass before building the update.')
    removed = [name for name in previous if not (bundle / name).is_file()]
    if removed:
        raise SystemExit('This build needs a full bundle update; removed files: ' + str(removed))
    files = {}
    changed = []
    for path in sorted(bundle.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle)
        if relative.parts[0] in {'data', '成片'} or path.suffix.lower() in {'.bin','.sqlite3','.db','.log','.py','.pyw'}:
            raise SystemExit('Source and runtime data cannot be included in an update.')
        if previous.get(relative.as_posix()) != checksum(path):
            changed.append(relative.as_posix())
            files[relative.as_posix()] = path
    if 'Seedance Studio.exe' not in changed:
        raise SystemExit('No updated executable was found.')
    instructions = '''Seedance Studio · 思考强度更新

默认思考强度为“高”，可以在“分析平台 · 连接设置”或“工作室设置”中手动调整。
按平台、模型分别保存选择，并写入实际分析请求和调用记录。

本更新包用于已安装的 Windows EXE 便携版。
1. 关闭 Seedance Studio，建议备份旧程序文件。
2. 解压更新包，将程序文件复制到原 Seedance Studio.exe 所在目录，覆盖同名文件。
3. 双击 Seedance Studio.exe 重新启动。
此包是增量更新，首次使用仍需完整解压 Windows-x64 便携包。

便携更新文件：
''' + '\n'.join('• ' + name for name in changed) + '''

保留现有 data、成片目录及 _internal 文件夹中的其他运行库。
不知道即梦 API Token 或中转站登录账号，请联系吴仕敏索取文档和密钥。
'''
    archive = root / 'dist' / 'Seedance-Studio-Reasoning-Update.zip'
    manifest = {name: checksum(path) for name, path in files.items()}
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as package:
        for name, path in files.items():
            package.write(path, name)
        package.writestr('更新说明.txt', instructions)
        package.writestr('文件校验.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(archive) as package:
        if package.testzip() is not None:
            raise SystemExit('Update archive CRC verification failed.')
    archive.with_suffix('.zip.sha256').write_text(checksum(archive) + '  ' + archive.name + '\n', encoding='ascii')
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size,
                      'portable_changed_files': changed}, ensure_ascii=False))


if __name__ == '__main__':
    main()
