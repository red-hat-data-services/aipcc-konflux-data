#!/usr/bin/env python3
"""Check task bundle versions in pipelines against the trusted task rules
from the rhtap-ec-policy repository (ADR 0053).

Tasks from quay.io/konflux-ci/tekton-catalog are trusted by default, but
specific versions may be denied via version constraints with optional
effective_on dates.

Requires: pyyaml

Usage:
    ./scripts/check-trusted-tasks.py [konflux-data-path]
"""
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

BUNDLE_RE = re.compile(
    r"value:\s*quay\.io/konflux-ci/tekton-catalog/task-"
    r"([^:]+):(\S+)@sha256:[0-9a-f]+"
)

RULES_URL = (
    "https://raw.githubusercontent.com/release-engineering/rhtap-ec-policy/"
    "main/data/trusted_task_rules.yaml"
)


def parse_version(v):
    """Parse a version string into a tuple of ints for comparison."""
    return tuple(int(x) for x in v.split("."))


def collect_pipeline_tasks(repo_path):
    """Extract unique (task_name, version, filename) tuples from pipeline YAMLs."""
    tasks = set()
    pipelines_dir = Path(repo_path) / "pipelines"
    for yaml_file in sorted(pipelines_dir.glob("*.yaml")):
        for m in BUNDLE_RE.finditer(yaml_file.read_text()):
            tasks.add((m.group(1), m.group(2), yaml_file.name))
    return sorted(tasks)


def fetch_deny_rules():
    """Fetch deny rules from trusted_task_rules.yaml.

    Returns a list of (task_name, min_version, effective_on) dicts.
    min_version is None when all versions are denied.
    effective_on is a datetime or None.
    """
    with urllib.request.urlopen(RULES_URL) as resp:
        data = yaml.safe_load(resp.read().decode())
    return parse_deny_rules(data)


def parse_deny_rules(data):
    """Parse deny rules from already-loaded YAML data."""
    rules = []
    deny_groups = (
        data.get("rule_data", {})
        .get("trusted_task_rules", {})
        .get("deny", {})
    )
    for group_rules in deny_groups.values():
        for rule in group_rules:
            task_name = rule["pattern"].split("/task-", 1)[-1]

            min_version = None
            for v in rule.get("versions", []):
                if v.startswith("<"):
                    min_version = v[1:]

            effective_on = None
            if "effective_on" in rule:
                effective_on = datetime.fromisoformat(rule["effective_on"])

            rules.append((task_name, min_version, effective_on))
    return rules


def check_task(task_name, version, deny_rules):
    """Check a task version against deny rules.

    Returns (status, detail) where status is 'ok', 'denied', or 'warning'.
    'warning' means a future rule will deny this version.
    """
    now = datetime.now(timezone.utc)
    upcoming = None

    for rule_name, min_version, effective_on in deny_rules:
        if rule_name != task_name:
            continue

        version_matches = True
        if min_version is not None:
            version_matches = parse_version(version) < parse_version(min_version)

        if not version_matches:
            continue

        if effective_on is None or effective_on <= now:
            if min_version:
                return "denied", f"version {version} < {min_version}"
            return "denied", "all versions denied"

        days = (effective_on - now).days
        detail = (
            f"denied in {days}d (min {min_version})"
            if min_version
            else f"denied in {days}d"
        )
        if upcoming is None or effective_on < upcoming[0]:
            upcoming = (effective_on, detail)

    if upcoming:
        return "warning", upcoming[1]
    return "ok", None


def main():
    repo_path = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent
    )

    tasks = collect_pipeline_tasks(repo_path)
    if not tasks:
        print("No task bundle refs found")
        sys.exit(0)

    deny_rules = fetch_deny_rules()
    denied = {}
    seen_ok = set()

    for task_name, version, filename in tasks:
        status, detail = check_task(task_name, version, deny_rules)
        if status == "denied":
            entry = denied.setdefault(task_name, {"version": version, "reason": detail, "files": []})
            entry["files"].append(filename)
        else:
            if task_name not in seen_ok:
                suffix = f"  ({detail})" if detail else ""
                print(f"  ✓  {task_name}:{version}{suffix}")
                seen_ok.add(task_name)

    for name in sorted(denied):
        entry = denied[name]
        print(f"  ✗  {name}:{entry['version']}  DENIED  ({', '.join(entry['files'])})")
        print(f"       {entry['reason']}")

    if denied:
        print(f"\n{len(denied)} denied task(s)")
        sys.exit(1)
    else:
        print("\nAll tasks meet version requirements.")


if __name__ == "__main__":
    main()