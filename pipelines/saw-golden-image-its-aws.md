# SAW golden image integration test on AWS — cluster secrets

`pipelines/saw-golden-image-its-aws.yaml` runs the same contract check as
`pipelines/saw-golden-image-its-ibmcloud.yaml` and runs it on AWS. PMT renders
the `IntegrationTestScenario` from the product management configs into the
Konflux GitOps repository, ArgoCD applies it, and the integration service starts
the PipelineRun.

The PipelineRun reads three secrets from the tenant namespace. The tenant
supplies the secrets. This repository never holds a secret value.

## Required secrets

| Secret | Read by | Keys | Required | `ai-incubator-tenant` on 2026-11-26 |
| --- | --- | --- | --- | --- |
| `aws-ai-incubator` | `create-snc-cluster`, `delete-snc-cluster` | `access-key`, `secret-key`, `region`, `bucket` | yes | vault backed, merge request `konflux-release-data!25563` open |
| `ocp-snc-credentials` | `create-snc-cluster` | `pull-secret` | yes | vault backed, merge request `konflux-release-data!25563` open |
| `components-namespace-pull` | `verify-saw-disk-image` | `.dockerconfigjson` | yes | present |

The vault record of this tenant holds the four keys, and it names the secret in
its `konflux_secret_name` field. `ai-tenant` and `rhel-ai-tenant` read the older
record `apps/ai-aipcc-cicd/aws-konflux-tests`; this scenario reads the record of
its own account:

| Secret | Vault record |
| --- | --- |
| `aws-ai-incubator` | `apps/ai-aipcc-cicd/service/release-pipeline/ai-incubator/cloud-providers/aws` |
| `ocp-snc-credentials` | `apps/ai-aipcc-cicd/service/release-pipeline/ai-incubator/ocp/pullsecret` |

The record maps its secret keys to vault properties. A change to a vault value
reaches the cluster on the next refresh, one hour at most. The ExternalSecret
owns its Secret, so a `kubectl create secret` of the same name is overwritten at
the next refresh. Put a new value in vault, never in the cluster.

Name the first two secrets in the pipeline params `aws-credentials-secret` and
`ocp-snc-credentials-secret` when the defaults do not fit. Name the third in the
task param `pull-secret-name`.

`ocp-snc-credentials` counts as required. mapt marks the volume optional, so
the pod starts, but the create step then exits with
`secret-ocp-snc-credentials must provide the key pull-secret`.

## Create the secrets

`aws-ai-incubator` needs no command in a tenant that maps the vault record.
The manual form below applies only to a cluster with no ExternalSecret:

```bash
kubectl create secret generic aws-ai-incubator -n ai-incubator-tenant \
  --from-literal=access-key=<access key id> \
  --from-literal=secret-key=<secret access key> \
  --from-literal=region=<region> \
  --from-literal=bucket=<bucket>
```

`ocp-snc-credentials` is the secret of the IBM Cloud scenario too. Its
ExternalSecret reads the record `ocp/pullsecret` and copies the whole
dockerconfigjson into the key `pull-secret`. mapt mounts the key at
`/opt/ocp-snc-secret/pull-secret` and passes that path to the installer.

Its manual form:

```bash
export KUBECONFIG=~/.config/kube/config
kubectl create secret generic ocp-snc-credentials -n ai-incubator-tenant \
  --from-file=pull-secret=<path to the pull secret file>
```

`global-pull-secret` is not a substitute for `ocp-snc-credentials`. That secret
holds only the `registry.redhat.io` auth entry. An OpenShift install needs the
full pull secret from console.redhat.com.

## Not required for this scenario

- `ibmcloud-ai-incubator` — the scenario runs on AWS only. The IBM Cloud
  pipeline `pipelines/saw-golden-image-its-ibmcloud.yaml` reads it.
- `rhel-ai-activation-key`, `snyk-secret` — the build pipelines use them.

## Secrets the run creates

| Secret | Owner | Note |
| --- | --- | --- |
| `mapt-aws-ocp-snc-<random>` | `create-snc-cluster` | The name is the task result `cluster-access-secret`. Holds `host`, `username`, `id_rsa`, `kubeadmin_pass`, `kubeconfig`. |
| `saw-pull-secret` | `verify-saw-disk-image` | Built from the `PULL_SECRET_JSON` environment variable. CDI reads an opaque secret with the keys `accessKeyId` and `secretKey`, so the task takes the user and the password of the image registry from the pull secret. A dockerconfigjson secret fails the import with `couldn't find key accessKeyId in Secret`. A public image takes no secret. |

The service account `its-pipeline-saw-disk-image-container` belongs to the
component. The integration service creates it. It must allow `create` on
Secrets, because both tasks make a secret. It needs no secret of its own.

## Check the secrets

```bash
kubectl get secret aws-ai-incubator ocp-snc-credentials \
  components-namespace-pull -n ai-incubator-tenant
```

## Other prerequisites

| Item | Value |
| --- | --- |
| AMI | The mapt account publishes the SNC AMI with cloud-importer `openshift-local aws`. mapt reads the AMI `openshift-local-<ocp-version>-x86_64`, so `ocp-version` names a bundle version, for example `4.22.14`. It names no catalog image. A missing AMI stops the create with `AMI openshift-local-<ocp-version>-x86_64 could not be found in region: <region>`. `4.22.14` is the newest stable bundle; `5.0.0-rc.1` is a pre-release. List the bundles with `aws ec2 describe-images --owners 391597328979 --filters Name=name,Values='openshift-local-*'`. |
| Architecture | x86_64 only. The AWS SNC task takes `arch`, and the pipeline sets `x86_64`, so the scenario covers the x86_64 variant of the image. |
| Region | The region of the `aws-ai-incubator` secret. It must offer a bare metal type that matches the architecture. |
| Compute | The `virtualization` profile requests nested virtualization, so mapt selects a bare metal type. With that request mapt filters on the architecture and on the bare metal flag only, so the task defaults `cpus` and `memory` do not narrow the choice. Set `snc-instance-types` to pin the type and the price. mapt takes that list as it is, so name a bare metal type. A virtual type exposes no `/dev/kvm`, and the verify task then stops. |
| Spot | The pipeline passes `spot: false`. A metal instance takes no spot capacity, and the task default is `true`, so the value must be explicit. |
| mapt task | `tkn/infra-aws-ocp-snc.yaml` at the tag `v0.14.7`. That tag holds the `arch` param, the `nested-virt` flag and the profile list. The create and the destroy read the same revision, so a change on `main` cannot give the destroy a task that no longer matches the state the create wrote. |
| Tags | The `pipeline-run-url` tag holds the PipelineRun name, as on IBM Cloud. The run name identifies the run on the provider console, and it carries no character that the provider can reject. |

## Limits

AWS supplies the KVM device on bare metal instances only. The
`virtualization` profile requests nested virtualization, so mapt selects a bare
metal instance. These points follow from that choice:

- A bare metal instance costs more than a virtual instance. Check the price of
  the type before the run. The `finally` task destroys the cluster.
- The default `snc-disk-size` is 1000 GB, the mapt default. The root volume must
  hold the SNC installation and the guest disk.
- Create takes about 45 minutes, plus the installation of OpenShift
  Virtualization.

The verify task stops when no node exposes `devices.kubevirt.io/kvm`. It holds
that check as a guard, not as an expected result.
