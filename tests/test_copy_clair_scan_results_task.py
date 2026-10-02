from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "copy-clair-scan-results.yaml"


def test_copy_clair_task_uses_digest_pinned_release_service_utils():
    task = yaml.safe_load(TASK.read_text())
    image = task["spec"]["steps"][0]["image"]

    assert image == (
        "quay.io/konflux-ci/release-service-utils@sha256:"
        "067f6f54dfa13e1c01b4a5b44aa82dbe027a003b8e0ffa21d77004bcac6b8370"
    )
