"""The production runner as real root against the website as uid 10001, in a throw-away container (opt-in: HM_DOCKER_TESTS=1).

The unit tests run as one unprivileged uid and cannot see group/permission mistakes between the root-owned runner and the container's user.
This runs tests/container_runner_probe.py in `docker run --rm --network none` (the website's own base image, the repository mounted read-only,
everything written on the container's own filesystem), so nothing of the host is touched and the container is removed when it exits.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

import conftest  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not os.environ.get("HM_DOCKER_TESTS"), reason="opt-in: runs a throw-away docker container (HM_DOCKER_TESTS=1)")
def test_container_the_production_runner_as_root_serves_the_first_run_login_for_uid_10001():
    image = re.search(r"^FROM (\S+)", (ROOT / "web" / "Dockerfile").read_text(), re.M).group(1)
    name = f"maintenance-web-test-{os.getpid()}-runner"
    cmd = ["docker", "run", "--rm", "--name", name, "--network", "none", "--user", "0:0", "-v", f"{ROOT}:/repo:ro", "-e", "PYTHONDONTWRITEBYTECODE=1", image,
           "python", "-B", "/repo/tests/container_runner_probe.py"]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)                # (--rm removes it; this only matters if the run was killed)
    assert run.returncode == 0, run.stderr[-2000:]
    res = json.loads(run.stdout.strip().splitlines()[-1])
    assert res and not [k for k, v in res.items() if v is not True], res
