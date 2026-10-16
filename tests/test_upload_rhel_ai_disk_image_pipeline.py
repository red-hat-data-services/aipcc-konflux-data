from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipelines" / "upload-rhel-ai-disk-image.yaml"
PIPELINERUN = ROOT / "pipelineruns" / "upload-rhel-ai-disk-image.yaml"


def test_upload_pipeline_defaults_helper_to_x86_64_and_passes_param():
    pipeline = yaml.safe_load(PIPELINE.read_text())
    params = {param["name"]: param for param in pipeline["spec"]["params"]}
    assert params["helper-vm-architecture"]["default"] == "x86_64"
    assert params["image-architecture"]["default"] == "x86_64"

    tasks = {task["name"]: task for task in pipeline["spec"]["tasks"]}
    helper = tasks["create-helper-vm-aws"]
    helper_params = {param["name"]: param["value"] for param in helper["params"]}
    assert helper_params["arch"] == "$(params.helper-vm-architecture)"

    extract = tasks["extract-and-upload-cloud-image"]
    extract_params = {param["name"]: param["value"] for param in extract["params"]}
    assert extract_params["image-architecture"] == "$(params.image-architecture)"


def test_upload_pipelinerun_sample_uses_x86_64_helper():
    pipelinerun = yaml.safe_load(PIPELINERUN.read_text())
    params = {param["name"]: param["value"] for param in pipelinerun["spec"]["params"]}
    assert params["helper-vm-architecture"] == "x86_64"
    assert params["image-architecture"] == "x86_64"
