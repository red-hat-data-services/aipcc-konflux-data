import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "check-roxctl-cves.yaml"
PIPELINE = ROOT / "pipelines" / "check-roxctl-cves-its.yaml"
CLAIR_TASK = ROOT / "tasks" / "check-clair-cves.yaml"
CLAIR_PIPELINE = ROOT / "pipelines" / "check-clair-cves-its.yaml"
RELEASE_SERVICE_UTILS = (
    "quay.io/konflux-ci/release-service-utils@sha256:"
    "6ce9a089345bcb8d650d3acb1fbd449e065bd43341e9eaeaec4540a981a5c5a2"
)
IMAGE_DIGEST = "sha256:" + "a" * 64
RESULT_NAMES = ["TEST_OUTPUT", "CVE_COUNT", "CVE_SUMMARY", "SCANNED_IMAGE"]


def _task():
    return yaml.safe_load(TASK.read_text())


def _step(name):
    return next(step for step in _task()["spec"]["steps"] if step["name"] == name)


def _jq_reports_program():
    match = re.search(r"JQ_REPORTS='\n(.*?)\n'", _step("fetch-reports")["script"], re.S)
    assert match, "the fetch-reports script no longer defines JQ_REPORTS as a block"
    return match.group(1)


def _jq_expected_digests_program():
    match = re.search(
        r"JQ_EXPECTED_DIGESTS='\n(.*?)\n'", _step("fetch-reports")["script"], re.S
    )
    assert match, "the fetch-reports script no longer defines JQ_EXPECTED_DIGESTS as a block"
    return match.group(1)


def _digest(char):
    return "sha256:" + char * 64


def _attestation_line(subject_digest, reports):
    payload = {
        "_type": "https://in-toto.io/Statement/v0.1",
        # in-toto records the digest as bare hex, without the sha256: prefix.
        "subject": [{"name": "image", "digest": {"sha256": subject_digest.split(":", 1)[-1]}}],
        "predicate": {
            "buildConfig": {
                "tasks": [
                    {
                        "name": "roxctl-scan",
                        "results": [{"name": "REPORTS", "value": reports}],
                    }
                ]
            }
        },
    }
    return json.dumps({"payload": base64.b64encode(json.dumps(payload).encode()).decode()})


def _run_report_step(
    tmp_path, reports, patched="Critical,High", unpatched="", enforce="false", error=None
):
    """Run the report-cves script against report blobs and return its results."""
    script = _step("report-cves")["script"]
    workspace = tmp_path / "workspace"
    (workspace / "reports").mkdir(parents=True)
    for name, content in reports.items():
        (workspace / "reports" / name).write_text(content)
    (workspace / "image-ref").write_text("quay.io/example/image@" + IMAGE_DIGEST)
    if error is not None:
        (workspace / "error").write_text(error)
    # The task hardcodes its workspace; point it at the fixture directory.
    script = script.replace("/workspace", str(workspace))

    result_paths = {}
    for result in RESULT_NAMES:
        path = tmp_path / f"{result}.txt"
        path.write_text("")
        result_paths[result] = path
        script = script.replace(f"$(results.{result}.path)", str(path))

    script_path = tmp_path / "report-cves.py"
    script_path.write_text(script)
    env = {
        "PATH": os.environ["PATH"],
        "PATCHED_SEVERITIES": patched,
        "UNPATCHED_SEVERITIES": unpatched,
        "CVE_LEEWAY": "Critical=6,High=29",
        "ENFORCE": enforce,
    }
    proc = subprocess.run(
        [sys.executable, str(script_path)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return {
        "test_output": json.loads(result_paths["TEST_OUTPUT"].read_text()),
        "cve_count": result_paths["CVE_COUNT"].read_text(),
        "cve_summary": result_paths["CVE_SUMMARY"].read_text(),
        "scanned_image": result_paths["SCANNED_IMAGE"].read_text(),
        "log": proc.stdout,
    }


def _report(*entries):
    return json.dumps({"roxctl_new": list(entries)})


def test_check_roxctl_task_keeps_the_clair_its_interface():
    clair = yaml.safe_load(CLAIR_TASK.read_text())["spec"]
    roxctl = _task()["spec"]

    assert [p["name"] for p in roxctl["params"]] == [p["name"] for p in clair["params"]]
    assert [p.get("default") for p in roxctl["params"]] == [
        p.get("default") for p in clair["params"]
    ]
    assert [r["name"] for r in roxctl["results"]] == [r["name"] for r in clair["results"]]


def test_check_roxctl_its_pipeline_keeps_the_clair_its_interface():
    clair = yaml.safe_load(CLAIR_PIPELINE.read_text())["spec"]
    roxctl = yaml.safe_load(PIPELINE.read_text())["spec"]

    assert [p["name"] for p in roxctl["params"]] == [p["name"] for p in clair["params"]]
    assert [p.get("default") for p in roxctl["params"]] == [
        p.get("default") for p in clair["params"]
    ]
    assert [r["name"] for r in roxctl["results"]] == [r["name"] for r in clair["results"]]
    assert roxctl["tasks"][0]["taskRef"]["params"][-1] == {
        "name": "pathInRepo",
        "value": "tasks/check-roxctl-cves.yaml",
    }


def test_both_steps_are_digest_pinned_to_release_service_utils():
    assert [step["image"] for step in _task()["spec"]["steps"]] == [
        RELEASE_SERVICE_UTILS,
        RELEASE_SERVICE_UTILS,
    ]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_expected_scan_digests_follow_get_image_manifests(tmp_path):
    # index: get_image_manifests lowercases the architecture and the last manifest of an
    # architecture wins, so this is the exact set roxctl-scan keys REPORTS by.
    index = tmp_path / "raw-manifest.json"
    index.write_text(
        json.dumps(
            {
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {"digest": _digest("a"), "platform": {"architecture": "amd64"}},
                    {"digest": _digest("b"), "platform": {"architecture": "arm64"}},
                    {"digest": _digest("c"), "platform": {"architecture": "AMD64"}},
                ],
            }
        )
    )

    def expected(path):
        proc = subprocess.run(
            ["jq", "-r", "--arg", "subject", IMAGE_DIGEST, _jq_expected_digests_program(), path],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return sorted(proc.stdout.split())

    assert expected(str(index)) == [_digest("b"), _digest("c")]

    # single manifest: get_image_manifests returns the image digest itself.
    single = tmp_path / "single.json"
    single.write_text(json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json"}))
    assert expected(str(single)) == [IMAGE_DIGEST]


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")
def test_report_map_pairs_come_only_from_the_statement_about_this_image(tmp_path):
    attestation = tmp_path / "attestation.jsonl"
    attestation.write_text(
        "\n".join(
            [
                _attestation_line(
                    IMAGE_DIGEST,
                    json.dumps({_digest("b"): _digest("c"), _digest("d"): _digest("e")}),
                ),
                # A statement about a different image must not contribute evidence.
                _attestation_line(_digest("f"), json.dumps({_digest("f"): _digest("1")})),
                # A malformed REPORTS value must not abort jq part-way.
                _attestation_line(IMAGE_DIGEST, "not-json"),
                # A report map in the Clair shape is not a roxctl REPORTS map.
                _attestation_line(IMAGE_DIGEST, json.dumps([_digest("2")])),
            ]
        )
        + "\n"
    )

    proc = subprocess.run(
        [
            "jq",
            "-r",
            "--arg",
            "subject",
            IMAGE_DIGEST.split(":", 1)[1],
            _jq_reports_program(),
            str(attestation),
        ],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "%s %s" % (_digest("b"), _digest("c")),
        "%s %s" % (_digest("d"), _digest("e")),
    ]


def test_report_cves_buckets_findings_the_way_conforma_does(tmp_path):
    result = _run_report_step(
        tmp_path,
        {
            "report-1-1.json": _report(
                {
                    "cve": "CVE-2025-0001",
                    "severity": "CRITICAL_VULNERABILITY_SEVERITY",
                    "fixedBy": "0:1.2.3-1.el9",
                    "advisory": [{"name": "RHSA-2025:0001", "link": "https://example/1"}],
                    "components": [
                        {"component": "openssl", "version": "1.2.2-1.el9", "source": "var/lib/rpm"}
                    ],
                },
                {
                    "cve": "CVE-2025-0002",
                    "severity": "IMPORTANT_VULNERABILITY_SEVERITY",
                    "fixedBy": "",
                    "components": [
                        {"component": "glibc", "version": "2.34", "source": "var/lib/rpm"}
                    ],
                },
                {
                    "cve": "CVE-2025-0003",
                    "severity": "MODERATE_VULNERABILITY_SEVERITY",
                    "fixedBy": "0:4.0-1",
                    "components": [{"component": "zlib", "version": "4.0"}],
                },
            ),
            # The same CVE repeats in the report of the other architecture.
            "report-2-1.json": _report(
                {
                    "cve": "CVE-2025-0001",
                    "severity": "CRITICAL_VULNERABILITY_SEVERITY",
                    "fixedBy": "0:1.2.3-1.el9",
                    "components": [
                        {"component": "openssl", "version": "1.2.2-1.el9", "source": "var/lib/rpm"}
                    ],
                }
            ),
        },
    )

    # Only the patched Critical CVE is at the stock thresholds.
    assert result["test_output"]["result"] == "WARNING"
    assert result["cve_count"] == "1"
    assert result["cve_summary"] == "Critical=1 patched=1 unpatched=0 blocking=1 pending=0"
    assert "CVE-2025-0001" in result["log"]
    assert "CVE-2025-0002" not in result["log"]
    assert "RHSA-2025:0001" in result["log"]
    assert result["scanned_image"] == "quay.io/example/image@" + IMAGE_DIGEST


def test_report_cves_enforces_unpatched_findings_when_configured(tmp_path):
    report = {
        "report-1-1.json": _report(
            {
                "cve": "CVE-2025-0002",
                "severity": "IMPORTANT_VULNERABILITY_SEVERITY",
                "fixedBy": "",
                "components": [{"component": "glibc", "version": "2.34"}],
            },
            {
                "cve": "CVE-2025-0004",
                "severity": "UNKNOWN_VULNERABILITY_SEVERITY",
                "fixedBy": "",
            },
        )
    }

    result = _run_report_step(
        tmp_path, report, patched="", unpatched="High,Unknown", enforce="true"
    )

    assert result["test_output"]["result"] == "FAILURE"
    assert result["test_output"]["failures"] == 2
    assert result["cve_count"] == "2"
    assert result["cve_summary"] == "High=1 Unknown=1 patched=0 unpatched=2 blocking=2 pending=0"


def test_a_report_without_roxctl_new_is_an_error_not_a_clean_image(tmp_path):
    result = _run_report_step(
        tmp_path,
        {
            # Clair-shaped and arbitrary payloads are not roxctl reports.
            "report-1-1.json": json.dumps({"vulnerabilities": {}}),
            "report-2-1.json": json.dumps({"unexpected": []}),
        },
    )

    assert result["test_output"]["result"] == "ERROR"
    assert "roxctl_new" in result["test_output"]["note"]
    assert result["cve_count"] == "0"


def test_error_from_an_earlier_step_is_reported_as_error(tmp_path):
    result = _run_report_step(
        tmp_path, {}, error="no roxctl-scan REPORTS entry found in the attestation"
    )

    assert result["test_output"]["result"] == "ERROR"
    assert result["test_output"]["note"] == "no roxctl-scan REPORTS entry found in the attestation"


ROX_REPORT_MEDIA_TYPE = "application/vnd.redhat.rox-report+json"
STUB = '''#!/usr/bin/env python3
"""Stand-in for skopeo and cosign: serves a fixture registry from JSON."""
import json
import os
import sys

name = os.path.basename(sys.argv[0])
args = sys.argv[1:]
registry = json.load(open(os.environ["STUB_REGISTRY"]))

if name == "cosign":
    sys.stdout.write(registry["attestation"])
elif args[0] == "inspect":
    print(json.dumps(registry["manifests"][args[-1].rsplit("@", 1)[1]]))
elif args[0] == "copy":
    digest = args[-2].rsplit("@", 1)[1]
    with open(os.environ["STUB_COPY_LOG"], "a") as log:
        log.write(args[-2] + " -> " + args[-1] + "\\n")
    layout = args[-1][len("oci:"):].rsplit(":", 1)[0]
    blobs = os.path.join(layout, "blobs", "sha256")
    os.makedirs(blobs, exist_ok=True)
    manifest = registry["manifests"][digest]
    with open(os.path.join(blobs, digest.split(":", 1)[1]), "w") as fh:
        fh.write(json.dumps(manifest))
    for layer in manifest.get("layers", []):
        with open(os.path.join(blobs, layer["digest"].split(":", 1)[1]), "w") as fh:
            fh.write(registry["blobs"][layer["digest"]])
    with open(os.path.join(layout, "index.json"), "w") as fh:
        json.dump({"schemaVersion": 2, "manifests": [{"digest": digest}]}, fh)
else:
    sys.exit("unsupported call: %s %s" % (name, args))
'''


def _report_artifact(subject, report):
    """An OCI artifact manifest as oras attach writes it, plus its report blob."""
    layer = "sha256:" + hashlib.sha256(report.encode()).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": ROX_REPORT_MEDIA_TYPE,
        "config": {"mediaType": "application/vnd.oci.empty.v1+json", "digest": _digest("0")},
        "layers": [{"mediaType": ROX_REPORT_MEDIA_TYPE, "digest": layer}],
    }
    if subject:
        manifest["subject"] = {"mediaType": "application/vnd.oci.image.manifest.v1+json",
                               "digest": subject}
    return manifest, {layer: report}


def _index(*manifests):
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": list(manifests),
    }


def _platform(digest, architecture):
    return {
        "digest": digest,
        "platform": {"architecture": architecture, "os": "linux"},
    }


def _run_fetch_step(tmp_path, raw_manifest, attestation, reports):
    """Run the fetch-reports script with skopeo and cosign replaced by fixture stubs."""
    manifests = {IMAGE_DIGEST: raw_manifest}
    blobs = {}
    for digest, (manifest, report_blobs) in reports.items():
        manifests[digest] = manifest
        blobs.update(report_blobs)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    auth = tmp_path / "auth"
    auth.mkdir()
    (auth / "config.json").write_text('{"auths": {}}')
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({"manifests": manifests, "blobs": blobs, "attestation": attestation})
    )
    copy_log = tmp_path / "copies.log"
    copy_log.write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("skopeo", "cosign"):
        stub = bin_dir / tool
        stub.write_text(STUB)
        stub.chmod(0o755)

    # The task hardcodes /workspace and /auth; point them at the fixtures.
    script = _step("fetch-reports")["script"]
    script = script.replace("/workspace", str(workspace)).replace("/auth", str(auth))
    script_path = tmp_path / "fetch-reports.sh"
    script_path.write_text(script)

    env = {
        "PATH": str(bin_dir) + ":" + os.environ["PATH"],
        "STUB_REGISTRY": str(registry),
        "STUB_COPY_LOG": str(copy_log),
        "COMPONENT": "image",
        "SNAPSHOT": json.dumps(
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
    proc = subprocess.run(
        ["bash", str(script_path)], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    error_file = workspace / "error"
    reports_dir = workspace / "reports"
    return {
        "error": error_file.read_text() if error_file.exists() else "",
        "reports": sorted(p.name for p in reports_dir.glob("*")) if reports_dir.exists() else [],
        "saved": {
            p.name: p.read_text()
            for p in (reports_dir.glob("*") if reports_dir.exists() else [])
        },
        "log": proc.stdout,
    }


def _two_arch_image():
    return _index(_platform(_digest("b"), "amd64"), _platform(_digest("c"), "arm64"))


def test_fetch_reports_saves_one_report_per_scanned_manifest(tmp_path):
    plain, plain_blobs = _report_artifact(_digest("b"), _report())
    other, other_blobs = _report_artifact(_digest("c"), _report())
    result = _run_fetch_step(
        tmp_path,
        _two_arch_image(),
        _attestation_line(
            IMAGE_DIGEST,
            json.dumps({_digest("b"): _digest("1"), _digest("c"): _digest("2")}),
        )
        + "\n",
        {
            _digest("1"): (plain, plain_blobs),
            _digest("2"): (other, other_blobs),
        },
    )

    assert result["error"] == ""
    assert result["reports"] == ["report-1-1.json", "report-2-1.json"]
    assert all(json.loads(blob) == {"roxctl_new": []} for blob in result["saved"].values())


def test_fetch_reports_fails_when_a_scanned_manifest_has_no_report(tmp_path):
    plain, plain_blobs = _report_artifact(_digest("b"), _report())
    result = _run_fetch_step(
        tmp_path,
        _two_arch_image(),
        _attestation_line(IMAGE_DIGEST, json.dumps({_digest("b"): _digest("1")})) + "\n",
        {_digest("1"): (plain, plain_blobs)},
    )

    # One architecture with a report and one without is not a clean image.
    assert "reported no scan for " + _digest("c") in result["error"]
    assert "partial" in result["error"]
    assert result["reports"] == []


def test_fetch_reports_fails_when_a_report_is_attached_to_another_manifest(tmp_path):
    # The map names the report for the arm64 manifest, but the artifact is attached to
    # the amd64 manifest, so it is not that manifest's scan evidence.
    plain, plain_blobs = _report_artifact(_digest("b"), _report())
    other, other_blobs = _report_artifact(_digest("b"), _report())
    result = _run_fetch_step(
        tmp_path,
        _two_arch_image(),
        _attestation_line(
            IMAGE_DIGEST,
            json.dumps({_digest("b"): _digest("1"), _digest("c"): _digest("2")}),
        )
        + "\n",
        {
            _digest("1"): (plain, plain_blobs),
            _digest("2"): (other, other_blobs),
        },
    )

    assert "is attached to " + _digest("b") in result["error"]
    assert "names it for " + _digest("c") in result["error"]


def test_fetch_reports_fails_when_a_report_has_no_subject(tmp_path):
    subjectless, subjectless_blobs = _report_artifact(None, _report())
    result = _run_fetch_step(
        tmp_path,
        _index(_platform(_digest("b"), "amd64")),
        _attestation_line(IMAGE_DIGEST, json.dumps({_digest("b"): _digest("1")})) + "\n",
        {_digest("1"): (subjectless, subjectless_blobs)},
    )

    assert "is attached to no subject" in result["error"]


def test_fetch_reports_fails_when_the_map_names_a_manifest_of_another_image(tmp_path):
    plain, plain_blobs = _report_artifact(_digest("b"), _report())
    stray, stray_blobs = _report_artifact(_digest("9"), _report())
    result = _run_fetch_step(
        tmp_path,
        _index(_platform(_digest("b"), "amd64")),
        _attestation_line(
            IMAGE_DIGEST,
            json.dumps({_digest("b"): _digest("1"), _digest("9"): _digest("2")}),
        )
        + "\n",
        {_digest("1"): (plain, plain_blobs), _digest("2"): (stray, stray_blobs)},
    )

    assert "not a manifest roxctl-scan would scan for it" in result["error"]
    assert _digest("9") in result["error"]


def test_fetch_reports_fails_when_two_statements_disagree_about_one_manifest(tmp_path):
    plain, plain_blobs = _report_artifact(_digest("b"), _report())
    rival, rival_blobs = _report_artifact(_digest("b"), _report())
    result = _run_fetch_step(
        tmp_path,
        _index(_platform(_digest("b"), "amd64")),
        "\n".join(
            [
                _attestation_line(IMAGE_DIGEST, json.dumps({_digest("b"): _digest("1")})),
                _attestation_line(IMAGE_DIGEST, json.dumps({_digest("b"): _digest("2")})),
            ]
        )
        + "\n",
        {_digest("1"): (plain, plain_blobs), _digest("2"): (rival, rival_blobs)},
    )

    assert "more than one roxctl report for " + _digest("b") in result["error"]
