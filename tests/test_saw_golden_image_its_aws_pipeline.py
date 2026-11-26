"""Tests of the AWS pipeline of the SAW golden image scenario.

The IBM Cloud pipeline has the same contract and its own module,
test_saw_golden_image_its_pipeline.py.
"""

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "pipelines" / "saw-golden-image-its-aws.yaml"
SECRET_DOC = ROOT / "pipelines" / "saw-golden-image-its-aws.md"
IBMCLOUD_PIPELINE = ROOT / "pipelines" / "saw-golden-image-its-ibmcloud.yaml"
CONFIG_TASK = ROOT / "tasks" / "config-from-snapshot.yaml"
VERIFY_TASK = ROOT / "tasks" / "verify-saw-disk-image.yaml"

LOCAL_REPO = "https://github.com/red-hat-data-services/aipcc-konflux-data"
MAPT_REPO = "https://github.com/redhat-developer/mapt"

MAPT_SNC_PATH = "tkn/infra-aws-ocp-snc.yaml"
# A released tag holds the AWS task with the arch param, the nested
# virtualization flag and the profile list, so no run reads the main branch.
MAPT_SNC_REVISION = "v0.14.7"

# Params of the AWS task. Tekton rejects a param the task does not declare, so
# the pipeline must stay inside this set. An IBM Cloud param, for example zone
# or catalog-source, would make the task refuse to start.
MAPT_AWS_PARAMS = {
    "arch",
    "cluster-access-secret-name",
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
    "secret-aws-credentials",
    "secret-ocp-snc-credentials",
    "service-endpoints",
    "spot",
    "spot-eviction-tolerance",
    "spot-excluded-regions",
    "spot-increase-rate",
    "tags",
    "timeout",
    "version",
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


def test_local_resolver_paths_exist():
    for task in _pipeline()["spec"]["tasks"] + _pipeline()["spec"]["finally"]:
        resolver = _resolver_params(task)
        if resolver["url"] == LOCAL_REPO:
            assert (ROOT / resolver["pathInRepo"]).is_file(), resolver["pathInRepo"]


def test_the_cluster_comes_from_the_mapt_aws_snc_task():
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
    # depends on: mapt marks id, operation and the AWS credentials as required.
    create = _params(_tasks()["create-snc-cluster"])
    destroy = _params(_finally()["delete-snc-cluster"])
    for param in ("id", "operation", "secret-aws-credentials",
                  "secret-ocp-snc-credentials"):
        assert param in create, param
        assert param in destroy, param


def test_the_pipeline_only_passes_declared_aws_params():
    for name in ("create-snc-cluster", "delete-snc-cluster"):
        task = (_tasks() if name in _tasks() else _finally())[name]
        unknown = set(_params(task)) - MAPT_AWS_PARAMS
        assert not unknown, (name, unknown)


def test_the_create_never_asks_for_spot():
    # The task default is true, and a metal instance takes no spot capacity.
    # The pipeline must say false, and it must offer no knob to turn it on.
    params = {p["name"] for p in _pipeline()["spec"]["params"]}
    assert "spot" not in params
    assert _params(_tasks()["create-snc-cluster"])["spot"] == "false"


def test_the_pipeline_runs_x86_64_only():
    # A metal instance for the guest is x86_64. The task takes arch, so the
    # pipeline sets it and offers no param that promises another architecture.
    params = {p["name"] for p in _pipeline()["spec"]["params"]}
    assert "architecture" not in params
    create = _params(_tasks()["create-snc-cluster"])
    assert create["arch"] == "x86_64"
    config = {p["name"]: p for p in _tasks()["create-configuration"]["params"]}
    assert config["architecture"]["value"] == "x86_64"


def test_the_ocp_version_names_a_bundle_version():
    # On AWS the version selects the AMI openshift-local-<version>-x86_64, not a
    # catalog image. A reader must not read the IBM Cloud meaning here.
    params = {p["name"]: p for p in _pipeline()["spec"]["params"]}
    default = params["ocp-version"]["default"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", default), default
    description = params["ocp-version"]["description"]
    assert "bundle" in description
    assert "AMI" in description
    # The IBM Cloud pipeline reads a catalog image with this param. Say that
    # this pipeline does not, so a reader does not wait for a catalog.
    assert "not a catalog image" in description
    assert _params(_tasks()["create-snc-cluster"])["version"] == "$(params.ocp-version)"


def test_the_create_requests_nested_virtualization():
    # The profile installs OpenShift Virtualization. The explicit request keeps
    # the metal selection even if the profile list changes.
    create = _params(_tasks()["create-snc-cluster"])
    assert create["profile"] == "$(tasks.create-configuration.results.snc-profile)"
    assert create["nested-virt"] == "true"

    script = _load(CONFIG_TASK)["spec"]["steps"][0]["script"]
    assert 'snc_profile="virtualization"' in script
    assert "*-disk-image*)" in script


def test_the_instance_choice_can_be_pinned():
    # The nested virtualization request filters on the bare metal flag and the
    # architecture only, so cpus and memory do not narrow the choice. The
    # instance list is the pin, and mapt takes it as it is, so the description
    # must ask for a bare metal type and name the price.
    params = {p["name"]: p for p in _pipeline()["spec"]["params"]}
    description = params["snc-instance-types"]["description"]
    assert "price" in description
    assert "bare metal" in description
    assert "/dev/kvm" in description
    create = _params(_tasks()["create-snc-cluster"])
    assert create["compute-sizes"] == "$(params.snc-instance-types)"
    assert "cpus" not in create and "memory" not in create


def test_the_root_volume_holds_the_install_and_the_guest():
    params = {p["name"]: p for p in _pipeline()["spec"]["params"]}
    assert params["snc-disk-size"]["default"] == "1000"
    create = _params(_tasks()["create-snc-cluster"])
    assert create["disk-size"] == "$(params.snc-disk-size)"


def test_the_tags_hold_the_run_name():
    # A tag carries no URL. The run name identifies the run on the provider
    # console, and no provider rejects it.
    tags = _params(_tasks()["create-snc-cluster"])["tags"]
    assert "://" not in tags, tags
    for char in ("/", "\\", "@", "#", "?"):
        assert char not in tags, (char, tags)
    assert "pipeline-run-url=$(context.pipelineRun.name)" in tags


def test_create_and_destroy_share_the_mapt_id_and_secret():
    create = _params(_tasks()["create-snc-cluster"])
    destroy = _params(_finally()["delete-snc-cluster"])

    assert create["id"] == destroy["id"] == "$(context.pipelineRun.name)"
    assert (
        create["secret-ocp-snc-credentials"]
        == destroy["secret-ocp-snc-credentials"]
        == "$(params.ocp-snc-credentials-secret)"
    )
    assert (
        create["secret-aws-credentials"]
        == destroy["secret-aws-credentials"]
        == "$(params.aws-credentials-secret)"
    )
    assert create["operation"] == "create"
    assert destroy["operation"] == "destroy"


def test_destroy_always_runs():
    # A failed run must not leave a metal instance on the account.
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


def test_the_run_has_a_timeout_budget():
    # The cluster Tekton rejects spec.timeouts on a Pipeline, so the budget
    # must live on the tasks. The cluster creation alone takes about 45 minutes.
    spec = _pipeline()["spec"]
    assert "timeouts" not in spec
    tasks = _tasks()
    assert tasks["create-snc-cluster"]["timeout"] == "2h0m0s"
    assert tasks["verify-saw-disk-image"]["timeout"] == "2h0m0s"
    finally_tasks = {t["name"]: t for t in spec["finally"]}
    assert finally_tasks["delete-snc-cluster"]["timeout"] == "2h0m0s"


def test_the_two_pipelines_check_the_same_contract():
    # The cloud can change, the contract cannot. Both pipelines boot the same
    # artifact and check it with the same task and the same params.
    def verify_params(document):
        pipeline = _load(document)
        tasks = {task["name"]: task for task in pipeline["spec"]["tasks"]}
        return _params(tasks["verify-saw-disk-image"]), {
            r["name"]: r["value"] for r in pipeline["spec"]["results"]
        }

    aws_params, aws_results = verify_params(PIPELINE)
    ibm_params, ibm_results = verify_params(IBMCLOUD_PIPELINE)
    assert aws_params == ibm_params
    assert aws_results == ibm_results


def test_the_secret_doc_names_the_secrets_the_pipeline_reads():
    # A tenant operator reads the doc, not the YAML. Keep the two in step.
    doc = SECRET_DOC.read_text()
    pipeline_params = {p["name"]: p for p in _pipeline()["spec"]["params"]}
    verify_params = {p["name"]: p for p in _load(VERIFY_TASK)["spec"]["params"]}
    names = [
        pipeline_params["aws-credentials-secret"].get("default"),
        pipeline_params["ocp-snc-credentials-secret"].get("default"),
        verify_params["pull-secret-name"].get("default"),
    ]
    for name in names:
        assert name, "a secret param has no default name"
        assert name in doc, f"{name} is not in {SECRET_DOC.name}"


def test_the_secret_doc_names_the_vault_records():
    # The record path is the delivery contract. A typo in it syncs nothing,
    # and the create step then fails on a missing key.
    doc = SECRET_DOC.read_text()
    for path in (
        "ai-aipcc-cicd/service/release-pipeline/ai-incubator/cloud-providers/aws",
        "ai-aipcc-cicd/service/release-pipeline/ai-incubator/ocp/pullsecret",
    ):
        assert path in doc, path


def test_the_secret_doc_lists_the_keys_of_each_secret():
    doc = SECRET_DOC.read_text()
    for key in ("access-key", "secret-key", "region", "bucket", "pull-secret",
                ".dockerconfigjson"):
        assert f"`{key}`" in doc, key


def test_the_secret_doc_does_not_name_a_secret_the_pipeline_never_reads():
    doc = SECRET_DOC.read_text()
    required = doc.split("## Required secrets", 1)[1].split("\n## ", 1)[0]
    body = PIPELINE.read_text() + VERIFY_TASK.read_text()
    rows = [row for row in required.splitlines() if row.startswith("| `")]
    assert rows, "the required table names no secret"
    for row in rows:
        name = row.split("`")[1]
        assert name in body, name
