"""Save the local graph as snapshot/dump.rdb for the deployment image, without touching local data.

The graph key is copied with DUMP/RESTORE into a throwaway FalkorDB container (same image as the
deployment), personal chat memory is removed there, and that container's RDB file is copied out.

    .venv/Scripts/python deploy/snapshot.py
"""

import subprocess
import sys
import time
from pathlib import Path

import redis

IMAGE = "falkordb/falkordb@sha256:b0bdcac2cbaff989e77cb0610439b7a9289e86de033ccbbd4368b66fb680ae7a"
GRAPH = "brain:falkordb"
NAME, PORT = "repobrain-snapshot", 6390
OUT = Path(__file__).resolve().parent.parent / "snapshot" / "dump.rdb"

# Test users' chat memory stays local; triage tasks and agents ship as demo state.
CLEAN = "MATCH (n) WHERE n:User OR n:Session OR n:Turn OR n:Fact DETACH DELETE n RETURN count(n)"


def sh(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def main() -> None:
    subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)
    sh("docker", "run", "-d", "--name", NAME, "-p", f"{PORT}:6379", "--entrypoint", "redis-server", IMAGE,
       "--loadmodule", "/var/lib/falkordb/bin/falkordb.so", "--dir", "/tmp", "--save", "", "--appendonly", "no")
    try:
        target = redis.Redis(port=PORT, socket_timeout=600)  # RESTORE rebuilds every index: slow
        for _ in range(30):
            try:
                target.ping()
                break
            except redis.ConnectionError:
                time.sleep(1)
        payload = redis.Redis(port=6379).dump(GRAPH)
        print(f"dumped {GRAPH}: {len(payload) / 1e6:.1f} MB")
        target.restore(GRAPH, 0, payload, replace=True)
        removed = target.execute_command("GRAPH.QUERY", GRAPH, CLEAN)[1][0][0]
        print(f"removed {removed} memory nodes (users, sessions, turns, facts)")
        target.save()
        OUT.parent.mkdir(exist_ok=True)
        sh("docker", "cp", f"{NAME}:/tmp/dump.rdb", str(OUT))
        print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")
    finally:
        subprocess.run(["docker", "rm", "-f", NAME], capture_output=True)


if __name__ == "__main__":
    sys.exit(main())
