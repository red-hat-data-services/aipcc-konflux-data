from pathlib import Path
import os
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "config-from-snapshot.yaml"
PIPELINES = [
    ROOT / "pipelines" / "rhelai-disk-image-test.yaml",
    ROOT / "pipelines" / "rhelai-bootc-upgrade-test.yaml",
]


def test_config_task_prefers_explicit_component_and_keeps_label_fallback():
    task = yaml.safe_load(TASK.read_text())
    params = {param["name"]: param for param in task["spec"]["params"]}
    env = {item["name"]: item for item in task["spec"]["steps"][0]["env"]}
    script = task["spec"]["steps"][0]["script"]

    assert params["component"]["default"] == ""
    assert env["TARGET_COMPONENT"]["value"] == "$(params.component)"
    assert env["PIPELINE_COMPONENT"]["valueFrom"]["fieldRef"]["fieldPath"] == (
        "metadata.labels['appstudio.openshift.io/component']"
    )
    assert 'application_suffix="${APPLICATION#rhelai-bootc-}"' in script
    assert 'COMPONENT="${COMPONENT}-${application_suffix}"' in script
    assert 'COMPONENT="${PIPELINE_COMPONENT}"' in script


@pytest.mark.parametrize(
    ("target", "application", "pipeline_component", "expected"),
    [
        (
            "bootc-rocm-azure-disk-image",
            "rhelai-bootc-3-6-ea1",
            "bootc-cuda-disk-image-container-3-6-ea1",
            "bootc-rocm-azure-disk-image-3-6-ea1",
        ),
        (
            "bootc-rocm-azure-disk-image-3-6-ea1",
            "rhelai-bootc-3-6-ea1",
            "bootc-cuda-disk-image-container-3-6-ea1",
            "bootc-rocm-azure-disk-image-3-6-ea1",
        ),
        (
            "bootc-rocm-azure-disk-image",
            "rhelai-bootc",
            "bootc-cuda-disk-image-container",
            "bootc-rocm-azure-disk-image",
        ),
        (
            "",
            "rhelai-bootc-3-6-ea1",
            "bootc-cuda-disk-image-container-3-6-ea1",
            "bootc-cuda-disk-image-container-3-6-ea1",
        ),
    ],
)
def test_component_selection_uses_target_or_falls_back_to_pipeline_label(
    target, application, pipeline_component, expected
):
    task = yaml.safe_load(TASK.read_text())
    script = task["spec"]["steps"][0]["script"]
    selection = script.split("# BEGIN target component selection", 1)[1].split(
        "# END target component selection", 1
    )[0]
    command = f'{selection}\nprintf "%s" "${{COMPONENT}}"\n'
    environment = os.environ | {
        "TARGET_COMPONENT": target,
        "APPLICATION": application,
        "PIPELINE_COMPONENT": pipeline_component,
    }

    result = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True, env=environment, check=True
    )

    assert result.stdout.splitlines()[-1] == expected


def test_disk_image_and_upgrade_pipelines_forward_target_component():
    for path in PIPELINES:
        pipeline = yaml.safe_load(path.read_text())
        params = {param["name"]: param for param in pipeline["spec"]["params"]}
        config_task = next(
            task for task in pipeline["spec"]["tasks"] if task["name"] == "create-configuration"
        )
        task_params = {param["name"]: param["value"] for param in config_task["params"]}

        assert params["component"]["default"] == ""
        assert task_params["component"] == "$(params.component)"
