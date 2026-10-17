"""Copy exactly the files the deployment image needs into a clean folder to upload.

Uploading the project folder itself would risk shipping .env (secrets) and .venv, and the
snapshot is gitignored, so uploaders that honour .gitignore would leave it out. The folder is
outside the repo for the same reason: parent .gitignore rules would apply to anything inside it.

    .venv/Scripts/python deploy/snapshot.py
    .venv/Scripts/python deploy/stage.py          # prints the folder
    railway up <that folder> --detach
"""

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["Dockerfile", ".dockerignore", "pyproject.toml", "deploy/start.sh", "snapshot/dump.rdb"]
FOLDERS = ["repobrain", "web"]
OUT = Path(tempfile.gettempdir()) / "repobrain-deploy"


def main() -> None:
    if not (ROOT / "snapshot" / "dump.rdb").exists():
        sys.exit("snapshot/dump.rdb is missing: run deploy/snapshot.py first")
    shutil.rmtree(OUT, ignore_errors=True)
    for f in FILES:
        (OUT / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, OUT / f)
    for d in FOLDERS:
        shutil.copytree(ROOT / d, OUT / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    files = [p for p in OUT.rglob("*") if p.is_file()]
    print(f"staged {len(files)} files ({sum(p.stat().st_size for p in files) / 1e6:.1f} MB) in {OUT}")


if __name__ == "__main__":
    main()
