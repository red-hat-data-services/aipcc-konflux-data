import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parent.parent
TASK = ROOT / "tasks" / "acs-scan-diff-resolve.yaml"
PIPELINE = ROOT / "pipelines" / "acs-scan-diff.yaml"
CODE_REVISION = "0123456789abcdef0123456789abcdef01234567"
COMPONENT_SELECTOR = "appstudio.openshift.io/component"


def _register_image(registry, name, architectures=("amd64",)):
    """Register a fake OCI image and return its digest-pinned reference."""
    repo = f"quay.io/example/ai-tenant/{name}"
    if len(architectures) == 1:
        raw = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"digest": f"sha256:{hashlib.sha256(name.encode()).hexdigest()}"},
            "layers": [{"digest": f"sha256:{hashlib.sha256((name + '-layer').encode()).hexdigest()}"}],
        }
        ref = f"{repo}@sha256:{hashlib.sha256(json.dumps(raw).encode()).hexdigest()}"
        registry[ref] = {
            "raw": raw,
            "config": {"architecture": architectures[0], "os": "linux",
                       "rootfs": {"type": "layers", "diff_ids": []}},
        }
        return ref
    manifests = []
    for arch in architectures:
        digest = hashlib.sha256(f"{name}-{arch}".encode()).hexdigest()
        manifests.append({"digest": f"sha256:{digest}",
                          "platform": {"architecture": arch, "os": "linux"}})
        registry[f"{repo}@sha256:{digest}"] = {
            "config": {"architecture": arch, "os": "linux",
                       "rootfs": {"type": "layers", "diff_ids": []}}}
    raw = {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
           "manifests": manifests}
    ref = f"{repo}@sha256:{hashlib.sha256(json.dumps(raw).encode()).hexdigest()}"
    registry[ref] = {"raw": raw}
    return ref


def _snapshot(name, application, components, component_label="unrelated-component"):
    """Build a Snapshot fixture whose metadata component label misleads."""
    return {
        "apiVersion": "appstudio.redhat.com/v1beta1",
        "kind": "Snapshot",
        "metadata": {"name": name, "namespace": "ai-tenant",
                     "labels": {COMPONENT_SELECTOR: component_label}},
        "spec": {"application": application,
                 "components": [{"name": component, "containerImage": image}
                                for component, image in components]},
    }


def _child_ref(ref, name, arch):
    """Return the per-architecture child manifest ref of a registered index."""
    return f"{ref.split('@')[0]}@sha256:{hashlib.sha256(f'{name}-{arch}'.encode()).hexdigest()}"


def _run_resolver(tmp_path, env, *, snapshots=(), release=None, releases_by_selector=None, registry=None):
    """Execute the resolver's embedded Python with mocked kubectl/skopeo."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    task = yaml.safe_load(TASK.read_text())
    secret = tmp_path / "components-namespace-pull"
    secret.mkdir()
    (secret / ".dockerconfigjson").write_text("{}")
    results = tmp_path / "results"
    results.mkdir()
    fixtures = {"snapshot": {s["metadata"]["name"]: s for s in snapshots},
                "releases": releases_by_selector or {}}
    if release is not None:
        fixtures["release"] = {release["metadata"]["name"]: release}
    fixtures_file = tmp_path / "fixtures.json"
    fixtures_file.write_text(json.dumps(fixtures))
    registry_file = tmp_path / "registry.json"
    registry_file.write_text(json.dumps(registry or {}))

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(f"""#!{sys.executable}
import json
import sys

fixtures = json.load(open({str(fixtures_file)!r}))
args = sys.argv[1:]
if args[:2] == ["get", "releases"]:
    print(json.dumps({{"items": fixtures["releases"].get(args[3], [])}}))
else:
    kind, name = args[1], args[2]
    print(json.dumps(fixtures[kind][name]))
""")
    kubectl.chmod(0o755)
    skopeo = bin_dir / "skopeo"
    skopeo.write_text(f"""#!{sys.executable}
import json
import sys

registry = json.load(open({str(registry_file)!r}))
args = sys.argv[1:]
ref = args[-1][len("docker://"):]
print(json.dumps(registry[ref]["raw" if "--raw" in args else "config"]))
""")
    skopeo.chmod(0o755)

    script = (task["spec"]["steps"][0]["script"]
              .replace("/mnt/components-namespace-pull/.dockerconfigjson", str(secret / ".dockerconfigjson"))
              .replace("/tmp/acs-registry-auth", str(tmp_path / "acs-registry-auth"))
              .replace("/tekton/results", str(results)))
    script_file = tmp_path / "resolver.py"
    script_file.write_text(script)
    environment = os.environ | {
        "CODE_REVISION": CODE_REVISION,
        "RELEASE": "", "RELEASE_PLAN": "", "SNAPSHOT": "", "PROD_RELEASE_PLAN": "",
        "COMPONENT": "", "BASELINE_REF": "", "CANDIDATE_REF": "", "BASELINE_SNAPSHOT": "",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        **env,
    }
    completed = subprocess.run([sys.executable, str(script_file)], capture_output=True,
                               text=True, env=environment, check=False, timeout=60)
    return completed, {path.name: path.read_text() for path in results.iterdir()}


def _snapshot_pair(registry, baseline_application="my-application",
                   candidate_application="my-application", baseline_component_count=1):
    baseline_ref = _register_image(registry, "baseline-image", ("amd64", "arm64"))
    candidate_ref = _register_image(registry, "candidate-image", ("amd64", "arm64"))
    baseline_components = [("my-component", baseline_ref)]
    if baseline_component_count == 2:
        baseline_components.append(("my-component", baseline_ref))
    snapshots = [
        _snapshot("baseline-snap", baseline_application, baseline_components),
        _snapshot("candidate-snap", candidate_application, [("my-component", candidate_ref)]),
    ]
    return snapshots, baseline_ref, candidate_ref


def _snapshot_mode_env():
    return {"COMPONENT": "my-component", "BASELINE_SNAPSHOT": "baseline-snap",
            "SNAPSHOT": "candidate-snap"}


def test_snapshot_pair_mode_selects_exact_component_from_grouped_snapshots(tmp_path):
    registry = {}
    snapshots, baseline_ref, candidate_ref = _snapshot_pair(registry)
    # Grouped snapshots: other components appear alongside the requested one.
    other_baseline = _register_image(registry, "baseline-other")
    other_candidate = _register_image(registry, "candidate-other")
    snapshots[0]["spec"]["components"].insert(
        0, {"name": "grouped-other-component", "containerImage": other_baseline})
    snapshots[1]["spec"]["components"].append(
        {"name": "candidate-other-component", "containerImage": other_candidate})

    completed, results = _run_resolver(tmp_path, _snapshot_mode_env(),
                                       snapshots=snapshots, registry=registry)

    assert completed.returncode == 0, completed.stderr
    assert results["component"] == "my-component"
    assert results["baseline-image"] == baseline_ref
    assert results["candidate-image"] == candidate_ref
    assert results["prev-snapshot"] == "baseline-snap"
    assert results["baseline-release"] == ""
    assert json.loads(results["scan-baseline-refs"]) == [
        {"platform": "linux/amd64", "ref": _child_ref(baseline_ref, "baseline-image", "amd64")},
        {"platform": "linux/arm64", "ref": _child_ref(baseline_ref, "baseline-image", "arm64")},
    ]
    assert [entry["platform"] for entry in json.loads(results["scan-candidate-refs"])] == [
        "linux/amd64", "linux/arm64"]


def test_snapshot_pair_mode_rejects_missing_and_duplicate_component(tmp_path):
    for baseline_count in (0, 2):
        registry = {}
        if baseline_count:
            snapshots, _, _ = _snapshot_pair(registry, baseline_component_count=baseline_count)
        else:
            candidate_ref = _register_image(registry, "candidate-image")
            snapshots = [
                _snapshot("baseline-snap", "my-application", [("other-component", candidate_ref)]),
                _snapshot("candidate-snap", "my-application", [("my-component", candidate_ref)]),
            ]

        completed, _ = _run_resolver(tmp_path / f"case{baseline_count}", _snapshot_mode_env(),
                                     snapshots=snapshots, registry=registry)

        assert completed.returncode != 0
        assert "Expected exactly one my-component entry" in completed.stderr


@pytest.mark.parametrize(
    "env, message",
    [
        ({"BASELINE_SNAPSHOT": "baseline-snap"},
         "Snapshot mode requires component, baseline-snapshot and snapshot"),
        ({"BASELINE_SNAPSHOT": "baseline-snap", "SNAPSHOT": "candidate-snap"},
         "Snapshot mode requires component, baseline-snapshot and snapshot"),
        ({"BASELINE_SNAPSHOT": "baseline-snap", "SNAPSHOT": "candidate-snap",
          "COMPONENT": "my-component", "RELEASE": "some-release"},
         "Snapshot mode requires component, baseline-snapshot and snapshot"),
        ({"BASELINE_SNAPSHOT": "baseline-snap", "SNAPSHOT": "candidate-snap",
          "COMPONENT": "my-component", "RELEASE_PLAN": "some-plan"},
         "Snapshot mode requires component, baseline-snapshot and snapshot"),
        ({"BASELINE_SNAPSHOT": "baseline-snap", "SNAPSHOT": "candidate-snap",
          "COMPONENT": "my-component", "PROD_RELEASE_PLAN": "some-prod-plan"},
         "Snapshot mode requires component, baseline-snapshot and snapshot"),
        ({"BASELINE_SNAPSHOT": "baseline-snap", "SNAPSHOT": "candidate-snap",
          "COMPONENT": "my-component", "BASELINE_REF": "quay.io/example/a@sha256:" + "0" * 64},
         "Explicit mode requires both image refs and component"),
        ({"BASELINE_SNAPSHOT": "baseline-snap", "SNAPSHOT": "candidate-snap",
          "COMPONENT": "my-component", "CANDIDATE_REF": "quay.io/example/a@sha256:" + "0" * 64},
         "Explicit mode requires both image refs and component"),
        ({"RELEASE": "some-release", "RELEASE_PLAN": "some-plan", "SNAPSHOT": "candidate-snap",
          "BASELINE_SNAPSHOT": "baseline-snap"},
         "Snapshot mode requires component, baseline-snapshot and snapshot"),
    ],
)
def test_snapshot_pair_mode_rejects_partial_and_mixed_inputs(tmp_path, env, message):
    completed, _ = _run_resolver(tmp_path, env)

    assert completed.returncode != 0
    assert message in completed.stderr


def test_snapshot_pair_mode_rejects_identical_snapshots(tmp_path):
    env = {"COMPONENT": "my-component", "BASELINE_SNAPSHOT": "same-snap",
           "SNAPSHOT": "same-snap"}

    completed, _ = _run_resolver(tmp_path, env)

    assert completed.returncode != 0
    assert "identical" in completed.stderr


def test_snapshot_pair_mode_rejects_application_mismatch(tmp_path):
    registry = {}
    snapshots, _, _ = _snapshot_pair(registry, baseline_application="app-one",
                                     candidate_application="app-two")

    completed, _ = _run_resolver(tmp_path, _snapshot_mode_env(),
                                 snapshots=snapshots, registry=registry)

    assert completed.returncode != 0
    assert "different applications" in completed.stderr


def test_snapshot_pair_mode_rejects_platform_mismatch(tmp_path):
    registry = {}
    baseline_ref = _register_image(registry, "baseline-image", ("amd64",))
    candidate_ref = _register_image(registry, "candidate-image", ("arm64",))
    snapshots = [
        _snapshot("baseline-snap", "my-application", [("my-component", baseline_ref)]),
        _snapshot("candidate-snap", "my-application", [("my-component", candidate_ref)]),
    ]

    completed, _ = _run_resolver(tmp_path, _snapshot_mode_env(),
                                 snapshots=snapshots, registry=registry)

    assert completed.returncode != 0
    assert "platform coverage differ" in completed.stderr


@pytest.mark.parametrize("application", [None, "", {"application": "my-application"}])
def test_snapshot_pair_mode_rejects_missing_or_malformed_application(tmp_path, application):
    registry = {}
    snapshots, _, _ = _snapshot_pair(registry)
    snapshots[0]["spec"]["application"] = application

    completed, _ = _run_resolver(tmp_path, _snapshot_mode_env(),
                                 snapshots=snapshots, registry=registry)

    assert completed.returncode != 0
    assert "Snapshot has no application" in completed.stderr


def test_explicit_ref_mode_still_succeeds(tmp_path):
    registry = {}
    baseline_ref = _register_image(registry, "baseline-image")
    candidate_ref = _register_image(registry, "candidate-image")
    snapshots = [_snapshot("candidate-snap", "my-application",
                           [("my-component", candidate_ref)])]

    completed, results = _run_resolver(
        tmp_path,
        {"COMPONENT": "my-component", "BASELINE_REF": baseline_ref,
         "CANDIDATE_REF": candidate_ref, "SNAPSHOT": "candidate-snap"},
        snapshots=snapshots, registry=registry)

    assert completed.returncode == 0, completed.stderr
    assert results["baseline-image"] == baseline_ref
    assert results["candidate-image"] == candidate_ref
    assert results["prev-snapshot"] == ""
    assert results["baseline-release"] == ""


def test_release_mode_automatic_lookup_still_succeeds(tmp_path):
    registry = {}
    baseline_ref = _register_image(registry, "baseline-image")
    candidate_ref = _register_image(registry, "candidate-image")
    snapshots = [
        _snapshot("baseline-snap", "my-application", [("my-component", baseline_ref)]),
        _snapshot("candidate-snap", "my-application", [("my-component", candidate_ref)]),
    ]
    release = {
        "apiVersion": "appstudio.redhat.com/v1beta1",
        "kind": "Release",
        "metadata": {"name": "current-release", "namespace": "ai-tenant",
                     "labels": {COMPONENT_SELECTOR: "my-component"}},
        "spec": {"snapshot": "candidate-snap", "releasePlan": "stage-plan"},
    }
    previous = {
        "apiVersion": "appstudio.redhat.com/v1beta1",
        "kind": "Release",
        "metadata": {"name": "previous-release", "namespace": "ai-tenant",
                     "creationTimestamp": "2024-01-01T00:00:00Z",
                     "labels": {COMPONENT_SELECTOR: "my-component"}},
        "spec": {"snapshot": "baseline-snap", "releasePlan": "prod-plan"},
        "status": {"conditions": [{"type": "Released", "status": "True"}]},
    }
    selector = f"{COMPONENT_SELECTOR}=my-component,release.appstudio.openshift.io/releasePlan=prod-plan"

    completed, results = _run_resolver(
        tmp_path,
        {"RELEASE": "current-release", "RELEASE_PLAN": "stage-plan",
         "SNAPSHOT": "candidate-snap", "PROD_RELEASE_PLAN": "prod-plan"},
        snapshots=snapshots, release=release,
        releases_by_selector={selector: [previous]}, registry=registry)

    assert completed.returncode == 0, completed.stderr
    assert results["component"] == "my-component"
    assert results["baseline-release"] == "previous-release"
    assert results["prev-snapshot"] == "baseline-snap"
    assert results["baseline-image"] == baseline_ref
    assert results["candidate-image"] == candidate_ref


def test_pipeline_and_task_wire_baseline_snapshot_param():
    pipeline = yaml.safe_load(PIPELINE.read_text())
    pipeline_params = {param["name"]: param for param in pipeline["spec"]["params"]}
    assert pipeline_params["baseline-snapshot"]["default"] == ""
    resolve = next(task for task in pipeline["spec"]["tasks"] if task["name"] == "resolve")
    task_params = {param["name"]: param["value"] for param in resolve["params"]}
    assert task_params["baseline-snapshot"] == "$(params.baseline-snapshot)"

    task = yaml.safe_load(TASK.read_text())
    task_params = {param["name"]: param for param in task["spec"]["params"]}
    assert task_params["baseline-snapshot"]["default"] == ""
    env = {item["name"]: item for item in task["spec"]["steps"][0]["env"]}
    assert env["BASELINE_SNAPSHOT"]["value"] == "$(params.baseline-snapshot)"
