import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "acs-scan-diff.yaml"
ANNOTATE_TASK = ROOT / "tasks" / "acs-scan-diff-annotate.yaml"
PIPELINE = ROOT / "pipelines" / "acs-scan-diff.yaml"
EXAMPLE_RUN = ROOT / "pipelineruns" / "acs-scan-diff.yaml"
CODE_REVISION = "0123456789abcdef0123456789abcdef01234567"
PIPELINE_RUN = "acs-scan-diff-run"
COMPONENT = "my-component"
ROXCTL_IMAGE = "registry.redhat.io/advanced-cluster-security/rhacs-roxctl-rhel9@sha256:98f01d623e119acbbc211bdeb36192b619969aa149d9c66bab8034eeef84c861"
STARTED = "2026-10-27T10:00:00Z"
FINISHED = "2026-10-27T10:05:00Z"
FRESH_EVIDENCE = ("ACS-observed CVE delta; not verified remediation, ECP or "
                  "release approval; live Central DB is not frozen")
# Build mode deliberately does not require the forced-scan flags in the client.
ROXCTL_HELP_CLIENT_DEFAULTS = """Usage:
  roxctl image scan [flags]

Flags:
  -i, --image string
      --timeout duration
"""
ROXCTL_HELP_FORCED = ROXCTL_HELP_CLIENT_DEFAULTS + """      --force
  -a, --include-snoozed
"""


def _digest(text):
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


BASELINE_REF = "quay.io/example/baseline@" + _digest("baseline-amd64")
CANDIDATE_REF = "quay.io/example/candidate@" + _digest("candidate-amd64")


def _load(path):
    return yaml.safe_load(path.read_text())


def _step(task, name):
    return next(step for step in task["spec"]["steps"] if step["name"] == name)


def _roxctl(bin_dir, calls_file, help_text):
    """Write a roxctl stub that records argv and returns a minimal report."""
    roxctl = bin_dir / "roxctl"
    roxctl.write_text(f"""#!{sys.executable}
import json
import sys

args = sys.argv[1:]
with open({str(calls_file)!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:1] == ["version"]:
    print("roxctl version test")
elif "--help" in args:
    sys.stdout.write({help_text!r})
else:
    print(json.dumps({{"scan": {{}}}}))
""")
    roxctl.chmod(0o755)


def _run_scan(tmp_path, scan_mode, help_text):
    """Execute the scan step's bash script against the stubbed client."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_file = tmp_path / "calls.jsonl"
    _roxctl(bin_dir, calls_file, help_text)
    (tmp_path / "acs-scan-plan.tsv").write_text(
        f"baseline\tamd64\t{BASELINE_REF}\ncandidate\tamd64\t{CANDIDATE_REF}\n")
    script_file = tmp_path / "scan.sh"
    script_file.write_text(_step(_load(TASK), "scan")["script"])
    environment = os.environ | {"SCAN_MODE": scan_mode,
                                "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    completed = subprocess.run(["bash", str(script_file)], cwd=tmp_path, capture_output=True,
                               text=True, env=environment, check=False, timeout=60)
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()] \
        if calls_file.exists() else []
    return completed, calls


def _scan_calls(calls):
    return [call for call in calls if call[:2] == ["image", "scan"] and "--help" not in call]


def _report(ref, scan_time, cves):
    return {
        "id": ref.split("@")[1],
        "notes": [],
        "scan": {
            "operatingSystem": "rhel:9",
            "scanTime": scan_time,
            "components": [{"name": "openssl", "vulns": [{"cve": cve} for cve in cves]}],
        },
    }


def _run_compare(tmp_path, scan_mode, scan_time):
    """Execute the compare step's embedded Python with one platform per side."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifests = {"baseline": [{"platform": "linux/amd64", "ref": BASELINE_REF}],
                 "candidate": [{"platform": "linux/amd64", "ref": CANDIDATE_REF}]}
    (tmp_path / "acs-manifests.json").write_text(json.dumps(manifests))
    (tmp_path / "acs-client-version.txt").write_text("roxctl version test\n")
    for prefix, ref, cves in (("acs-baseline-amd64", BASELINE_REF, ["CVE-2025-1111"]),
                              ("acs-candidate-amd64", CANDIDATE_REF, ["CVE-2026-2222"])):
        (tmp_path / (prefix + ".json")).write_text(json.dumps(_report(ref, scan_time, cves)))
        (tmp_path / (prefix + ".started")).write_text(STARTED + "\n")
        (tmp_path / (prefix + ".finished")).write_text(FINISHED + "\n")
    results = tmp_path / "results"
    results.mkdir()
    script_file = tmp_path / "compare.py"
    script_file.write_text(_step(_load(TASK), "compare")["script"]
                           .replace("/tekton/results", str(results)))
    environment = os.environ | {
        "CODE_REVISION": CODE_REVISION, "PIPELINE_RUN": PIPELINE_RUN, "COMPONENT": COMPONENT,
        "PREV_RELEASE": "", "PREV_SNAPSHOT": "", "BASELINE_REF": BASELINE_REF,
        "CANDIDATE_REF": CANDIDATE_REF, "SCAN_MODE": scan_mode,
        "ROX_ENDPOINT": "https://acs.example.test",
    }
    completed = subprocess.run([sys.executable, str(script_file)], cwd=tmp_path,
                               capture_output=True, text=True, env=environment,
                               check=False, timeout=60)
    result_file = tmp_path / "fresh-scan-result.json"
    result = json.loads(result_file.read_text()) if result_file.exists() else None
    return completed, result


def test_pipeline_defaults_to_fresh_and_passes_scan_mode_to_the_scan_task():
    pipeline = _load(PIPELINE)
    params = {param["name"]: param for param in pipeline["spec"]["params"]}
    assert params["scan-mode"]["default"] == "fresh"
    tasks = {task["name"]: task for task in pipeline["spec"]["tasks"]}
    scan_params = {param["name"]: param["value"] for param in tasks["scan"]["params"]}
    assert scan_params["scan-mode"] == "$(params.scan-mode)"


def test_pipeline_annotation_requires_fresh_mode_and_a_snapshot():
    pipeline = _load(PIPELINE)
    annotate = next(task for task in pipeline["spec"]["tasks"] if task["name"] == "annotate")
    assert {"input": "$(params.scan-mode)", "operator": "in", "values": ["fresh"]} \
        in annotate["when"]
    assert {"input": "$(params.snapshot)", "operator": "notin", "values": [""]} \
        in annotate["when"]


def test_pipeline_scan_task_allows_two_hour_catalog_scan_budget():
    pipeline = _load(PIPELINE)
    scan = next(task for task in pipeline["spec"]["tasks"] if task["name"] == "scan")
    assert scan["timeout"] == "2h"
    example = _load(EXAMPLE_RUN)
    assert example["spec"]["timeouts"]["pipeline"] == "2h20m"
    assert "scan-mode" not in {param["name"] for param in example["spec"]["params"]}


def test_task_defaults_to_fresh_and_exposes_scan_mode_to_scan_and_compare():
    task = _load(TASK)
    params = {param["name"]: param for param in task["spec"]["params"]}
    assert params["scan-mode"]["default"] == "fresh"
    for step_name in ("scan", "compare"):
        env = {item["name"]: item for item in _step(task, step_name)["env"]}
        assert env["SCAN_MODE"]["value"] == "$(params.scan-mode)"


def test_task_uses_catalog_v02_client_for_exchange_and_scan():
    task = _load(TASK)
    for name in ("exchange-service-account-token", "scan"):
        assert _step(task, name)["image"] == ROXCTL_IMAGE


def test_fresh_scan_keeps_the_forced_scan_command(tmp_path):
    completed, calls = _run_scan(tmp_path, "fresh", ROXCTL_HELP_FORCED)

    assert completed.returncode == 0, completed.stderr
    assert _scan_calls(calls) == [
        ["image", "scan", "--timeout=30m", "--force", "--include-snoozed",
         "--format=json", f"--image={BASELINE_REF}"],
        ["image", "scan", "--timeout=30m", "--force", "--include-snoozed",
         "--format=json", f"--image={CANDIDATE_REF}"],
    ]


def test_build_scan_uses_twenty_minute_timeout_and_no_forced_rescan(tmp_path):
    completed, calls = _run_scan(tmp_path, "build", ROXCTL_HELP_CLIENT_DEFAULTS)

    assert completed.returncode == 0, completed.stderr
    assert _scan_calls(calls) == [
        ["image", "scan", "--timeout=30m", "--format=json", f"--image={BASELINE_REF}"],
        ["image", "scan", "--timeout=30m", "--format=json", f"--image={CANDIDATE_REF}"],
    ]
    # No forced-scan help probe and no weakened TLS verification in build mode.
    assert [call for call in calls if "--help" in call] == []
    assert not any("--insecure-skip-tls-verify" in arg for call in calls for arg in call)


@pytest.mark.parametrize("scan_mode", ["", "FRESH", "table", "rescan"])
def test_unsupported_scan_mode_fails_before_any_scan(tmp_path, scan_mode):
    completed, calls = _run_scan(tmp_path, scan_mode, ROXCTL_HELP_FORCED)

    assert completed.returncode != 0
    assert "Unsupported scan mode" in completed.stderr
    assert _scan_calls(calls) == []


def test_fresh_mode_records_forced_provenance_for_an_in_window_scan(tmp_path):
    completed, result = _run_compare(tmp_path, "fresh", "2026-10-27T10:02:30Z")

    assert completed.returncode == 0, completed.stderr
    assert result["scan_mode"] == "fresh"
    assert result["force_rescan"] is True
    assert result["include_snoozed"] is True
    assert result["evidence"] == FRESH_EVIDENCE
    assert result["request_timeout"] == "30m"
    assert result["scanner_image"] == ROXCTL_IMAGE
    assert result["fixed_in_candidate"] == ["CVE-2025-1111"]
    assert result["new_in_candidate"] == ["CVE-2026-2222"]


def test_fresh_mode_still_rejects_an_out_of_window_scan(tmp_path):
    completed, result = _run_compare(tmp_path, "fresh", "2026-10-20T08:00:00Z")

    assert completed.returncode != 0
    assert "out-of-window timestamp" in completed.stderr
    assert result is None


def test_build_mode_accepts_a_cached_scan_and_labels_it_non_fresh(tmp_path):
    completed, result = _run_compare(tmp_path, "build", "2026-10-20T08:00:00Z")

    assert completed.returncode == 0, completed.stderr
    assert result["scan_mode"] == "build"
    assert result["force_rescan"] is False
    assert result["include_snoozed"] is False
    assert result["request_timeout"] == "30m"
    assert result["scanner_image"] == ROXCTL_IMAGE
    assert "may serve cached scans" in result["evidence"]
    assert "not fresh-scan evidence" in result["evidence"]
    assert result["fixed_in_candidate"] == ["CVE-2025-1111"]


@pytest.mark.parametrize("scan_time, message",
                         [("2026-10-20T08:00:00", "lacks timezone"),
                          (None, "Expected scan timestamp")])
def test_build_mode_still_requires_a_well_formed_scan_timestamp(tmp_path, scan_time, message):
    completed, result = _run_compare(tmp_path, "build", scan_time)

    assert completed.returncode != 0
    assert message in completed.stderr
    assert result is None


def test_compare_rejects_an_unknown_scan_mode(tmp_path):
    completed, result = _run_compare(tmp_path, "table", "2026-10-27T10:02:30Z")

    assert completed.returncode != 0
    assert "Unsupported scan mode" in completed.stderr
    assert result is None


@pytest.mark.parametrize("provenance, message", [
    ({"scan_mode": "build", "force_rescan": False, "include_snoozed": False,
      "evidence": "build-parity scans"},
     "Expected forced scans including snoozed findings"),
    # The mode label must agree with the forced-scan flags, not just with them.
    ({"scan_mode": "build", "force_rescan": True, "include_snoozed": True,
      "evidence": "forged flags"},
     "Expected fresh scan provenance"),
    ({"force_rescan": True, "include_snoozed": True, "evidence": "unlabeled"},
     "Expected fresh scan provenance"),
])
def test_annotation_task_rejects_non_fresh_provenance_before_patching(
        tmp_path, provenance, message):
    task = _load(ANNOTATE_TASK)
    workdir = tmp_path / "acs-result"
    workdir.mkdir()
    (workdir / "fresh-scan-result.json").write_text(json.dumps({
        "engine": "acs", "component": COMPONENT, "pipeline_run": PIPELINE_RUN,
        "code_revision": CODE_REVISION, "scanned_at": "2026-10-27T10:06:00+00:00",
        **provenance,
    }))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "kubectl-called"
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"""#!{sys.executable}
from pathlib import Path
Path({str(marker)!r}).write_text("called")
raise SystemExit(1)
""")
    kubectl.chmod(0o755)
    script_file = tmp_path / "annotate.py"
    script_file.write_text(_step(task, "annotate")["script"]
                           .replace("/var/workdir/acs-result", str(workdir)))
    environment = os.environ | {
        "SNAPSHOT": "candidate-snap", "CODE_REVISION": CODE_REVISION,
        "PIPELINE_RUN": PIPELINE_RUN, "COMPONENT": COMPONENT,
        "BASELINE_REF": BASELINE_REF, "CANDIDATE_REF": CANDIDATE_REF,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }

    completed = subprocess.run([sys.executable, str(script_file)], capture_output=True,
                               text=True, env=environment, check=False, timeout=60)

    assert completed.returncode != 0
    assert message in completed.stderr
    assert not marker.exists()
