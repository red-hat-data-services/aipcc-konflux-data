import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipelines" / "saw-golden-image-its.yaml"
SECRET_DOC = ROOT / "pipelines" / "saw-golden-image-its.md"
CONFIG_TASK = ROOT / "tasks" / "config-from-snapshot.yaml"
VERIFY_TASK = ROOT / "tasks" / "verify-saw-disk-image.yaml"

LOCAL_REPO = "https://github.com/red-hat-data-services/aipcc-konflux-data"
MAPT_REPO = "https://github.com/redhat-developer/mapt"

# The cluster must come from the mapt SNC task, at a revision that provides it.
# No release tag holds the IBM Cloud task or the bare metal selection that
# nested virtualization needs, so the pipeline reads main until mapt publishes
# a tag with both.
MAPT_SNC_PATH = "tkn/infra-ibmcloud-ocp-snc.yaml"
MAPT_SNC_REVISION = "main"

# Params of the IBM Cloud task. Tekton rejects a param the task does not
# declare, so the pipeline must stay inside this set.
MAPT_IBMCLOUD_PARAMS = {
    "cluster-access-secret-name",
    "catalog-source",
    "compute-families",
    "compute-sizes",
    "cpus",
    "debug",
    "disable-cluster-readiness",
    "disk-size",
    "force-destroy",
    "gpu-manufacturer",
    "gpus",
    "id",
    "keep-state",
    "memory",
    "nested-virt",
    "operation",
    "operator-channel",
    "ownerKind",
    "ownerName",
    "ownerUid",
    "profile",
    "secret-ibmcloud-credentials",
    "secret-ocp-snc-credentials",
    "spot",
    "tags",
    "version",
    "vpc-id",
    "zone",
}


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
    # Create and destroy must read the same revision, else the destroy can run
    # a task that no longer matches the state the create wrote.
    seen = set()
    for task in _pipeline()["spec"]["tasks"] + _pipeline()["spec"]["finally"]:
        resolver = _resolver_params(task)
        if resolver["url"] != MAPT_REPO:
            continue
        assert resolver["pathInRepo"] == MAPT_SNC_PATH, resolver
        seen.add(resolver["revision"])
    assert seen == {MAPT_SNC_REVISION}


def test_the_profile_installs_virtualization():
    # The virtualization profile installs OpenShift Virtualization, so a guest
    # can run on the cluster.
    create = _params(_tasks()["create-snc-cluster"])
    assert create["profile"] == "$(tasks.create-configuration.results.snc-profile)"

    script = _load(CONFIG_TASK)["spec"]["steps"][0]["script"]
    assert 'snc_profile="virtualization"' in script
    assert 'cloud_provider="ibm"' in script
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
    for param in ("id", "operation", "secret-ibmcloud-credentials", "secret-ocp-snc-credentials"):
        assert param in create, param
        assert param in destroy, param


def test_the_pipeline_only_passes_declared_ibmcloud_params():
    # The IBM Cloud task replaced the AWS one. A leftover AWS param, for example
    # arch or spot-eviction-tolerance, would make the task refuse to start.
    for name in ("create-snc-cluster", "delete-snc-cluster"):
        task = (_tasks() if name in _tasks() else _finally())[name]
        unknown = set(_params(task)) - MAPT_IBMCLOUD_PARAMS
        assert not unknown, (name, unknown)


def test_the_create_never_asks_for_spot():
    # A request that needs nested virtualization makes mapt select a bare metal
    # server, and mapt takes no spot capacity for one. The pipeline therefore
    # offers no spot knob, so the task default "false" stands.
    params = {p["name"] for p in _pipeline()["spec"]["params"]}
    assert "spot" not in params
    assert "spot" not in _params(_tasks()["create-snc-cluster"])


def test_the_pipeline_runs_x86_64_only():
    # The IBM Cloud SNC task sets the architecture itself and IBM Cloud VPC
    # offers no arm64 profile, so a run covers x86_64 variants only. A param
    # would promise a coverage that the task cannot give.
    params = {p["name"] for p in _pipeline()["spec"]["params"]}
    assert "architecture" not in params
    create = {p["name"]: p for p in _tasks()["create-configuration"]["params"]}
    assert create["architecture"]["value"] == "x86_64"


def test_the_catalog_image_version_has_a_default():
    # The catalog name follows the version, so the run must name a version
    # unless the operator asks for the latest of the private catalog.
    params = {p["name"]: p for p in _pipeline()["spec"]["params"]}
    default = params["ocp-version"]["default"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", default), default
    assert "latest" in params["ocp-version"]["description"]
    assert _params(_tasks()["create-snc-cluster"])["version"] == "$(params.ocp-version)"


def test_the_pipeline_expects_a_bare_metal_server():
    # The virtualization profile requests nested virtualization, so the
    # compute resource is a bare metal server. A reader of the description must
    # know that, and must not read the old claim that a virtual server instance
    # hosts the cluster.
    description = _pipeline()["spec"]["description"]
    assert "A bare metal server costs more than a virtual server instance" in description
    assert "does not support nested virtualization" not in description


def test_the_cpu_and_memory_values_steer_the_bare_metal_selection():
    # mapt reads cpus and memory as floors of the bare metal profile, so the
    # two params decide the cost of the run. Their text must say so.
    params = {p["name"]: p for p in _pipeline()["spec"]["params"]}
    assert "floor" in params["snc-cpus"]["description"]
    assert "floor" in params["snc-memory"]["description"]
    create = _params(_tasks()["create-snc-cluster"])
    assert create["cpus"] == "$(params.snc-cpus)"
    assert create["memory"] == "$(params.snc-memory)"


def test_the_creation_sets_the_ibmcloud_zone():
    # mapt reads IC_ZONE from the environment and stops without it, so the
    # create operation needs the zone. The destroy operation reads the state
    # from Object Storage and needs no zone.
    create = _params(_tasks()["create-snc-cluster"])
    assert create["zone"] == "$(params.ibmcloud-zone)"
    assert "zone" not in _params(_finally()["delete-snc-cluster"])


def test_create_and_destroy_share_the_mapt_id_and_secret():
    create = _params(_tasks()["create-snc-cluster"])
    destroy = _params(_finally()["delete-snc-cluster"])

    assert create["id"] == destroy["id"] == "$(context.pipelineRun.name)"
    assert (
        create["secret-ocp-snc-credentials"]
        == destroy["secret-ocp-snc-credentials"]
        == "$(params.ocp-snc-credentials-secret)"
    )
    assert create["secret-ibmcloud-credentials"] == destroy["secret-ibmcloud-credentials"]
    assert create["operation"] == "create"
    assert destroy["operation"] == "destroy"


def test_destroy_always_runs():
    # A failed run must not leave a virtual server instance on the account.
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


def test_the_node_must_support_hardware_virtualization():
    # A node without /dev/kvm accepts the VirtualMachine and then never boots it.
    # The kvm device plugin resource is the evidence of the device. Fail with the
    # cause, before the boot wait.
    script = _step("deploy")["script"]
    assert 'devices.kubevirt.io/kvm' in script
    assert "no node exposes the devices.kubevirt.io/kvm resource" in script
    assert script.index('devices.kubevirt.io/kvm') < script.index("Creating the VirtualMachine")


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


def test_a_missing_tool_does_not_look_like_a_bad_artifact():
    # skopeo and jq come from the step image. When either is absent, the label
    # stays unread and the failure must name the tool, not the artifact.
    script = _step("verify")["script"]
    assert "command -v skopeo" in script
    assert "command -v jq" in script
    assert "the step image has no skopeo or no jq" in script
    assert script.index("command -v skopeo") < script.index("label=$(skopeo")


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


def test_the_task_outlives_its_own_waits():
    # A bare timeout hides which wait failed.
    verify_params = {p["name"]: p for p in _load(VERIFY_TASK)["spec"]["params"]}
    waits = sum(int(verify_params[name]["default"]) for name in
                ("cnv-timeout", "dv-timeout", "boot-timeout"))
    timeout = _tasks()["verify-saw-disk-image"]["timeout"]
    duration = re.fullmatch(r"(\d+)h(\d+)m\d+s", timeout)
    assert duration, timeout
    seconds = int(duration.group(1)) * 3600 + int(duration.group(2)) * 60
    assert seconds > waits, timeout


def test_the_run_has_a_timeout_budget():
    # The cluster Tekton rejects spec.timeouts on a Pipeline, so the budget
    # must live on the tasks. The cluster creation alone takes about 45 minutes.
    spec = _pipeline()["spec"]
    assert "timeouts" not in spec
    tasks = _tasks()
    assert tasks["create-snc-cluster"]["timeout"] == "2h0m0s"
    assert tasks["verify-saw-disk-image"]["timeout"] == "2h0m0s"
    # Destroy retries against the bucket, so it keeps the same budget.
    finally_tasks = {t["name"]: t for t in spec["finally"]}
    assert finally_tasks["delete-snc-cluster"]["timeout"] == "2h0m0s"


def test_the_secret_doc_names_the_secrets_the_pipeline_reads():
    # A tenant operator reads the doc, not the YAML. Keep the two in step.
    doc = SECRET_DOC.read_text()
    pipeline_params = {p["name"]: p for p in _pipeline()["spec"]["params"]}
    verify_params = {p["name"]: p for p in _load(VERIFY_TASK)["spec"]["params"]}
    names = [
        pipeline_params["ibmcloud-credentials-secret"].get("default"),
        pipeline_params["ocp-snc-credentials-secret"].get("default"),
        verify_params["pull-secret-name"].get("default"),
    ]
    for name in names:
        assert name, "a secret param has no default name"
        assert name in doc, f"{name} is not in {SECRET_DOC.name}"


def test_the_secret_doc_lists_the_keys_of_each_secret():
    doc = SECRET_DOC.read_text()
    for key in ("api-key", "region", "cos-access-key", "cos-secret-key",
                "bucket", "pull-secret", ".dockerconfigjson"):
        assert f"`{key}`" in doc, key


def test_the_secret_doc_does_not_name_a_secret_the_pipeline_never_reads():
    doc = SECRET_DOC.read_text()
    required = doc.split("## Required secrets", 1)[1].split("\n## ", 1)[0]
    body = PIPELINE.read_text() + VERIFY_TASK.read_text()
    names = re.findall(r"`([a-z0-9-]*(?:konflux-tests|namespace-pull))`", required)
    assert names, "the required table names no secret"
    for name in names:
        assert name in body, name


def test_every_step_binds_the_variables_it_uses():
    # set -u stops the step on an unbound variable. BOOT_TIMEOUT was missing.
    for step in _load(VERIFY_TASK)["spec"]["steps"]:
        script = step["script"]
        bound = {item["name"] for item in step.get("env", [])}
        for name in ("BOOT_TIMEOUT", "DV_TIMEOUT", "CNV_TIMEOUT", "GUEST_CPUS",
                     "GUEST_MEMORY", "GUEST_DISK_SIZE", "NAMESPACE"):
            if "${%s" % name in script or "$%s" % name in script.replace("${", "\0"):
                assert name in bound, (step["name"], name)


def test_a_failed_step_still_reports():
    # Tekton skips the remaining steps after a failure, so the report step
    # would never write TEST_OUTPUT.
    steps = {step["name"]: step for step in _load(VERIFY_TASK)["spec"]["steps"]}
    assert steps["deploy"]["onError"] == "continue"
    assert steps["verify"]["onError"] == "continue"
    assert steps["report"].get("onError") is None


def test_the_verify_step_skips_a_guest_that_does_not_exist():
    script = _step("verify")["script"]
    assert "if [ ! -f /workspace/deployed ]" in script
    assert _step("deploy")["script"].count("touch /workspace/deployed") == 1


def test_the_registry_credentials_are_never_traced():
    script = _step("deploy")["script"]
    write = script.index('printf \'%s\' "${PULL_SECRET_JSON}"')
    assert "{ set +x; } 2>/dev/null" in script[:write]
    # Tracing must stay off, so no re-enable follows the write.
    assert "set -x" not in script[write:]


def test_the_masquerade_interface_forwards_the_ssh_port():
    # Masquerade needs a declared port, else nothing on the pod reaches sshd.
    vm = _step("deploy")["script"]
    assert "masquerade: {}" in vm
    assert "ports:" in vm
    assert "port: 22" in vm
    assert "protocol: TCP" in vm


def test_an_interrupted_check_list_is_a_failure():
    script = _step("report")["script"]
    assert 'raw.startswith("CHECK-END|")' in script
    assert 'names.append("incomplete")' in script
    assert 'log "CHECK-END|all|PASS|the check list finished"' in _step("verify")["script"]


def test_a_guest_call_never_reads_the_step_stdin():
    assert 'GUEST_SSH="ssh -n -i' in _step("verify")["script"]


def test_the_report_step_survives_a_missing_log():
    script = _step("report")["script"]
    # The log is absent when the deploy step fails. The step must still write
    # TEST_OUTPUT, so the read is guarded and the run reports one failure.
    assert 'os.environ["VERIFY_LOG"]' in script
    assert "except OSError:" in script
    assert 'guest_ip = "unknown"' in script
    assert "if passed + failed == 0:" in script


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
