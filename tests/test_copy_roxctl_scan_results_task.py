import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "copy-roxctl-scan-results.yaml"
PIPELINE = ROOT / "pipelines" / "copy-roxctl-scan-results.yaml"
CLAIR_TASK = ROOT / "tasks" / "copy-clair-scan-results.yaml"
CLAIR_PIPELINE = ROOT / "pipelines" / "copy-clair-scan-results.yaml"
ROX_REPORT_MEDIA_TYPE = "application/vnd.redhat.rox-report+json"
IMAGE_DIGEST = "sha256:" + "a" * 64
SCANNED_DIGEST = "sha256:" + "b" * 64
OTHER_DIGEST = "sha256:" + "d" * 64


def _step():
    task = yaml.safe_load(TASK.read_text())
    return task["spec"]["steps"][0]


def _jq_reports_program():
    match = re.search(r"JQ_REPORTS='\n(.*?)\n'", _step()["script"], re.S)
    assert match, "the copy-results script no longer defines JQ_REPORTS as a block"
    return match.group(1)


def _attestation_line(tasks):
    payload = {
        "_type": "https://in-toto.io/Statement/v0.1",
        # in-toto records the digest as bare hex, without the sha256: prefix.
        "subject": [{"name": "image", "digest": {"sha256": "a" * 64}}],
        "predicate": {"buildConfig": {"tasks": tasks}},
    }
    return json.dumps({"payload": base64.b64encode(json.dumps(payload).encode()).decode()})


def test_copy_roxctl_task_uses_digest_pinned_release_service_utils():
    task = yaml.safe_load(TASK.read_text())

    assert task["spec"]["steps"][0]["image"] == (
        "quay.io/konflux-ci/release-service-utils@sha256:"
        "6ce9a089345bcb8d650d3acb1fbd449e065bd43341e9eaeaec4540a981a5c5a2"
    )


def test_copy_roxctl_task_keeps_the_clair_copier_interface_and_credentials():
    clair = yaml.safe_load(CLAIR_TASK.read_text())["spec"]
    roxctl = yaml.safe_load(TASK.read_text())["spec"]

    assert [p["name"] for p in roxctl["params"]] == [p["name"] for p in clair["params"]]
    assert [p.get("default") for p in roxctl["params"]] == [
        p.get("default") for p in clair["params"]
    ]
    assert roxctl["volumes"] == clair["volumes"]


def test_copy_roxctl_pipeline_keeps_the_clair_copier_interface():
    clair = yaml.safe_load(CLAIR_PIPELINE.read_text())["spec"]
    roxctl = yaml.safe_load(PIPELINE.read_text())["spec"]

    assert [p["name"] for p in roxctl["params"]] == [p["name"] for p in clair["params"]]
    assert roxctl["tasks"][0]["taskRef"]["params"][-1] == {
        "name": "pathInRepo",
        "value": "tasks/copy-roxctl-scan-results.yaml",
    }


def test_copy_task_pushes_only_roxctl_report_artifacts():
    script = _step()["script"]

    assert 'MEDIA_TYPE=%s' % ("'" + ROX_REPORT_MEDIA_TYPE + "'") in script
    assert "skopeo copy --preserve-digests" in script


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_report_digests_come_from_the_roxctl_scan_reports_map(tmp_path):
    attestation = tmp_path / "attestation.jsonl"
    attestation.write_text(
        "\n".join(
            [
                _attestation_line(
                    [
                        # A Clair report in the same provenance must not be copied as a
                        # roxctl report.
                        {
                            "name": "clair-scan",
                            "results": [
                                {
                                    "name": "REPORTS",
                                    "value": json.dumps(["sha256:" + "9" * 64]),
                                }
                            ],
                        },
                        {
                            "name": "roxctl-scan",
                            "results": [
                                {
                                    "name": "REPORTS",
                                    "value": json.dumps(
                                        {
                                            SCANNED_DIGEST: "sha256:" + "c" * 64,
                                            OTHER_DIGEST: "sha256:" + "e" * 64,
                                        }
                                    ),
                                }
                            ],
                        },
                    ]
                )
            ]
        )
        + "\n"
    )

    proc = subprocess.run(
        ["jq", "-r", "--arg", "subject", "a" * 64, _jq_reports_program(), str(attestation)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "%s %s" % (SCANNED_DIGEST, "sha256:" + "c" * 64),
        "%s %s" % (OTHER_DIGEST, "sha256:" + "e" * 64),
    ]


STUB = '''#!/usr/bin/env python3
"""Stand-in for skopeo, cosign and get-resource: serves a fixture registry."""
import json
import os
import sys

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
registry = json.load(open(os.environ["STUB_REGISTRY"]))

if name == "get-resource":
    print(registry["snapshot"])
elif name == "cosign":
    sys.stdout.write(registry["attestation"])
elif args[0] == "inspect":
    print(json.dumps(registry["manifests"][args[-1].rsplit("@", 1)[1]]))
elif args[0] == "copy":
    with open(os.environ["STUB_COPY_LOG"], "a") as log:
        log.write(args[-2] + " -> " + args[-1] + "\\n")
else:
    sys.exit("unsupported call: %s %s" % (name, args))
'''


def _artifact(subject=None, media_type=ROX_REPORT_MEDIA_TYPE):
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": media_type,
        "layers": [{"mediaType": media_type, "digest": "sha256:" + "e" * 64}],
    }
    if subject:
        manifest["subject"] = {"digest": subject}
    return manifest


def _run_copy_step(tmp_path, artifact, known_digests, attestation):
    """Run the copy-results script with skopeo and get-resource replaced by stubs."""
    manifests = {IMAGE_DIGEST: {"mediaType": "application/vnd.oci.image.manifest.v1+json"}}
    manifests.update({digest: artifact for digest in known_digests})

    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "manifests": manifests,
                "attestation": attestation,
                "snapshot": json.dumps(
                    {
                        "components": [
                            {
                                "name": "image",
                                "containerImage": "quay.io/example/image@" + IMAGE_DIGEST,
                            }
                        ]
                    }
                ),
            }
        )
    )
    copy_log = tmp_path / "copies.log"
    copy_log.write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("skopeo", "cosign", "get-resource"):
        stub = bin_dir / tool
        stub.write_text(STUB)
        stub.chmod(0o755)

    pull = tmp_path / "creds-pull"
    push = tmp_path / "creds-push"
    pull.mkdir()
    push.mkdir()
    for creds in (pull, push):
        (creds / "config.json").write_text('{"auths": {}}')

    # The task hardcodes its credentials, its workspace and its scratch file.
    script = _step()["script"]
    for old, new in (
        ("/creds/pull", str(pull)),
        ("/creds/push", str(push)),
        ("/tmp/report-manifest.json", str(tmp_path / "report-manifest.json")),
    ):
        script = script.replace(old, new)
    script_path = tmp_path / "copy-results.sh"
    script_path.write_text(script)

    env = {
        "PATH": str(bin_dir) + ":" + os.environ["PATH"],
        "STUB_REGISTRY": str(registry),
        "STUB_COPY_LOG": str(copy_log),
        "SNAPSHOT": "snapshot-name",
        "RELEASE": "release-name",
        "TARGET_REPO": "quay.io/example/target",
    }
    proc = subprocess.run(
        ["bash", str(script_path)], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    return {"returncode": proc.returncode, "output": proc.stdout + proc.stderr,
            "copies": copy_log.read_text().splitlines()}


def test_copy_step_copies_the_report_of_the_manifest_the_map_names(tmp_path):
    report = "sha256:" + "c" * 64
    result = _run_copy_step(
        tmp_path,
        _artifact(subject=SCANNED_DIGEST),
        [report],
        _attestation_line(
            [
                {
                    "name": "roxctl-scan",
                    "results": [
                        {"name": "REPORTS", "value": json.dumps({SCANNED_DIGEST: report})}
                    ],
                }
            ]
        )
        + "\n",
    )

    assert result["returncode"] == 0, result["output"]
    assert result["copies"] == [
        "docker://quay.io/example/image@%s -> docker://quay.io/example/target@%s" % (report, report)
    ]


def test_copy_step_refuses_a_report_attached_to_another_manifest(tmp_path):
    report = "sha256:" + "c" * 64
    result = _run_copy_step(
        tmp_path,
        # Attached to the image itself, not to the manifest the REPORTS map names.
        _artifact(subject=IMAGE_DIGEST),
        [report],
        _attestation_line(
            [
                {
                    "name": "roxctl-scan",
                    "results": [
                        {"name": "REPORTS", "value": json.dumps({SCANNED_DIGEST: report})}
                    ],
                }
            ]
        )
        + "\n",
    )

    assert result["returncode"] == 1
    assert "not to " + SCANNED_DIGEST in result["output"]
    assert result["copies"] == []


def test_copy_step_refuses_a_report_without_a_subject(tmp_path):
    report = "sha256:" + "c" * 64
    result = _run_copy_step(
        tmp_path,
        _artifact(),
        [report],
        _attestation_line(
            [
                {
                    "name": "roxctl-scan",
                    "results": [
                        {"name": "REPORTS", "value": json.dumps({SCANNED_DIGEST: report})}
                    ],
                }
            ]
        )
        + "\n",
    )

    assert result["returncode"] == 1
    assert "attached to no subject" in result["output"]
    assert result["copies"] == []
