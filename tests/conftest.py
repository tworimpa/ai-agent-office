import os
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest

from agent_office.engines import resolve_executable


@pytest.fixture(scope="session")
def trusted_validator_image():
    if os.environ.get("OFFICE_DOCKER_ACCEPTANCE") != "1":
        pytest.skip("requires opt-in local Linux Docker daemon")
    docker = resolve_executable("docker.exe" if os.name == "nt" else "docker")
    assert docker, "Docker executable is required for the requested integration test"
    tag = "office-validator-test:" + uuid4().hex
    context = Path(__file__).resolve().parents[1] / "validation"
    subprocess.run([docker, "build", "-t", tag, str(context)], check=True, timeout=300)
    try:
        yield subprocess.check_output([docker, "image", "inspect", "--format", "{{.Id}}", tag],
                                      text=True, timeout=15).strip()
    finally:
        subprocess.run([docker, "image", "rm", tag], check=True, timeout=30)
