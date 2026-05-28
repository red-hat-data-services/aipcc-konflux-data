#!/usr/bin/env python3
"""Compare task bundle digests in konflux-data pipelines against the live
conforma trust store (data-acceptable-bundles OCI artifact on quay.io).

Matches digests by provenance URI and respects expires_on dates, mirroring
how conforma evaluates trust at release time.

Requires: pyyaml

Usage:
    ./scripts/check-trusted-tasks.py [konflux-data-path]

Examples:
    ./scripts/check-trusted-tasks.py
    ./scripts/check-trusted-tasks.py /path/to/konflux-data
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

BUNDLE_RE = re.compile(
    r"value:\s*(quay\.io/konflux-ci/tekton-catalog/task-"
    r"[^:]+:\S+)@(sha256:[0-9a-f]+)"
)
TRUST_REPO = "konflux-ci/tekton-catalog/data-acceptable-bundles"
TRUST_TAG = "latest"


def load_renovate_skips(repo_path):
    """Parse renovate.json for disabled package/file combinations.

    Returns a set of (package_prefix, filename) tuples to skip.
    """
    skips = set()
    renovate_path = Path(repo_path) / "renovate.json"
    if not renovate_path.is_file():
        return skips
    with open(renovate_path) as f:
        config = json.load(f)
    for rule in config.get("tekton", {}).get("packageRules", []):
        if rule.get("enabled") is False:
            for pkg in rule.get("matchPackageNames", []):
                for fn in rule.get("matchFileNames", []):
                    skips.add((pkg, Path(fn).name))
    return skips


def collect_konflux_data_refs(repo_path):
    """Extract unique (image_ref, digest, filename) tuples from pipeline YAMLs.

    image_ref is e.g. "quay.io/konflux-ci/tekton-catalog/task-apply-tags:0.2"
    Skips refs that renovate.json has explicitly disabled.
    """
    skips = load_renovate_skips(repo_path)
    refs = set()
    pipelines_dir = Path(repo_path) / "pipelines"
    for yaml_file in sorted(pipelines_dir.glob("*.yaml")):
        for m in BUNDLE_RE.finditer(yaml_file.read_text()):
            image_ref = m.group(1)
            pkg_prefix = image_ref.rsplit(":", 1)[0]
            if (pkg_prefix, yaml_file.name) in skips:
                continue
            refs.add((image_ref, m.group(2), yaml_file.name))
    return sorted(refs)


def fetch_trust_store():
    """Fetch the trusted task list from quay.io.

    Returns a dict mapping provenance URI -> list of unexpired (ref, expires_on) tuples.
    """
    base = f"https://quay.io/v2/{TRUST_REPO}"

    req = urllib.request.Request(
        f"{base}/manifests/{TRUST_TAG}",
        headers={"Accept": "application/vnd.oci.image.manifest.v1+json"},
    )
    with urllib.request.urlopen(req) as resp:
        manifest = json.loads(resp.read())

    blob_digest = manifest["layers"][0]["digest"]

    with urllib.request.urlopen(f"{base}/blobs/{blob_digest}") as resp:
        data = yaml.safe_load(resp.read().decode())

    now = datetime.now(timezone.utc)
    store = {}

    for key, entries in data.get("trusted_tasks", {}).items():
        unexpired = []
        for entry in entries:
            ref = entry.get("ref", "")
            if not ref:
                continue
            expires_on = entry.get("expires_on")
            if expires_on:
                expires_dt = datetime.fromisoformat(expires_on)
                if expires_dt <= now:
                    continue
                unexpired.append((ref, expires_dt))
            else:
                unexpired.append((ref, None))
        if unexpired:
            store[key] = unexpired

    return store


def check_ref(image_ref, digest, store):
    """Check if a digest is trusted for the given image ref.

    Returns (trusted, expires_on) where trusted is bool and
    expires_on is a datetime or None.
    """
    provenance_key = f"oci://{image_ref}"
    entries = store.get(provenance_key, [])
    for ref, expires in entries:
        if ref == digest:
            return True, expires
    return False, None


def task_name_from_ref(image_ref):
    """Extract short task name from image ref."""
    name = image_ref.split("/")[-1]
    if ":" in name:
        name = name.split(":")[0]
    if name.startswith("task-"):
        name = name[5:]
    return name


def main():
    konflux_data = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent
    )

    refs = collect_konflux_data_refs(konflux_data)
    if not refs:
        print("No task bundle refs found")
        sys.exit(0)

    store = fetch_trust_store()
    mismatches = {}
    seen_trusted = set()

    for image_ref, digest, filename in refs:
        name = task_name_from_ref(image_ref)
        trusted, expires = check_ref(image_ref, digest, store)
        if trusted:
            if name not in seen_trusted:
                if expires:
                    days = (expires - datetime.now(timezone.utc)).days
                    print(f"  ✓  {name}  (expires in {days}d)")
                else:
                    print(f"  ✓  {name}")
                seen_trusted.add(name)
        else:
            mismatches.setdefault(name, (digest, []))[1].append(filename)

    for name in sorted(mismatches):
        digest, files = mismatches[name]
        print(f"  ✗  {name}  ({', '.join(files)})")
        print(f"       {digest}")

    if mismatches:
        print(f"\n{len(mismatches)} untrusted task(s)")
        sys.exit(1)
    else:
        print("\nAll tasks trusted.")


if __name__ == "__main__":
    main()
