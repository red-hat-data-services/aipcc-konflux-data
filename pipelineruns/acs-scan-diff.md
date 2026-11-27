# ACS CVE scan diff

Compare two digest-pinned images for one component. ACS deltas are evidence,
not verified remediation, ECP, or release approval. Findings do not gate a
release, but a failed run configured as a stage `finalPipeline` fails the stage
Release. There is no Clair fallback.

## Run it

1. Copy [`acs-scan-diff.yaml`](acs-scan-diff.yaml) and replace the placeholders.
   Set `pipelineRef.params.revision` and `task-revision` to the **same published
   full Git SHA**. This pins the pipeline and tasks, not Central's database.
   A stage `finalPipeline` instead resolves both the pipeline and its tasks
   from `main` and needs no `task-revision` param; `task-revision` accepts
   exactly `main` or a full lower-case SHA and defaults to `main`. Like any
   Git-resolved pipeline, a `main` run can resolve its pipeline and tasks
   across a `main` update. Use the same full SHA to reproduce the code used
   by a run.
2. Keep `aipcc-releaser-bot-sa` as the default for resolve and annotate. Override
   only `scan` with the ACS-authorized component account (e.g.
   `build-pipeline-base-image-cpu-3-5` in `ai-tenant`). Resolve uses
   `components-namespace-pull`; ACS pulls through Central's registry integration.
3. Choose inputs and a scan mode below. Submit the prepared run when authorized:

   ```sh
   oc create -f <PREPARED_PIPELINERUN.yaml>
   tkn pipelinerun logs -f -n ai-tenant <PIPELINERUN_NAME>
   ```

| Inputs | Baseline and candidate |
| --- | --- |
| `component`, `baseline-ref`, `candidate-ref` | Explicit `repo@sha256:digest` refs (tag plus digest is also accepted; tags alone are not). Omit `snapshot` to leave Snapshots unchanged. |
| `component`, `baseline-snapshot`, `snapshot` | Two distinct Snapshots from the same application. The resolver selects the exact component from each, even in grouped Snapshots. Verify the baseline against its archived `Released=True` Release and matching artifact digest first; the pipeline does not check publication or query the archive. |
| `release`, `releasePlan`, `snapshot` | These must identify the same candidate Release. The resolver picks the latest visible `Released=True` prod Release for its component. Set `prod-release-plan` if replacing `-stage-` with `-prod-` is wrong. Run before prod promotion or use explicit refs to avoid selecting the candidate as its own baseline. |

The resolver requires matching Linux platform sets (`amd64`, `arm64`, `ppc64le`,
`s390x`; single-platform pairs work too). It uses child configs to classify
unlabeled descriptors, skips explicitly marked BuildKit attestations and
non-Linux entries, and fails on unclassifiable entries, unsupported variants,
duplicate platforms, or inspection errors.

| `scan-mode` | ACS behavior | Snapshot annotation |
| --- | --- | --- |
| `fresh` (default) | Forced rescan per manifest, includes snoozed findings, requires a timestamp inside the request window with five minutes of clock-skew allowance. | Only with `snapshot`, after a successful comparison and artifact upload. |
| `build` | Non-forced scan, default retries, excludes snoozed findings. Cached responses with valid timestamps are accepted and labeled non-fresh. | Never. |

Both modes use a **30m client timeout per request**. The scan Task has 2h; the
example gives the PipelineRun 2h20m. Retries can exhaust the task budget and a
shorter Central or proxy deadline still applies. The scanner rejects wrong
manifest digests, incomplete scan data, and missing or empty package inventories.
Packages with no vulnerabilities are valid.

## Collect evidence

Use results only if the producing PipelineRun `Succeeded=True`. To extract the
per-platform comparison from live logs (use `oc ka logs` after pruning):

```sh
RUN='<PIPELINERUN_NAME>'
if [ "$(oc get pipelinerun "$RUN" -n ai-tenant -o jsonpath='{.status.conditions[?(@.type=="Succeeded")].status}')" = True ]; then
  POD=$(oc get taskrun -n ai-tenant -l "tekton.dev/pipelineRun=$RUN,tekton.dev/pipelineTask=scan" -o jsonpath='{.items[0].status.podName}') &&
    oc logs "$POD" -n ai-tenant -c step-compare > "$RUN.log" &&
    awk '/^===RESULT-BEGIN===$/{on=1;next}/^===RESULT-END===$/{on=0}on' "$RUN.log" > "$RUN.json" &&
    jq '{component, code_revision, fixed_in_candidate, new_in_candidate, platform_diffs}' "$RUN.json"
else
  echo "$RUN did not succeed; do not use its evidence" >&2
fi
```

- `fixed_in_candidate` means baseline CVEs absent from the candidate across all
  compared platforms. `new_in_candidate` means candidate-only CVEs. Central's
  live database is not frozen; scanner-version changes are flagged per platform.
- `code_revision` keeps its field name but records the requested Git ref: `main`
  for a default stage run (not the SHA it resolved to), or the pinned full SHA.
- Native `REPORT` log sections retain package, advisory, and `fixedBy` details.
  An available `fixedBy` version is not proof it is installed. Validate installed
  versions and applicability separately before using the diff in an advisory.
- The temporary Trusted Artifact contains only `fresh-scan-result.json`, never
  ACS auth or native reports. Tekton carries its digest-bound locator and counts
  rather than the full JSON. The existing `ociStorage` and
  `ociArtifactExpiresAfter` defaults use the check-labels repository and `1d`
  retention. Direct artifact steps use the existing registry credential and
  explicit mounts because this cluster rejects those overrides on StepActions.
- Annotation validates the artifact's provenance, component, digests, and CVE
  sets before patching `fresh-scan.aipcc.redhat.com/summary` and `/result`. It
  checks the Snapshot's candidate digest, resource version, and annotation size.
  Later successful runs can overwrite these keys; verify `pipeline_run` because
  failed runs leave older annotations in place.

## Before automatic rollout

Pilot the stage `finalPipeline` separately. Verify a same-image four-platform
control (zero delta and current Catalog coverage), a real older/newer pair, and
Snapshot annotation. Confirm the deployed controller preserves the release
account default and `finalPipeline.taskRunSpecs` scan-account override.
