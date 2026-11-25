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
| `ibmcloud-konflux-tests` | `create-snc-cluster`, `delete-snc-cluster` | `api-key`, `region`, `cos-access-key`, `cos-secret-key`, `bucket` | yes | absent |
| `ocp-snc-credentials` | `create-snc-cluster`, `delete-snc-cluster` | `pull-secret` | yes | absent |
| `components-namespace-pull` | `verify-saw-disk-image` | `.dockerconfigjson` | yes | present |

Name the first two secrets in the pipeline params `ibmcloud-credentials-secret`
and `ocp-snc-credentials-secret` when the defaults do not fit. Name the third in
the task param `pull-secret-name`.

`ocp-snc-credentials` counts as required. mapt marks the volume optional, so
the pod starts, but the create step then exits with
`secret-ocp-snc-credentials must provide the key pull-secret`.

## Create the secrets

```bash
export KUBECONFIG=~/.config/kube/config
kubectl create secret generic ibmcloud-konflux-tests -n ai-incubator-tenant \
  --from-literal=api-key=<api key> \
  --from-literal=region=us-south \
  --from-literal=cos-access-key=<access key> \
  --from-literal=cos-secret-key=<secret key> \
  --from-literal=bucket=<bucket>
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
kubectl get secret ibmcloud-konflux-tests ocp-snc-credentials \
  components-namespace-pull -n ai-incubator-tenant
```

## Other prerequisites

| Item | Value |
| --- | --- |
| `ibmcloud-zone` pipeline param | for example `us-south-2`. mapt stops on create without it. |
| Catalog image | `openshift-local-<version>-x86-64` in the private catalog of the account. Select it with the `ocp-version` param. |
| mapt task | `tkn/infra-ibmcloud-ocp-snc.yaml` at commit `60acb4d878c2ec1089c1b22c8743c42672d0011b` |

## Limits

IBM Cloud does not support nested virtualization on a virtual server instance.
The `virtualization` profile therefore installs OpenShift Virtualization on a
machine that exposes no `devices.kubevirt.io/kvm` resource. The verify task
stops with a clear message until mapt selects a bare metal server
(redhat-developer/mapt#985).
