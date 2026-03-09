# OCIR Image Update Function

This Oracle Function listens for the OCI Container Registry image upload event
`com.oraclecloud.artifacts.uploaddockerimage` and updates matching Argo CD
manifests in `wafflestudio/waffle-world-oci` on the `main` branch.

## Flow

1. OCI Events routes the OCIR push event to this function.
2. The function reads `data.additionalDetails.path` and `data.resourceName`.
3. It maps `ax1dvc8vmenm/snutt-dev/snutt-ev` to:
   - image repository: `yny.ocir.io/ax1dvc8vmenm/snutt-dev/snutt-ev`
   - manifest scan root: `argocd`
4. It scans YAML files under that root recursively.
5. It replaces matching `image: ...:<old-tag>` lines.
6. It commits the changes directly to `wafflestudio/waffle-world-oci@main`.

This avoids coupling the updater to Argo CD directory names. If the same image
appears in multiple app or environment directories, every matching manifest
under `argocd/` is updated.

## Defaults

These values are built into the code:

```text
GITHUB_OWNER=wafflestudio
GITHUB_REPO=waffle-world-oci
GITHUB_BRANCH=main
GITHUB_COMMIT_MESSAGE=build: update {repository_path} to {tag}
MANIFEST_SCAN_ROOT=argocd
OCIR_REGISTRY=yny.ocir.io
OCIR_NAMESPACE=ax1dvc8vmenm
OCIR_CLEANUP_RETAIN_COUNT=3
```

## Required configuration

None. The current deployment uses built-in defaults for GitHub target,
manifest scan root, OCIR registry, OCIR namespace, cleanup retention count,
GitHub App ID, and the Vault secret OCID for the GitHub App private key.

Optional:

```text
GITHUB_OWNER=wafflestudio
GITHUB_REPO=waffle-world-oci
GITHUB_BRANCH=main
GITHUB_COMMIT_NAME=ocir-image-updater
GITHUB_COMMIT_EMAIL=ocir-image-updater@example.com
GITHUB_COMMIT_MESSAGE=build: update {repository_path} to {tag}
HTTP_TIMEOUT_SECONDS=10
GITHUB_APP_PRIVATE_KEY=<pem with \n escapes>
MANIFEST_SCAN_ROOT=argocd
OCIR_REGISTRY=yny.ocir.io
OCIR_NAMESPACE=ax1dvc8vmenm
OCIR_CLEANUP_RETAIN_COUNT=3
```

If both a plain env var and a `*_SECRET_OCID` are set, the plain env var wins.
If `GITHUB_APP_PRIVATE_KEY_SECRET_OCID` is omitted, the code defaults to:

```text
ocid1.vaultsecret.oc1.ap-chuncheon-1.amaaaaaat2m5lbqa2sn77mucconq5hgglwa7gflf6fx5rbt5lh3jbnrqavtq
```

The GitHub App ID is hardcoded in the function as `2842871`.

If `OCIR_CLEANUP_RETAIN_COUNT` is greater than zero, the function also lists
images in the pushed repository and deletes all but the latest N unique image
digests. The default is `3`. Set `OCIR_CLEANUP_RETAIN_COUNT=0` to disable this.

## Deploy

This project uses a custom `Dockerfile`, so `func.yaml` is set to
`runtime: docker` and the image's `ENTRYPOINT` is taken from the Dockerfile.

```bash
fn -v deploy --app <your-functions-app>
```

Recommended config for production:

```text
No function config is required for the current deployment.
```

Override only if you want to replace a built-in default such as the private key
secret OCID or cleanup retention count.

## Logging

The function emits one-line JSON application logs to stdout/stderr. Once
Function Invocation Logs are enabled in OCI Logging, you can filter by fields
such as `event`, `repository_path`, `tag`, `status_code`, and
`updated_file_count`.

Typical log events:

- `invocation.started`
- `event.accepted`
- `github.config_loaded`
- `manifest.file_updated`
- `manifest.update_complete`
- `invocation.completed`

## OCI Events rule

Broad rule:

```json
{
  "eventType": "com.oraclecloud.artifacts.uploaddockerimage"
}
```

The function filters by manifest path structure internally.

## Vault and IAM

The function reads Vault secrets using its resource principal.

Example policy:

```text
Allow dynamic-group <functions-dynamic-group> to read secret-family in compartment <compartment-name>
Allow dynamic-group <functions-dynamic-group> to manage repos in compartment <compartment-name>
```

If the secret uses a customer-managed key, the function may also need key usage
permissions.

## Local test

```bash
nix develop -c python -m unittest discover -s tests
```

## GitHub Actions CI/CD

The workflow at `.github/workflows/build-and-push-ocir.yml` builds this project
into a container image and pushes it to OCIR on every push to `main`.
It uses plain `docker build` on `ubuntu-latest`, so the output is a single
`linux/amd64` image rather than a multi-platform buildx manifest.

Default image repository:

```text
yny.ocir.io/ax1dvc8vmenm/ocir-image-update
```

Required repository secrets:

```text
OCIR_USERNAME=<docker login username for OCIR>
OCIR_AUTH_TOKEN=<OCI auth token>
```

The workflow pushes:

- `yny.ocir.io/ax1dvc8vmenm/ocir-image-update:<12-char git sha>`

## Reference docs

- Oracle Container Registry event types:
  https://docs.oracle.com/en-us/iaas/Content/Events/Reference/eventsproducers.htm
- Oracle Functions config parameters:
  https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionspassingconfigparams-about.htm
- GitHub App authentication:
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app
- GitHub Contents API:
  https://docs.github.com/en/rest/repos/contents
