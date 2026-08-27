import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from importlib import import_module

ctt = import_module("check-trusted-tasks")


# -- parse_version --


@pytest.mark.parametrize(
    "version, expected",
    [
        ("0.4", (0, 4)),
        ("0.10", (0, 10)),
        ("0.7.1", (0, 7, 1)),
        ("1.0", (1, 0)),
    ],
)
def test_parse_version(version, expected):
    assert ctt.parse_version(version) == expected


# -- collect_pipeline_tasks --


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
          value: quay.io/konflux-ci/tekton-catalog/task-buildah:0.10@sha256:11223344
"""


def test_collect_pipeline_tasks(tmp_path):
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "test.yaml").write_text(PIPELINE_CONTENT)
    tasks = ctt.collect_pipeline_tasks(str(tmp_path))
    assert len(tasks) == 2
    names = {(t[0], t[1]) for t in tasks}
    assert ("init", "0.4") in names
    assert ("buildah", "0.10") in names


def test_collect_pipeline_tasks_empty_dir(tmp_path):
    (tmp_path / "pipelines").mkdir()
    assert ctt.collect_pipeline_tasks(str(tmp_path)) == []


def test_collect_pipeline_tasks_multiple_files(tmp_path):
    (tmp_path / "pipelines").mkdir()
    (tmp_path / "pipelines" / "a.yaml").write_text(
        "      value: quay.io/konflux-ci/tekton-catalog/task-init:0.4@sha256:aabb\n"
    )
    (tmp_path / "pipelines" / "b.yaml").write_text(
        "      value: quay.io/konflux-ci/tekton-catalog/task-init:0.4@sha256:aabb\n"
    )
    tasks = ctt.collect_pipeline_tasks(str(tmp_path))
    assert len(tasks) == 2
    assert tasks[0][2] == "a.yaml"
    assert tasks[1][2] == "b.yaml"


# -- parse_deny_rules --


def _make_rules_yaml(deny_entries):
    """Build rules YAML from list of dicts with pattern, versions, effective_on."""
    return {
        "rule_data": {
            "trusted_task_rules": {
                "allow": {"defaults": [{"pattern": "oci://quay.io/konflux-ci/tekton-catalog/task-*"}]},
                "deny": {"defaults": deny_entries},
            }
        }
    }


def test_parse_deny_rules_basic():
    data = _make_rules_yaml([
        {"pattern": "oci://quay.io/konflux-ci/tekton-catalog/task-init", "versions": ["<0.3"]},
    ])
    rules = ctt.parse_deny_rules(data)
    assert len(rules) == 1
    assert rules[0] == ("init", "0.3", None)


def test_parse_deny_rules_with_effective_on():
    data = _make_rules_yaml([
        {
            "pattern": "oci://quay.io/konflux-ci/tekton-catalog/task-buildah",
            "versions": ["<0.9"],
            "effective_on": "2026-08-06T00:00:00Z",
        },
    ])
    rules = ctt.parse_deny_rules(data)
    assert rules[0][0] == "buildah"
    assert rules[0][1] == "0.9"
    assert rules[0][2] == datetime(2026, 8, 6, tzinfo=timezone.utc)


def test_parse_deny_rules_no_versions():
    data = _make_rules_yaml([
        {
            "pattern": "oci://quay.io/konflux-ci/tekton-catalog/task-show-sbom",
            "effective_on": "2026-09-05T00:00:00Z",
        },
    ])
    rules = ctt.parse_deny_rules(data)
    assert rules[0][1] is None


def test_parse_deny_rules_empty():
    data = {"rule_data": {"trusted_task_rules": {"deny": {}}}}
    assert ctt.parse_deny_rules(data) == []


# -- check_task --


PAST = datetime.now(timezone.utc) - timedelta(days=10)
FUTURE = datetime.now(timezone.utc) + timedelta(days=30)


def test_check_task_ok_above_minimum():
    rules = [("init", "0.3", None)]
    status, detail = ctt.check_task("init", "0.4", rules)
    assert status == "ok"
    assert detail is None


def test_check_task_denied_below_minimum():
    rules = [("init", "0.3", None)]
    status, detail = ctt.check_task("init", "0.2", rules)
    assert status == "denied"
    assert "0.2 < 0.3" in detail


def test_check_task_denied_effective_in_past():
    rules = [("buildah", "0.9", PAST)]
    status, detail = ctt.check_task("buildah", "0.8", rules)
    assert status == "denied"


def test_check_task_warning_effective_in_future():
    rules = [("buildah", "0.10", FUTURE)]
    status, detail = ctt.check_task("buildah", "0.9", rules)
    assert status == "warning"
    assert "denied in" in detail
    assert "min 0.10" in detail


def test_check_task_denied_all_versions():
    rules = [("show-sbom", None, PAST)]
    status, detail = ctt.check_task("show-sbom", "0.1", rules)
    assert status == "denied"
    assert "all versions denied" in detail


def test_check_task_warning_all_versions_future():
    rules = [("summary", None, FUTURE)]
    status, detail = ctt.check_task("summary", "0.1", rules)
    assert status == "warning"
    assert "denied in" in detail


def test_check_task_no_matching_rules():
    rules = [("init", "0.3", None)]
    status, detail = ctt.check_task("buildah", "0.1", rules)
    assert status == "ok"


def test_check_task_empty_rules():
    status, detail = ctt.check_task("init", "0.4", [])
    assert status == "ok"


def test_check_task_version_at_boundary():
    rules = [("init", "0.3", None)]
    status, _ = ctt.check_task("init", "0.3", rules)
    assert status == "ok"


def test_check_task_multiple_rules_picks_effective():
    rules = [
        ("init", "0.3", PAST),
        ("init", "0.4", FUTURE),
    ]
    status, detail = ctt.check_task("init", "0.3", rules)
    assert status == "warning"
    assert "min 0.4" in detail


# -- main --


def _pipeline_with_task(tmp_path, name="init", version="0.4", digest="sha256:aabb"):
    (tmp_path / "pipelines").mkdir(exist_ok=True)
    (tmp_path / "pipelines" / "test.yaml").write_text(
        f"      value: quay.io/konflux-ci/tekton-catalog/task-{name}:{version}@{digest}\n"
    )


def test_main_all_ok(tmp_path, capsys, mocker):
    _pipeline_with_task(tmp_path)
    rules = [("init", "0.3", None)]
    mocker.patch.object(ctt, "fetch_deny_rules", return_value=rules)

    sys.argv = ["check-trusted-tasks.py", str(tmp_path)]
    ctt.main()

    assert "All tasks meet version requirements" in capsys.readouterr().out


def test_main_denied_exits_1(tmp_path, capsys, mocker):
    _pipeline_with_task(tmp_path, version="0.2")
    rules = [("init", "0.3", None)]
    mocker.patch.object(ctt, "fetch_deny_rules", return_value=rules)

    with pytest.raises(SystemExit) as exc_info:
        sys.argv = ["check-trusted-tasks.py", str(tmp_path)]
        ctt.main()

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "DENIED" in out
    assert "1 denied" in out


def test_main_warning_still_passes(tmp_path, capsys, mocker):
    _pipeline_with_task(tmp_path, version="0.3")
    rules = [
        ("init", "0.3", PAST),
        ("init", "0.4", FUTURE),
    ]
    mocker.patch.object(ctt, "fetch_deny_rules", return_value=rules)

    sys.argv = ["check-trusted-tasks.py", str(tmp_path)]
    ctt.main()

    out = capsys.readouterr().out
    assert "denied in" in out
    assert "All tasks meet version requirements" in out