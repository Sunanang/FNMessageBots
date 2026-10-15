#!/usr/bin/env python3
"""核对 fnpack 产物、校验和及包内实际文件，不解压到工作目录。"""
import hashlib
import io
import sys
import tarfile
from pathlib import Path, PurePosixPath


def manifest_values(text):
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def release_files(folder):
    for path in folder.rglob("*"):
        if (path.is_file() and "__pycache__" not in path.parts and "data" not in path.parts
                and path.name != ".DS_Store" and path.suffix not in (".pyc", ".pyo", ".pyd")):
            yield path


def archive_files(archive):
    files = {}
    for member in archive.getmembers():
        if not member.isfile():
            continue
        name = str(PurePosixPath(member.name))
        if name in files:
            raise ValueError(f"Duplicate archive entry: {name}")
        stream = archive.extractfile(member)
        if stream is not None:
            files[name] = stream.read()
    return files


def compare_files(actual, expected):
    for name, content in expected.items():
        if actual.get(name) != content:
            raise ValueError(f"Package does not match checkout: {name}")


def verify_package(package, root):
    package, root = Path(package), Path(root)
    checksum = Path(str(package) + ".sha256").read_text().split()
    if checksum != [hashlib.sha256(package.read_bytes()).hexdigest(), package.name]:
        raise ValueError("FPK SHA-256 does not match")
    with tarfile.open(package, "r:*") as archive:
        files = archive_files(archive)
    metadata = manifest_values(files["manifest"].decode())
    expected_metadata = manifest_values((root / "fpk/manifest").read_text())
    for key in ("appname", "version", "platform"):
        if metadata.get(key) != expected_metadata.get(key):
            raise ValueError(f"Wrong manifest {key}")
    if metadata.get("platform") != "all":
        raise ValueError("Expected a platform=all FPK")
    expected = {}
    for source, target in (("src", "cmd/fnmessagebots/src"), ("assets", "cmd/fnmessagebots/assets")):
        for path in release_files(root / source):
            expected[f"{target}/{path.relative_to(root / source).as_posix()}"] = path.read_bytes()
    expected["cmd/fnmessagebots/requirements.txt"] = (root / "requirements.txt").read_bytes()
    for path in release_files(root / "fpk"):
        name = path.relative_to(root / "fpk").as_posix()
        if name.startswith(("cmd/fnmessagebots/src/", "cmd/fnmessagebots/assets/", "app/")):
            continue
        if name == "cmd/fnmessagebots/requirements.txt" or name == "manifest" or path.name == ".gitignore" or path.suffix == ".fpk":
            continue
        expected[name] = path.read_bytes()
    compare_files(files, expected)
    for name in files:
        if "__pycache__" in PurePosixPath(name).parts or name.endswith((".pyc", ".pyo", ".pyd", ".DS_Store")):
            raise ValueError(f"Unexpected build cache: {name}")
    with tarfile.open(fileobj=io.BytesIO(files["app.tgz"]), mode="r:*") as archive:
        app_files = archive_files(archive)
    compare_files(app_files, {
        path.relative_to(root / "fpk/app").as_posix(): path.read_bytes()
        for path in release_files(root / "fpk/app")
    })
    return metadata


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/verify-fpk.py package.fpk")
    metadata = verify_package(sys.argv[1], Path(__file__).resolve().parents[1])
    print(f"Verified {metadata['appname']} {metadata['version']}: checksum, source, dependencies, assets and UI match checkout")
