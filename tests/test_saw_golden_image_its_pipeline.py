from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipelines" / "saw-golden-image-its.yaml"
CONFIG_TASK = ROOT / "tasks" / "config-from-snapshot.yaml"
VERIFY_TASK = ROOT / "tasks" / "verify-saw-disk-image.yaml"

LOCAL_REPO = "https://github.com/red-hat-data-services/aipcc-konflux-data"
MAPT_REPO = "https://github.com/redhat-developer/mapt"

# The cluster must come from the mapt SNC task, at a revision that provides it.
MAPT_SNC_PATH = "tkn/infra-aws-ocp-snc.yaml"
MAPT_SNC_REVISION = "v0.14.7"


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


def _step(name):
    for step in _load(VERIFY_TASK)["spec"]["steps"]:
        if step["name"] == name:
            return step
    raise AssertionError(f"no step {name}")


def test_local_resolver_paths_exist():
    for task in _pipeline()["spec"]["tasks"] + _pipeline()["spec"]["finally"]:
        resolver = _resolver_params(task)
        if resolver["url"] == LOCAL_REPO:
            assert (ROOT / resolver["pathInRepo"]).is_file(), resolver["pathInRepo"]


def test_the_cluster_comes_from_the_mapt_snc_task():
    for task in _pipeline()["spec"]["tasks"] + _pipeline()["spec"]["finally"]:
        resolver = _resolver_params(task)
        if resolver["url"] != MAPT_REPO:
            continue
        assert resolver["pathInRepo"] == MAPT_SNC_PATH, resolver
        assert resolver["revision"] == MAPT_SNC_REVISION, resolver


def test_the_profile_installs_virtualization():
    # The virtualization profile installs OpenShift Virtualization and makes
    # mapt select a bare metal instance, so a guest can run on the cluster.
    create = _params(_tasks()["create-snc-cluster"])
    assert create["profile"] == "$(tasks.create-configuration.results.snc-profile)"

    script = _load(CONFIG_TASK)["spec"]["steps"][0]["script"]
    assert 'snc_profile="virtualization"' in script
    assert "*-disk-image*)" in script


def test_pipeline_provides_every_required_local_task_param():
    specs = {
        "create-configuration": _load(CONFIG_TASK)["spec"],
        "verify-saw-disk-image": _load(VERIFY_TASK)["spec"],
    }
    for name, spec in specs.items():
        passed = _params(_tasks()[name])
        declared = {param["name"]: param for param in spec.get("params", [])}
        for param in declared.values():
            if "default" not in param:
                assert param["name"] in passed, f"{name} misses {param['name']}"
        assert set(passed) <= set(declared), f"{name} passes an unknown param"


def test_pipeline_provides_every_param_the_mapt_task_requires():
    # The remote task is not in this repository, so guard the names the pipeline
    # depends on: mapt marks id, operation and the credential secrets as required.
    create = _params(_tasks()["create-snc-cluster"])
    destroy = _params(_finally()["delete-snc-cluster"])
    for param in ("id", "operation", "secret-aws-credentials", "secret-ocp-snc-credentials"):
        assert param in create, param
        assert param in destroy, param


def test_create_and_destroy_share_the_mapt_id_and_secret():
    create = _params(_tasks()["create-snc-cluster"])
    destroy = _params(_finally()["delete-snc-cluster"])

    assert create["id"] == destroy["id"] == "$(context.pipelineRun.name)"
    assert (
        create["secret-ocp-snc-credentials"]
        == destroy["secret-ocp-snc-credentials"]
        == "$(params.ocp-snc-credentials-secret)"
    )
    assert create["secret-aws-credentials"] == destroy["secret-aws-credentials"]
    assert create["operation"] == "create"
    assert destroy["operation"] == "destroy"


def test_destroy_always_runs():
    # A failed run must not leave a bare metal instance on the account.
    finalizers = _finally()
    assert "delete-snc-cluster" in finalizers
    assert _params(finalizers["delete-snc-cluster"])["force-destroy"] == "true"


def test_verify_waits_for_the_cluster():
    verify = _tasks()["verify-saw-disk-image"]
    assert verify["runAfter"] == ["create-snc-cluster"]
    assert _params(verify)["cluster-access-secret-name"] == (
        "$(tasks.create-snc-cluster.results.cluster-access-secret)"
    )


def test_pipeline_results_are_wired_to_the_verify_task():
    results = {r["name"]: r["value"] for r in _pipeline()["spec"]["results"]}
    declared = {r["name"] for r in _load(VERIFY_TASK)["spec"]["results"]}

    assert "TEST_OUTPUT" in declared
    assert results["TEST_OUTPUT"] == "$(tasks.verify-saw-disk-image.results.TEST_OUTPUT)"
    assert results["container-image"] == "$(tasks.create-configuration.results.container-image)"


def test_the_guest_starts_from_the_artifact():
    # The production path: CDI imports the OCI artifact, KubeVirt boots it.
    script = _step("deploy")["script"]
    assert "kind: VirtualMachine" in script
    assert "dataVolumeTemplates:" in script
    assert "url: docker://${CONTAINER_IMAGE}" in script
    assert "secretRef: saw-pull-secret" in script
    assert "contentType: kubevirt" in script


def test_the_pull_secret_comes_from_the_pod_environment():
    # The deploy step switches KUBECONFIG to the SNC cluster. A kubectl read of
    # the tenant secret would then reach the wrong cluster.
    deploy = _step("deploy")
    env = {item["name"]: item for item in deploy["env"]}
    ref = env["PULL_SECRET_JSON"]["valueFrom"]["secretKeyRef"]
    assert ref["name"] == "$(params.pull-secret-name)"
    assert ref["key"] == ".dockerconfigjson"
    assert 'kubectl get secret "${PULL_SECRET_NAME}"' not in deploy["script"]


def test_the_guest_is_reachable_through_the_api_server():
    # The task pod lives outside the SNC cluster, so SSH goes through a tunnel.
    script = _step("verify")["script"]
    assert "kubectl port-forward" in script
    assert "svc/saw-golden-ssh 2222:22" in script
    assert "127.0.0.1" in script


def test_the_guest_waits_for_the_cdi_import():
    script = _step("deploy")["script"]
    assert 'Failed) echo "ERROR: the CDI import failed"' in script
    assert 'if [ "${phase:-}" != "Succeeded" ]' in script


def test_verify_task_maps_the_architecture_of_the_node():
    # The artifact is a multi-arch list. skopeo must read the node manifest,
    # else it inspects the list and finds no labels.
    script = _step("verify")["script"]
    assert "nodeInfo.architecture" in script
    assert '--override-arch "${arch}"' in script


def test_report_step_is_fail_closed():
    script = _step("report")["script"]

    # A run without a check line must not report success.
    assert "if passed + failed == 0:" in script
    for field in ("result", "timestamp", "successes", "failures", "warnings", "note"):
        assert f'"{field}"' in script
    assert '"SUCCESS" if failed == 0 else "FAILURE"' in script
    assert 'result = "WARNING"' in script


def test_the_report_step_reads_the_step_log():
    # Tekton steps do not share stdout, so the checks travel through a file.
    deploy = _step("deploy")
    verify = _step("verify")
    report = _step("report")

    for step in (deploy, verify, report):
        mounts = {m["name"]: m["mountPath"] for m in step.get("volumeMounts", [])}
        assert mounts.get("workspace") == "/workspace", step["name"]
    assert "LOG=/workspace/verify.log" in verify["script"]
    assert 'os.environ["VERIFY_LOG"]' in report["script"]
    # Every check line goes through log(), which also appends to the file.
    assert verify["script"].count('log "CHECK|') >= 3
    # A plain wait would block on the tunnel supervisor until the step timeout.
    assert "\n        wait\n" not in verify["script"]


def test_the_report_step_survives_a_missing_log():
    script = _step("report")["script"]
    assert "except OSError:" in script
    assert 'guest_ip = "unknown"' in script


def test_the_guest_memory_is_a_kubernetes_quantity():
    # KubeVirt reads the guest memory from the domain, as a quantity.
    deploy = _step("deploy")["script"]
    verify_params = {p["name"]: p for p in _load(VERIFY_TASK)["spec"]["params"]}
    assert verify_params["guest-memory"]["default"] == "8Gi"
    assert "memory: ${GUEST_MEMORY}" in deploy


def test_the_disk_size_covers_the_raw_conversion():
    # CDI converts the 20 GiB qcow2 to raw, so the PVC must be larger.
    verify_params = {p["name"]: p for p in _load(VERIFY_TASK)["spec"]["params"]}
    assert verify_params["guest-disk-size"]["default"] == "25Gi"
    assert "storage: ${GUEST_DISK_SIZE}" in _step("deploy")["script"]


def test_checks_cover_the_golden_image_contract():
    script = _step("verify")["script"]
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
    script = _step("verify")["script"]
    assert "io.openshell.saw.bootc-version" in script
    assert "CHECK|artifact-label|PASS|" in script
    assert "CHECK|artifact-label|FAIL|" in script
