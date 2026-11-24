from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipelines" / "saw-golden-image-its.yaml"
SELECT_TASK = ROOT / "tasks" / "select-snapshot-image.yaml"
VERIFY_TASK = ROOT / "tasks" / "verify-saw-golden-image.yaml"

LOCAL_REPO = "https://github.com/red-hat-data-services/aipcc-konflux-data"
MAPT_REPO = "https://github.com/redhat-developer/mapt"


def _load(path):
    return yaml.safe_load(path.read_text())


def _pipeline():
    return _load(PIPELINE)


def _tasks():
    return {task["name"]: task for task in _pipeline()["spec"]["tasks"]}


def _finally():
    return {task["name"]: task for task in _pipeline()["spec"]["finally"]}


def _resolver_params(task):
    return {param["name"]: param["value"] for param in task["taskRef"]["params"]}


def _params(task):
    return {param["name"]: param.get("value") for param in task.get("params", [])}


def test_local_resolver_paths_exist():
    for task in _pipeline()["spec"]["tasks"] + _pipeline()["spec"]["finally"]:
        resolver = _resolver_params(task)
        if resolver["url"] == LOCAL_REPO:
            assert (ROOT / resolver["pathInRepo"]).is_file(), resolver["pathInRepo"]


def test_mapt_tasks_pin_a_revision():
    for task in _pipeline()["spec"]["tasks"] + _pipeline()["spec"]["finally"]:
        resolver = _resolver_params(task)
        if resolver["url"] == MAPT_REPO:
            assert resolver["revision"].startswith("v"), resolver


def test_pipeline_provides_every_required_local_task_param():
    specs = {
        "select-image": _load(SELECT_TASK)["spec"],
        "verify-saw-golden-image": _load(VERIFY_TASK)["spec"],
    }
    for name, spec in specs.items():
        passed = _params(_tasks()[name])
        declared = {param["name"]: param for param in spec.get("params", [])}
        for param in declared.values():
            if "default" not in param:
                assert param["name"] in passed, f"{name} misses {param['name']}"
        assert set(passed) <= set(declared), f"{name} passes an unknown param"


def test_create_and_destroy_share_the_mapt_id_and_secret():
    tasks = _tasks()
    finalizers = _finally()
    create = _params(tasks["create-vm"])
    verify = _params(tasks["verify-saw-golden-image"])
    destroy = _params(finalizers["delete-vm"])

    assert create["id"] == destroy["id"] == "$(context.pipelineRun.name)"
    assert create["host-access-secret-name"] == verify["host-access-secret-name"]
    assert create["host-access-secret-name"] == "mapt-$(context.pipelineRun.name)"
    assert create["operation"] == "create"
    assert destroy["operation"] == "destroy"


def test_verify_waits_for_the_host():
    assert _tasks()["verify-saw-golden-image"]["runAfter"] == ["create-vm"]


def test_pipeline_results_are_wired_to_the_verify_task():
    results = {r["name"]: r["value"] for r in _pipeline()["spec"]["results"]}
    declared = {r["name"] for r in _load(VERIFY_TASK)["spec"]["results"]}

    for name in ("TEST_OUTPUT", "GUEST_IP", "ACCELERATION"):
        assert name in declared
        assert results[name] == f"$(tasks.verify-saw-golden-image.results.{name})"
    assert results["CONTAINER_IMAGE"] == "$(tasks.select-image.results.container-image)"


def test_verify_task_maps_the_architecture_for_skopeo():
    script = _load(VERIFY_TASK)["spec"]["steps"][1]["script"]
    assert "x86_64|amd64) SKOPEO_ARCH=\"amd64\"" in script
    assert "aarch64|arm64) SKOPEO_ARCH=\"arm64\"" in script
    assert '--override-arch "${SKOPEO_ARCH}"' in script


def test_report_step_is_fail_closed():
    script = _load(VERIFY_TASK)["spec"]["steps"][2]["script"]

    # A run without a check line must not report success.
    assert "if passed + failed == 0:" in script
    for field in ("result", "timestamp", "successes", "failures", "warnings", "note"):
        assert f'"{field}"' in script
    assert '"SUCCESS" if failed == 0 else "FAILURE"' in script
    assert 'result = "WARNING"' in script


def test_guest_memory_is_converted_to_mib():
    script = _load(VERIFY_TASK)["spec"]["steps"][1]["script"]

    # virt-install reads a bare --memory value as MiB, so 8 would give an 8 MiB guest.
    assert "guest_memory_mib=$((GUEST_MEMORY * 1024))" in script
    assert '--memory "${guest_memory_mib}"' in script
    assert '--memory "${GUEST_MEMORY}"' not in script


def test_console_output_goes_to_a_serial_file():
    script = _load(VERIFY_TASK)["spec"]["steps"][1]["script"]

    # virt-install has no --console log,file= form. The serial backend does it.
    assert "--serial file,path=/var/log/saw-console.log" in script
    assert "log,file=" not in script


def test_layer_selection_ignores_directories():
    script = _load(VERIFY_TASK)["spec"]["steps"][1]["script"]

    # The OCI layout directory sums every file, so a du sort can pick the directory.
    assert "find /var/saw-test/oci -type f -printf '%s %p\\n'" in script
    assert "du -aBM /var/saw-test/oci" not in script


def test_report_step_survives_a_missing_acceleration_file():
    script = _load(VERIFY_TASK)["spec"]["steps"][2]["script"]

    assert "acceleration = \"unknown\"" in script
    assert "except OSError:" in script


def test_checks_cover_the_golden_image_contract():
    script = _load(VERIFY_TASK)["spec"]["steps"][1]["script"]
    for check in (
        "os-release",
        "system-state",
        "cloud-init",
        "guest-agent",
        "openshell-rpms",
        "saw-install-disabled",
        "saw-apply-disabled",
        "trust-root",
        "registry-template",
        "podman",
        "python3",
        "pip",
        "bootc-deployment",
        "root-size",
        "no-docker",
    ):
        assert f"rcheck {check} " in script, check


def test_checks_the_artifact_label():
    script = _load(VERIFY_TASK)["spec"]["steps"][1]["script"]
    assert "io.openshell.saw.bootc-version" in script
    assert 'CHECK|artifact-label|PASS|' in script
    assert 'CHECK|artifact-label|FAIL|' in script
