from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipelines" / "rhelai-disk-image-test.yaml"


def test_arm_helper_uses_explicit_test_image_architecture():
    pipeline = yaml.safe_load(PIPELINE.read_text())
    tasks = {task["name"]: task for task in pipeline["spec"]["tasks"]}
    helper_params = {
        param["name"]: param["value"]
        for param in tasks["create-helper-vm-aws"]["params"]
    }
    extract_params = {
        param["name"]: param["value"]
        for param in tasks["extract-and-upload-cloud-image"]["params"]
    }

    assert helper_params["arch"] == "arm64"
    assert extract_params["image-architecture"] == "$(params.architecture)"
