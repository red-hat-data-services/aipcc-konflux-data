import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
CONFIG_TASK = ROOT / "tasks" / "config-from-snapshot.yaml"
PIPELINE = ROOT / "pipelines" / "rhaiis-rhoai-test.yaml"


def _run_configuration(tmp_path, component, architecture="x86_64"):
    task = yaml.safe_load(CONFIG_TASK.read_text())
    result_names = {result["name"] for result in task["spec"]["results"]}
    assert {"snc-compute-sizes", "snc-profile"} <= result_names

    script = task["spec"]["steps"][0]["script"]
    script = script.replace("$(params.architecture)", architecture)
    for result_name in result_names:
        result_path = tmp_path / result_name
        script = script.replace(
            f"$(results.{result_name}.path)", str(result_path)
        )

    snapshot = (
        '{"components":[{"name":"%s","containerImage":"example.invalid/image@sha256:abc",'
        '"source":{"git":{"revision":"deadbeef"}}}]}' % component
    )
    jq = tmp_path / "jq"
    jq.write_text(
        f"""#!{sys.executable}
import json
import sys

selector = sys.argv[-1]
value = json.load(sys.stdin)
if selector == ".components[]":
    for component in value["components"]:
        print(json.dumps(component, separators=(",", ":")))
else:
    selectors = {{
        ".name": ("name",),
        ".containerImage": ("containerImage",),
        ".source.git.revision": ("source", "git", "revision"),
    }}
    for key in selectors[selector]:
        value = value[key]
    print(value)
"""
    )
    jq.chmod(0o755)
    env = {
        **os.environ,
        "APPLICATION": "rhaiis",
        "COMPONENT": component,
        "CONSOLE_URL": "https://konflux.example.test",
        "EVENT_TYPE": "manual",
        "INFERENCE_SERVER_FALLBACK_IMAGE": "",
        "NAMESPACE": "ai-tenant",
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SNAPSHOT": snapshot,
    }
    subprocess.run(["bash", "-c", script], check=True, cwd=tmp_path, env=env)
    return {
        name: (tmp_path / name).read_text()
        for name in ("snc-compute-sizes", "snc-profile")
    }


@pytest.mark.parametrize(
    "component, architecture, expected",
    [
        (
            "rhaiis-cuda-ubi9",
            "x86_64",
            {
                "snc-compute-sizes": "g4dn.8xlarge,g4dn.12xlarge,g4dn.16xlarge,g6.8xlarge,g6.12xlarge,g6.16xlarge,gr6.8xlarge,g6e.8xlarge",
                "snc-profile": "ai,nvidia",
            },
        ),
        (
            "rhaiis-cuda-ubi9",
            "aarch64",
            {
                "snc-compute-sizes": "g5g.16xlarge,g5g.metal",
                "snc-profile": "ai,nvidia",
            },
        ),
        (
            "rhaiis-cpu-ubi9",
            "x86_64",
            {
                "snc-compute-sizes": "",
                "snc-profile": "ai",
            },
        ),
    ],
)
def test_configuration_selects_snc_hardware_for_accelerator(
    tmp_path, component, architecture, expected
):
    assert _run_configuration(tmp_path, component, architecture) == expected


def test_pipeline_consumes_automatic_snc_configuration():
    pipeline = yaml.safe_load(PIPELINE.read_text())
    tasks = {task["name"]: task for task in pipeline["spec"]["tasks"]}
    create_params = {
        param["name"]: param["value"]
        for param in tasks["create-snc-cluster"]["params"]
    }
    assert create_params["profile"] == "$(tasks.create-configuration.results.snc-profile)"
    assert create_params["compute-sizes"] == "$(tasks.create-configuration.results.snc-compute-sizes)"
    assert "validate-gpu-params" not in tasks
