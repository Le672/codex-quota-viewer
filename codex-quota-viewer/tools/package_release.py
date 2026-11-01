"""只打包分发所需文件，不包含设置、日志、凭据或采集快照。"""
import hashlib
import sys
import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from codexquota.appenv import APP_VERSION

destination = root / '.release'
destination.mkdir(exist_ok=True)
files = ['dist/Codex额度查看器.exe', 'dist/Codex额度联动.exe', 'icon.ico',
         'install.ps1', 'register-follow-task.ps1', 'README.md', 'CHANGELOG.md', 'LICENSE']
for relative in files:
    if not (root / relative).is_file():
        raise SystemExit(f'缺少分发文件：{relative}')
archive = destination / f'CodexQuotaViewer-v{APP_VERSION}-windows-x64.zip'
with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
    for relative in files:
        output.write(root / relative, relative)
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
(destination / 'SHA256SUMS.txt').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
print(archive)
