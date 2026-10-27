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
3. Keep `aipcc-releaser-bot-sa` as the default service account for resolution
   and annotation. Override **only `scan`** through `taskRunSpecs` with an
   existing ACS-authorized component build account. The CPU 3.5.4 control used
   `build-pipeline-base-image-cpu-3-5` in `ai-tenant`; other accounts still need
   validation. The release account need not authenticate to ACS, and the build
   account need not patch Snapshots. No rolebindings or ACS mappings are added.
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

The default manual mode does **not** mutate Snapshots or upload native reports
or image referrers. It does call Central with `--force --include-snoozed` and
publish a temporary Trusted Artifact containing **only `fresh-scan-result.json`**.
It reuses the check-labels flow and defaults to
`quay.io/aipcc-cicd/aipcc-konflux-trusted-artifacts`, with a unique TaskRun tag and
`1d` expiry (`ociStorage` / `ociArtifactExpiresAfter`). The expiry is registry
retention, not a guarantee of immediate deletion. The publisher archives the
single comparison file, never `/tekton/home` or its ACS authentication data.

Both artifact steps explicitly mount the existing
`quay-aipcc-cicd-konflux-access` credential read-only. `AUTHFILE` selects it
without depending on service-account credential merging; ORAS also receives the
explicit registry-config option. No credentials are exported to results or
artifacts, and no service-account links or permissions are changed. The steps use
`quay.io/konflux-ci/build-trusted-artifacts@sha256:9bd32f6bafb517b309e11a2d89365052b4ab3f1c9c23c4ffd45aff6f03960476`,
the image/arguments from catalog StepActions at commit
`5954499ad691f94e36e7b0cc86a1ca46672f81d8`. Direct Steps are used because this
cluster rejects environment and volume-mount overrides on StepAction refs.

Add a `snapshot` parameter only if you intentionally want annotations published.
The separate `annotate` task restores the digest-bound artifact to its own local
`emptyDir`, validates the full JSON's run/revision/component/image bindings and
CVE-set consistency, then patches the Snapshot with the release account. It runs
only after successful scanning and artifact publication. No shared workspace or
PVC is required: Tekton carries only the small `sourceDataArtifact` locator and
counts. A missing artifact, failed restore, missing/malformed JSON or inconsistent
evidence fails rather than publishing partial data.

The earlier 29,756-byte one-step results probe was not representative: the real
multi-step scan pod has less termination-message space per container and lost
its compressed results. The full JSON is therefore no longer a Tekton result.
The component's candidate digest must match the Snapshot. Annotation
checks resourceVersion to reject concurrent edits; successful runs are otherwise
last-writer-wins. Failed runs do not erase earlier evidence: check the producing run.

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
No stage ReleasePlan references this pipeline yet. Future finalPipeline wiring
must retain the release-account default and the scan-account override under
`finalPipeline.taskRunSpecs`; this is supported by the upstream release-service
API but must be validated against the deployed version before rollout.

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
oc logs "$POD" -n ai-tenant -c step-compare > "$RUN.log"
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
The scan Task's `sourceDataArtifact` result carries the digest-bound locator to
the annotation Task; count results and Pipeline results retain their contract.
Retain/export logs before cluster pruning.

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
