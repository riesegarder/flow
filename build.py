"""Builds dist/flow.exe and dist/flow-<version>-<platform>.zip (the asset rokit installs)."""
import platform
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
version = re.search(r'^VERSION = "([^"]+)"', (ROOT / "flow" / "flow.py").read_text(encoding="utf-8"), re.M).group(1)

subprocess.run([sys.executable, "-m", "PyInstaller", "--onefile", "--noconfirm", "--clean", "--name", "flow",
                "--paths", str(ROOT / "flow"), str(ROOT / "flow" / "flow.py")], cwd=ROOT, check=True)

exe = ROOT / "dist" / ("flow.exe" if sys.platform == "win32" else "flow")
osname = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
out = ROOT / "dist" / f"flow-{version}-{osname}-{arch}.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(exe, exe.name)
print(out)
