# Manual ACS CVE scan diff

Use `acs-scan-diff.yaml` before a release, without changing ReleasePlans. Existing
Clair definitions and callers remain unchanged during this branch-only trial.

## Start a run

1. Publish the feature branch and select its full 40-character commit SHA. Put
   the **same SHA** in `pipelineRef.params.revision` and `params.task-revision`.
   Nested task references use that parameter, not `main`. The commit identifies
   the pipeline/client code, **not a frozen Central vulnerability database**.
2. Copy the example to a task-owned working directory and replace every
   `<PLACEHOLDER>`. Supply the component name, the previous published image's
   source repository/index digest and the candidate's exact source repository/
   index digest. Tags alone are rejected; `repository:tag@sha256:digest` is
   normalized to `repository@sha256:digest` by the resolver.
3. Choose an existing ACS-authorized service account. The CPU 3.5.4 control used
   `build-pipeline-base-image-cpu-3-5` in `ai-tenant`. This is evidence for that
   account only: access for other component accounts and
   `aipcc-releaser-bot-sa` must be checked, not assumed or granted automatically.
   The resolver needs the existing `components-namespace-pull` secret. ACS pulls
   through Central's existing registry integration, not that mounted secret.
4. Create the prepared run only with operator authorization:

   ```sh
   oc create -f <PREPARED_PIPELINERUN.yaml>
   tkn pipelinerun logs -f -n ai-tenant <PIPELINERUN_NAME>
   ```

All four base-image Linux architectures are supported: amd64, arm64, ppc64le,
s390x. Single-platform pairs also work. Baseline/candidate coverage must match;
unknown architectures, unsupported variants, duplicate platforms and inspection
errors fail rather than inventing a partial comparison.

The default manual mode does **not** mutate Snapshots or upload registry reports.
It does call Central's scan API with `--force --include-snoozed`. Add a `snapshot`
parameter only if you intentionally want to publish annotations to the candidate
Snapshot and the chosen account has get/patch permission for it. The component's
candidate digest must match that Snapshot. Annotation is last-writer-wins;
failed runs do not erase earlier evidence, so always check the producing run.

### Resolve a baseline from an existing release

Instead of `component`, `baseline-ref`, and `candidate-ref`, supply `release`,
`releasePlan`, and `snapshot`. These must identify the same candidate release.
The resolver obtains its component label and finds the latest `Released=True`
prod release for that component; stale sibling entries in Snapshots are ignored.
`prod-release-plan` is optional when replacing `-stage-` with `-prod-` gives the
correct plan. This mode also needs get/list releases and get snapshots.

Run before the candidate is promoted to prod: otherwise the latest prod baseline
may already be the candidate. Use explicit image mode to preserve the intended
older baseline after promotion. A failed run never gates or approves a release.
No stage ReleasePlan references this pipeline yet.

## Collect evidence

After the run ends, require its `Succeeded` condition to be `True` before using
any results. A successful scan with CVEs is not a policy failure. Authentication,
missing scan data, stale/partial coverage, wrong digests or mismatched platforms
are failures, not empty CVE lists. The client must support forced scans; each
returned scan timestamp must fall within its request window with five minutes
of clock-skew allowance. Native protobuf JSON may omit empty repeated fields;
those are accepted only within an otherwise valid, digest-bound, timestamped scan.

For plain JSON without the `tkn` log prefixes:

```sh
RUN=<PIPELINERUN_NAME>
test "$(oc get pipelinerun "$RUN" -n ai-tenant -o jsonpath='{.status.conditions[?(@.type=="Succeeded")].status}')" = True
POD=$(oc get taskrun -n ai-tenant -l "tekton.dev/pipelineRun=$RUN,tekton.dev/pipelineTask=scan" -o jsonpath='{.items[0].status.podName}')
oc logs "$POD" -n ai-tenant -c step-compare-and-annotate > "$RUN.log"
awk '/^===RESULT-BEGIN===$/{on=1;next}/^===RESULT-END===$/{on=0}on' "$RUN.log" > "$RUN.json"
jq '{component, engine, code_revision, baseline: .baseline.cves, candidate: .candidate.cves, fixed_in_candidate, new_in_candidate, platform_diffs}' "$RUN.json"
```

The marked result records the producing run, code revision, client digest/version,
Central endpoint, platform manifest bindings, scan times and scanner/data-source
metadata when available. Database revision/consistency is explicitly unknown;
scanner-version changes are flagged per platform. Pinning the client and pairing
scans close together cannot guarantee identical Central databases or current
coverage. Keep the run specification with the evidence and periodically check
coverage against exact-image Catalog findings.

Raw native **scan** sections between `===REPORT ...` markers retain package,
severity, advisory and available-fix metadata. Unrelated image layers/build
commands/labels are excluded. Full reports stay in logs; Snapshot annotations
hold compact CVE sets/provenance and are checked against the Kubernetes size cap.
Tekton results carry counts only. Retain/export logs before cluster pruning.

- `fixed_in_candidate`: baseline CVEs absent from the candidate's complete ACS
  inventory, across all comparable platforms. This is an **observed removal**,
  not independent proof that a package fix is installed.
- `new_in_candidate`: candidate-only CVEs; `platform_diffs` gives platform detail.
- `fixedBy`: an **available** fix version, not necessarily an installed version.
  Findings are not discarded just because this field is populated.

Before preparing advisory content, validate installed package versions/RHSAs,
product/VEX applicability and open tracker coverage separately. The published
3.5.4 CPU control detected all 12 contemporaneous Catalog CVEs on each platform;
that does not validate a different candidate, all future feed coverage or a release.

## Validation before rollout

Repeat the same-image four-platform control (zero delta and current Catalog
coverage), then a genuine older/newer tagged pair. Verify the intended service
account in-cluster. Local mock checks are not substitutes for those controls.
Publishing the branch, creating PipelineRuns, changing ReleasePlans and releasing
images are separate actions requiring authorization. There is no Clair fallback.
