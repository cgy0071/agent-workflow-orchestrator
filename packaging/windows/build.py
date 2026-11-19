"""Build a portable Windows ZIP from committed source and the pinned CPython ZIP."""
import argparse
import hashlib
import io
import subprocess
import zipfile
from pathlib import Path


def build(runtime, output):
    repo = Path(__file__).resolve().parents[2]
    data = runtime.read_bytes()
    if hashlib.sha256(data).hexdigest() != "91d828c2da3a029b41699e918674a0cb379c02cf20dab9c501306885f837402a":
        raise ValueError("Expected the official CPython 3.13.9 Windows x64 embeddable ZIP")
    source = subprocess.check_output(["git", "archive", "--format=zip", "HEAD"], cwd=repo)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    with zipfile.ZipFile(io.BytesIO(source)) as files:
        entries = {"app/" + n: files.read(n) for n in files.namelist() if not n.endswith("/")}
    with zipfile.ZipFile(io.BytesIO(data)) as files:
        entries.update({"app/python/" + n: files.read(n) for n in files.namelist()})
    entries["app/python/python313._pth"] = b"python313.zip\n.\n../scripts\n"
    entries["app/start.py"] = entries["app/packaging/windows/start.py"]
    entries["先读我.md"] = entries["app/packaging/windows/QUICKSTART.zh-CN.md"]
    entries["开始.cmd"] = (b'@echo off\r\nsetlocal\r\n"%~dp0app\\python\\python.exe" -B "%~dp0app\\start.py"\r\n'
                         b'set "rc=%errorlevel%"\r\npause\r\nexit /b %rc%\r\n')
    entries["app/SOURCE_COMMIT"] = commit + b"\n"
    with zipfile.ZipFile(output, "x") as bundle:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name)
            info.create_system = 0
            bundle.writestr(info, entries[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    print(hashlib.sha256(output.read_bytes()).hexdigest(), output.name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.runtime, args.output)
