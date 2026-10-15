from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipelines" / "rhelai-disk-image-test.yaml"


def test_helper_vm_uses_normalized_test_architecture():
    pipeline = yaml.safe_load(PIPELINE.read_text())
    helper = next(
        task for task in pipeline["spec"]["tasks"] if task["name"] == "create-helper-vm-aws"
    )
    helper_params = {param["name"]: param["value"] for param in helper["params"]}

    assert helper_params["arch"] == "$(tasks.create-configuration.results.mapt-architecture)"
