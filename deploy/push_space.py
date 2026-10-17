"""Upload the deployment to a Hugging Face Space (Docker SDK) in a single commit.

One-time setup: create a write token on huggingface.co and run `hf auth login` yourself.
Then make the snapshot and push:

    .venv/Scripts/python deploy/snapshot.py
    .venv/Scripts/python deploy/push_space.py <hf-user>/repo-brain

The Space needs one secret, GEMINI_API_KEY (Settings -> Variables and secrets).
"""

import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi

ROOT = Path(__file__).resolve().parent.parent
FILES = ["Dockerfile", "pyproject.toml", "deploy/start.sh", "snapshot/dump.rdb"]
FOLDERS = ["repobrain", "web"]


def main(space: str) -> None:
    api = HfApi()
    print(f"logged in as {api.whoami()['name']}")
    if not (ROOT / "snapshot" / "dump.rdb").exists():
        sys.exit("snapshot/dump.rdb is missing: run deploy/snapshot.py first")
    api.create_repo(space, repo_type="space", space_sdk="docker", exist_ok=True)

    paths = list(FILES)
    for folder in FOLDERS:
        paths += [p.relative_to(ROOT).as_posix() for p in (ROOT / folder).rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts]
    ops = [CommitOperationAdd(path_in_repo=p, path_or_fileobj=str(ROOT / p)) for p in paths]
    ops.append(CommitOperationAdd(path_in_repo="README.md", path_or_fileobj=str(ROOT / "deploy" / "space_README.md")))
    api.create_commit(space, operations=ops, repo_type="space", commit_message="Deploy Repo Brain")
    print(f"pushed {len(ops)} files; the Space builds now: https://huggingface.co/spaces/{space}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or "/" not in sys.argv[1]:
        sys.exit("usage: python deploy/push_space.py <hf-user>/<space-name>")
    main(sys.argv[1])
