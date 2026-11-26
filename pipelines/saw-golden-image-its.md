# SAW golden image integration test — cluster secrets

`pipelines/saw-golden-image-its.yaml` runs after a Release of
`saw-disk-image-container`. PMT renders the `IntegrationTestScenario` from the
product management configs into the Konflux GitOps repository, ArgoCD applies
it, and the integration service starts the PipelineRun. The scenario object in
`ai-incubator-tenant` carries the ArgoCD tracking annotation.

The PipelineRun reads three secrets from the tenant namespace. The tenant
supplies the secrets. This repository never holds a secret value.

## Required secrets

| Secret | Read by | Keys | Required | `ai-incubator-tenant` on 2026-11-25 |
| --- | --- | --- | --- | --- |
| `ibmcloud-ai-incubator` | `create-snc-cluster`, `delete-snc-cluster` | `api-key`, `region`, `cos-access-key`, `cos-secret-key`, `bucket` | yes | vault backed, merge request `konflux-release-data!25561` pending |
| `ocp-snc-credentials` | `create-snc-cluster` | `pull-secret` | yes | vault backed, merge request `konflux-release-data!25563` pending |
| `components-namespace-pull` | `verify-saw-disk-image` | `.dockerconfigjson` | yes | present |

`konflux-release-data` holds one ExternalSecret per secret:

| Secret | Vault path |
| --- | --- |
| `ibmcloud-ai-incubator` | `apps/ai-aipcc-cicd/service/release-pipeline/ai-incubator/cloud-providers/ibmcloud` |
| `ocp-snc-credentials` | `apps/ai-aipcc-cicd/service/release-pipeline/ai-incubator/ocp/pullsecret` |

Each record maps its secret keys to vault properties. A change to a vault value
reaches the cluster on the next refresh, one hour at most. The ExternalSecret
owns its Secret, so a `kubectl create secret` of the same name is overwritten at
the next refresh. Put a new value in vault, never in the cluster.

Name the first two secrets in the pipeline params `ibmcloud-credentials-secret`
and `ocp-snc-credentials-secret` when the defaults do not fit. Name the third in
the task param `pull-secret-name`.

`ocp-snc-credentials` counts as required. mapt marks the volume optional, so
the pod starts, but the create step then exits with
`secret-ocp-snc-credentials must provide the key pull-secret`.

## Create the secrets

`ibmcloud-ai-incubator` needs no command. The ExternalSecret creates it. The
manual form below applies only to a cluster with no ExternalSecret:

```bash
kubectl create secret generic ibmcloud-ai-incubator -n ai-incubator-tenant \
  --from-literal=api-key=<api key> \
  --from-literal=region=us-south \
  --from-literal=cos-access-key=<access key> \
  --from-literal=cos-secret-key=<secret key> \
  --from-literal=bucket=<bucket>
```

`ocp-snc-credentials` needs no command either. Its ExternalSecret reads the
record `ocp/pullsecret` and copies the whole dockerconfigjson into the key
`pull-secret`. mapt mounts the key at `/opt/ocp-snc-secret/pull-secret` and
passes that path to the installer.

Its manual form, for the same case:

```bash
export KUBECONFIG=~/.config/kube/config
kubectl create secret generic ocp-snc-credentials -n ai-incubator-tenant \
  --from-file=pull-secret=<path to the pull secret file>
```

`global-pull-secret` is not a substitute for `ocp-snc-credentials`. That secret
holds only the `registry.redhat.io` auth entry. An OpenShift install needs the
full pull secret from console.redhat.com.

## Not required for this scenario

- `aws-konflux-tests` — the scenario runs on IBM Cloud only.
- `rhel-ai-activation-key`, `snyk-secret` — the build pipelines use them.

## Secrets the run creates

| Secret | Owner | Note |
| --- | --- | --- |
| `mapt-ibmcloud-ocp-snc-<random>` | `create-snc-cluster` | The name is the task result `cluster-access-secret`. Holds `host`, `username`, `id_rsa`, `kubeadmin_pass`, `kubeconfig`. |
| `saw-pull-secret` | `verify-saw-disk-image` | Built from the `PULL_SECRET_JSON` environment variable. The CDI import reads it. |

The service account `its-pipeline-saw-disk-image-container` belongs to the
component. The integration service creates it. It must allow `create` on
Secrets, because both tasks make a secret. It needs no secret of its own.

## Check the secrets

```bash
kubectl get secret ibmcloud-ai-incubator ocp-snc-credentials \
  components-namespace-pull -n ai-incubator-tenant
```

## Other prerequisites

| Item | Value |
| --- | --- |
| IAM policies of the credential | The service ID needs `Viewer` on Catalog Management, because mapt reads the private catalog. It needs `Administrator` on all account management services, because mapt creates a resource group per run. It needs `Editor` on VPC Infrastructure for **all resource groups**, because the run creates its resources in the new group. A policy scoped to one group fails with `the provided token is not authorized to create vpcs in this account`. |
| `ibmcloud-zone` pipeline param | for example `us-south-2`. The zone must belong to the region of the credentials secret. mapt stops on create without it. |
| Catalog image | `openshift-local-<version>-x86-64` in the private catalog of the account. `ocp-version` defaults to `5.0.0`, which reads `openshift-local-5-0-0-x86-64`. An empty value selects the latest version of the private catalog. |
| Architecture | x86_64 only. The IBM Cloud SNC task sets the architecture itself, and IBM Cloud VPC offers no arm64 profile, so the scenario covers the x86_64 variant of the image. |
| VPC image | The account must hold a VPC image for the catalog offering version. A bare metal server takes an image ID and no catalog offering, so mapt resolves the offering to an image. Without that image the create stops with `no VPC image found for catalog offering`. |
| mapt task | `tkn/infra-ibmcloud-ocp-snc.yaml` on the `main` branch. No release tag holds the IBM Cloud task or the bare metal selection yet, so the pipeline reads `main`. Point the revision to a tag when mapt publishes one that holds both. |
| Compute | the `virtualization` profile requests nested virtualization, so mapt selects the smallest bare metal profile that holds `snc-cpus` and `snc-memory`. It checks the capacity of the zone before the create. |
| Tags | IBM Cloud VPC accepts the tag characters `A-Z a-z 0-9 : _ . -` only. The `pipeline-run-url` tag therefore holds the PipelineRun name and not the console URL. A URL carries `/`, and IBM Cloud VPC rejects the whole resource with `should match regexp ^[A-Za-z0-9:_ .-]+$`. |

## Limits

IBM Cloud supplies the KVM device on bare metal servers only. The
`virtualization` profile requests nested virtualization, so mapt selects a bare
metal server. These points follow from that choice:

- A bare metal server costs more than a virtual server instance. Check the
  capacity and the price before the run. The `finally` task destroys the
  cluster.
- The pipeline passes no spot request. A bare metal server takes no spot
  capacity, so the task default `false` stands and no operator can turn it on
  by mistake.
- mapt ignores `snc-disk-size`, because the server boots from its local disks.
- The account must hold a bare metal profile with at least `snc-cpus` cores and
  `snc-memory` GiB. Empty bare metal capacity in the zone stops the create with
  a clear message.

The verify task still stops when no node exposes `devices.kubevirt.io/kvm`. It
holds that check as a guard, not as an expected result.
