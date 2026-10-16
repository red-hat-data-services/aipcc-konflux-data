from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "extract-and-upload-cloud-image.yaml"


def test_skopeo_copy_selects_artifact_platform_explicitly():
    task = yaml.safe_load(TASK.read_text())
    params = {param["name"]: param for param in task["spec"]["params"]}
    script = task["spec"]["steps"][0]["script"]

    assert params["image-architecture"]["default"] == "x86_64"
    assert "x86_64|amd64" in script and "SKOPEO_ARCH=amd64" in script
    assert "aarch64|arm64" in script and "SKOPEO_ARCH=arm64" in script
    assert "SKOPEO_ARCH=${IMAGE_ARCHITECTURE}" in script
    assert "[[ \"${IMAGE_ARCHITECTURE}\" =~ ^[a-z0-9_]+$ ]]" in script
    assert "--override-os linux --override-arch ${SKOPEO_ARCH}" in script
