import base64
import json
import os
import subprocess
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
PIPELINE_PATH = REPO_ROOT / "pipelines" / "rhaiis-post-publication-finalization.yaml"


def _pipeline():
    return yaml.safe_load(PIPELINE_PATH.read_text())


def _task_script(task_name):
    task = next(task for task in _pipeline()["spec"]["tasks"] if task["name"] == task_name)
    return task["taskSpec"]["steps"][0]["script"]


def _make_command(bin_dir, name, script):
    command = bin_dir / name
    command.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + script)
    command.chmod(0o755)


def test_copies_clair_reports_to_the_explicit_target_repository(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_command(bin_dir, "get-resource", 'printf "%s\\n" "$SNAPSHOT_RESOURCE"\n')
    attestation = {
        "predicate": {
            "buildConfig": {
                "tasks": [
                    {
                        "name": "clair-scan",
                        "results": [
                            {"name": "REPORTS", "value": '["sha256:report-one", "sha256:report-two"]'}
                        ],
                    }
                ]
            }
        }
    }
    payload = base64.b64encode(json.dumps(attestation).encode()).decode()
    _make_command(bin_dir, "cosign", f"printf '%s\\n' '{{\"payload\": \"{payload}\"}}'\n")
    _make_command(bin_dir, "skopeo", 'printf "%s\\n" "$*" >> "$CAPTURE_DIR/skopeo.txt"\n')

    result = subprocess.run(
        ["bash", "-c", _task_script("copy-clair-scan-results")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SNAPSHOT": "ai-tenant/rhaiis-snapshot",
            "TARGET_REPO": "quay.io/aipcc/rhaiis/reports",
            "SNAPSHOT_RESOURCE": json.dumps(
                {"components": [{"containerImage": "quay.io/aipcc/rhaiis/cuda@sha256:image"}]}
            ),
            "CAPTURE_DIR": str(tmp_path),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "skopeo.txt").read_text().splitlines() == [
        "copy docker://quay.io/aipcc/rhaiis/cuda@sha256:report-one docker://quay.io/aipcc/rhaiis/reports@sha256:report-one",
        "copy docker://quay.io/aipcc/rhaiis/cuda@sha256:report-two docker://quay.io/aipcc/rhaiis/reports@sha256:report-two",
    ]


def _run_notification(tmp_path, artifacts, curl_exit_code=0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_command(bin_dir, "get-resource", 'printf "%s\\n" "$RELEASE_ARTIFACTS"\n')
    _make_command(
        bin_dir,
        "curl",
        '''count_file="$CAPTURE_DIR/curl-count"
count=$(cat "$count_file" 2>/dev/null || echo 0)
count=$((count + 1))
printf '%s' "$count" > "$count_file"
payload_file=
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--data-binary" ]; then
    shift
    payload_file=${1#@}
  fi
  shift
done
cp "$payload_file" "$CAPTURE_DIR/payload-${count}.json"
exit "$CURL_EXIT_CODE"
''',
    )
    return subprocess.run(
        ["bash", "-c", _task_script("notify-release")],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "RELEASE": "ai-tenant/rhaiis-release",
            "RELEASE_ARTIFACTS": json.dumps(artifacts),
            "CAPTURE_DIR": str(tmp_path),
            "CURL_EXIT_CODE": str(curl_exit_code),
        },
    )


def test_notifies_once_per_quay_repository_with_unique_tags(tmp_path):
    result = _run_notification(
        tmp_path,
        {
            "images": [
                {
                    "urls": [
                        "quay.io/aipcc/rhaiis/cuda:3.5.0",
                        "quay.io/aipcc/rhaiis/cuda:3.5.0@sha256:one",
                        "quay.io/aipcc/rhaiis/cuda:3.5.0-1700000000",
                        "quay.io/aipcc/rhaiis/cuda@sha256:one",
                        "registry.redhat.io/rhaii/cuda:3.5.0",
                    ]
                },
                {"urls": ["quay.io/aipcc/rhaiis/rocm:3.5.0"]},
            ]
        },
    )

    assert result.returncode == 0, result.stderr
    payloads = sorted(
        (json.loads(path.read_text()) for path in tmp_path.glob("payload-*.json")),
        key=lambda payload: payload["repository"],
    )
    assert payloads == [
        {
            "repository": "aipcc/rhaiis/cuda",
            "docker_url": "quay.io/aipcc/rhaiis/cuda",
            "updated_tags": ["3.5.0", "3.5.0-1700000000"],
        },
        {
            "repository": "aipcc/rhaiis/rocm",
            "docker_url": "quay.io/aipcc/rhaiis/rocm",
            "updated_tags": ["3.5.0"],
        },
    ]


def test_notification_stops_after_a_nonzero_http_failure(tmp_path):
    result = _run_notification(
        tmp_path,
        {"images": [{"urls": ["quay.io/aipcc/rhaiis/cuda:3.5.0"]}]},
        curl_exit_code=22,
    )

    assert result.returncode == 22
    assert (tmp_path / "curl-count").read_text() == "1"


def test_pipeline_keeps_the_runtime_interface_and_reports_task_statuses():
    pipeline = _pipeline()
    params = pipeline["spec"]["params"]
    assert [param["name"] for param in params] == [
        "release",
        "releasePlan",
        "snapshot",
        "target-repo",
    ]
    assert all("default" not in param for param in params)

    tasks = {task["name"]: task for task in pipeline["spec"]["tasks"]}
    clair_params = {param["name"]: param["value"] for param in tasks["copy-clair-scan-results"]["params"]}
    assert clair_params == {"snapshot": "$(params.snapshot)", "target-repo": "$(params.target-repo)"}
    assert {param["name"]: param["value"] for param in tasks["notify-release"]["params"]} == {
        "release": "$(params.release)"
    }

    reporter = pipeline["spec"]["finally"][0]
    reporter_params = {param["name"]: param["value"] for param in reporter["params"]}
    assert reporter_params == {
        "clair-copy-status": "$(tasks.copy-clair-scan-results.status)",
        "notification-status": "$(tasks.notify-release.status)",
        "release": "$(params.release)",
        "pipeline-run-name": "$(context.pipelineRun.name)",
    }
