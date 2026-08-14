import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from importlib import import_module

ctt = import_module("check-trusted-tasks")


# -- task_name_from_ref --


@pytest.mark.parametrize(
    "image_ref, expected",
    [
        ("quay.io/konflux-ci/tekton-catalog/task-apply-tags:0.2", "apply-tags"),
        ("quay.io/konflux-ci/tekton-catalog/task-init:0.4", "init"),
        ("quay.io/konflux-ci/tekton-catalog/task-buildah-remote-oci-ta:0.10", "buildah-remote-oci-ta"),
        ("task-git-clone:1.0", "git-clone"),
        ("some-task", "some-task"),
    ],
)
def test_task_name_from_ref(image_ref, expected):
    assert ctt.task_name_from_ref(image_ref) == expected


# -- check_ref --


DIGEST_A = "sha256:aaaa"
DIGEST_B = "sha256:bbbb"
FUTURE = datetime.now(timezone.utc) + timedelta(days=90)


def _make_store(entries):
    """Build a store dict from (provenance_uri, digest, expires) tuples."""
    store = {}
    for uri, digest, expires in entries:
        store.setdefault(uri, []).append((digest, expires))
    return store


def test_check_ref_trusted_no_expiry():
    store = _make_store([("oci://img:1.0", DIGEST_A, None)])
    found, expires = ctt.check_ref("img:1.0", DIGEST_A, store)
    assert found is True
    assert expires is None


def test_check_ref_trusted_with_expiry():
    store = _make_store([("oci://img:1.0", DIGEST_A, FUTURE)])
    found, expires = ctt.check_ref("img:1.0", DIGEST_A, store)
    assert found is True
    assert expires == FUTURE


def test_check_ref_wrong_digest():
    store = _make_store([("oci://img:1.0", DIGEST_A, None)])
    found, expires = ctt.check_ref("img:1.0", DIGEST_B, store)
    assert found is False
    assert expires is None


def test_check_ref_missing_key():
    store = _make_store([("oci://other:1.0", DIGEST_A, None)])
    found, _ = ctt.check_ref("img:1.0", DIGEST_A, store)
    assert found is False


def test_check_ref_empty_store():
    found, expires = ctt.check_ref("img:1.0", DIGEST_A, {})
    assert found is False
    assert expires is None


def test_check_ref_finds_in_expired_store():
    past = datetime.now(timezone.utc) - timedelta(days=10)
    store = _make_store([("oci://img:1.0", DIGEST_A, past)])
    found, expires = ctt.check_ref("img:1.0", DIGEST_A, store)
    assert found is True
    assert expires == past


# -- load_renovate_skips --


def test_load_renovate_skips_no_file(tmp_path):
    assert ctt.load_renovate_skips(str(tmp_path)) == set()


def test_load_renovate_skips_parses_disabled_rules(tmp_path):
    config = {
        "tekton": {
            "packageRules": [
                {
                    "matchFileNames": ["pipelines/modelcar.yaml", "pipelines/models-oci-copy.yaml"],
                    "matchPackageNames": ["quay.io/konflux-ci/tekton-catalog/task-apply-tags"],
                    "enabled": False,
                },
                {
                    "matchUpdateTypes": ["digest"],
                    "automerge": True,
                },
            ]
        }
    }
    (tmp_path / "renovate.json").write_text(json.dumps(config))
    skips = ctt.load_renovate_skips(str(tmp_path))
    assert skips == {
        ("quay.io/konflux-ci/tekton-catalog/task-apply-tags", "modelcar.yaml"),
        ("quay.io/konflux-ci/tekton-catalog/task-apply-tags", "models-oci-copy.yaml"),
    }


# -- collect_konflux_data_refs --


PIPELINE_CONTENT = """\
spec:
  tasks:
    - name: init
      params:
        - name: bundle
          value: quay.io/konflux-ci/tekton-catalog/task-init:0.4@sha256:aabbccdd
    - name: build
      params:
        - name: bundle
          value: quay.io/konflux-ci/tekton-catalog/task-buildah:0.2@sha256:11223344
"""


def test_collect_refs(tmp_path):
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "test.yaml").write_text(PIPELINE_CONTENT)
    refs = ctt.collect_konflux_data_refs(str(tmp_path))
    assert len(refs) == 2
    image_refs = {r[0] for r in refs}
    assert "quay.io/konflux-ci/tekton-catalog/task-init:0.4" in image_refs
    assert "quay.io/konflux-ci/tekton-catalog/task-buildah:0.2" in image_refs


def test_collect_refs_respects_skips(tmp_path):
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "modelcar.yaml").write_text(
        "    - name: bundle\n"
        "      value: quay.io/konflux-ci/tekton-catalog/task-apply-tags:0.2@sha256:aabb\n"
        "    - name: bundle\n"
        "      value: quay.io/konflux-ci/tekton-catalog/task-init:0.4@sha256:ccdd\n"
    )
    config = {
        "tekton": {
            "packageRules": [
                {
                    "matchFileNames": ["pipelines/modelcar.yaml"],
                    "matchPackageNames": ["quay.io/konflux-ci/tekton-catalog/task-apply-tags"],
                    "enabled": False,
                }
            ]
        }
    }
    (tmp_path / "renovate.json").write_text(json.dumps(config))
    refs = ctt.collect_konflux_data_refs(str(tmp_path))
    assert len(refs) == 1
    assert refs[0][0] == "quay.io/konflux-ci/tekton-catalog/task-init:0.4"


def test_collect_refs_empty_dir(tmp_path):
    (tmp_path / "pipelines").mkdir()
    assert ctt.collect_konflux_data_refs(str(tmp_path)) == []


# -- fetch_trust_store --


def _make_trust_yaml(entries):
    """Build trust store YAML data from (key, ref, expires_on_iso|None) tuples."""
    trusted_tasks = {}
    for key, ref, expires_on in entries:
        trusted_tasks.setdefault(key, []).append(
            {"ref": ref, **({"expires_on": expires_on} if expires_on else {})}
        )
    return {"trusted_tasks": trusted_tasks}


def _stub_urlopen(trust_data):
    """Return a side_effect callable simulating two sequential quay.io requests."""
    manifest = {"layers": [{"digest": "sha256:blobdigest"}]}
    blob_bytes = yaml.dump(trust_data).encode()
    call_count = 0

    def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.read.return_value = (
            json.dumps(manifest).encode() if call_count == 1 else blob_bytes
        )
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    return side_effect


def test_fetch_trust_store_separates_active_and_expired(mocker):
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()
    data = _make_trust_yaml([
        ("oci://img:1.0", "sha256:active", future),
        ("oci://img:1.0", "sha256:expired", past),
        ("oci://img:2.0", "sha256:noexpiry", None),
    ])
    mocker.patch("urllib.request.urlopen", side_effect=_stub_urlopen(data))

    active, expired = ctt.fetch_trust_store()

    active_digests = [ref for ref, _ in active["oci://img:1.0"]]
    assert "sha256:active" in active_digests
    assert "sha256:expired" not in active_digests

    expired_digests = [ref for ref, _ in expired["oci://img:1.0"]]
    assert "sha256:expired" in expired_digests

    assert "sha256:noexpiry" in [ref for ref, _ in active["oci://img:2.0"]]


def test_fetch_trust_store_skips_empty_ref(mocker):
    data = _make_trust_yaml([("oci://img:1.0", "sha256:good", None)])
    data["trusted_tasks"]["oci://img:1.0"].append({"ref": ""})
    mocker.patch("urllib.request.urlopen", side_effect=_stub_urlopen(data))

    active, _ = ctt.fetch_trust_store()
    assert len(active["oci://img:1.0"]) == 1


def test_fetch_trust_store_all_expired_not_in_active(mocker):
    past = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    data = _make_trust_yaml([("oci://img:1.0", "sha256:old", past)])
    mocker.patch("urllib.request.urlopen", side_effect=_stub_urlopen(data))

    active, expired = ctt.fetch_trust_store()
    assert "oci://img:1.0" not in active
    assert "oci://img:1.0" in expired


# -- main --


def _pipeline_with_task(tmp_path, ref="task-init:0.4", digest="sha256:aabb"):
    (tmp_path / "pipelines").mkdir(exist_ok=True)
    (tmp_path / "pipelines" / "test.yaml").write_text(
        f"      value: quay.io/konflux-ci/tekton-catalog/{ref}@{digest}\n"
    )


def test_main_all_trusted(tmp_path, capsys, mocker):
    _pipeline_with_task(tmp_path)
    active = {"oci://quay.io/konflux-ci/tekton-catalog/task-init:0.4": [("sha256:aabb", None)]}
    mocker.patch.object(ctt, "fetch_trust_store", return_value=(active, {}))

    sys.argv = ["check-trusted-tasks.py", str(tmp_path)]
    ctt.main()

    assert "All tasks trusted" in capsys.readouterr().out


def test_main_untrusted_exits_1(tmp_path, capsys, mocker):
    _pipeline_with_task(tmp_path)
    mocker.patch.object(ctt, "fetch_trust_store", return_value=({}, {}))

    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["check-trusted-tasks.py", str(tmp_path)]
        ctt.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "UNTRUSTED" in out
    assert "1 untrusted" in out


def test_main_expired_shows_expired_label(tmp_path, capsys, mocker):
    _pipeline_with_task(tmp_path)
    past = datetime.now(timezone.utc) - timedelta(days=5)
    expired = {"oci://quay.io/konflux-ci/tekton-catalog/task-init:0.4": [("sha256:aabb", past)]}
    mocker.patch.object(ctt, "fetch_trust_store", return_value=({}, expired))

    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["check-trusted-tasks.py", str(tmp_path)]
        ctt.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "EXPIRED" in out
    assert "UNTRUSTED" not in out