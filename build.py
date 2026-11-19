"""Assemble the add-on with a separately supplied, previously qualified dependency bundle."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--dependency-bundle", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent
code = root / "hard_surface_workflow"
files = {p.relative_to(code).as_posix(): p.read_bytes() for p in code.rglob("*")
         if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
expected = json.loads((root / "dependencies.json").read_bytes())
with ZipFile(args.dependency_bundle) as upstream:
    for name, digest in expected.items():
        parts = PurePosixPath(name).parts
        if not parts or parts[0] != "vendor" or ".." in parts or "\\" in name or name in files:
            raise ValueError(f"Invalid dependency path: {name}")
        data = upstream.read(name)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"Dependency SHA-256 differs: {name}")
        files[name] = data
with ZipFile(args.output, "x") as archive:
    for name, data in sorted(files.items()):
        archive.writestr(ZipInfo(name), data, compress_type=ZIP_DEFLATED)
with ZipFile(args.output) as archive:
    if archive.testzip() or any(archive.read(name) != data for name, data in files.items()):
        raise RuntimeError("Archive readback failed")
print(hashlib.sha256(args.output.read_bytes()).hexdigest(), args.output)
