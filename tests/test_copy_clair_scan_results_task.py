from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "copy-clair-scan-results.yaml"


def test_copy_clair_task_uses_digest_pinned_release_service_utils():
    task = yaml.safe_load(TASK.read_text())
    image = task["spec"]["steps"][0]["image"]

    assert image == (
        "quay.io/konflux-ci/release-service-utils@sha256:"
        "6ce9a089345bcb8d650d3acb1fbd449e065bd43341e9eaeaec4540a981a5c5a2"
    )
