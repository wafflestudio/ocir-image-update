# OCIR Image Update Function

OCI Container Registry의 이미지 push 이벤트
`com.oraclecloud.artifacts.uploaddockerimage`를 받아서,
`wafflestudio/waffle-world-oci`의 Argo CD manifest 이미지 태그를 갱신하는
Oracle Function입니다.

## 동작 방식

1. OCI Events가 OCIR push 이벤트를 이 함수로 전달합니다.
2. 함수는 이벤트의 `data.additionalDetails.path`와 `data.resourceName`을 읽습니다.
3. 예를 들어 `ax1dvc8vmenm/snutt-dev/snutt-ev`가 들어오면 대상 이미지는
   `yny.ocir.io/ax1dvc8vmenm/snutt-dev/snutt-ev`로 계산합니다.
4. GitHub code search로 `argocd` 아래에서 해당 이미지를 참조하는 YAML 후보를 찾고,
   search를 못 쓰는 경우에는 Git tree API 한 번으로 전체 YAML 목록을 가져옵니다.
5. 후보 파일을 읽어서 `image: ...:<old-tag>`를 새 태그로 치환합니다.
6. 변경된 파일만 `wafflestudio/waffle-world-oci@main`에 직접 커밋합니다.
7. 추가로, 같은 OCIR repository의 오래된 이미지를 정리해서 최신 N개만 남깁니다.

즉 디렉터리 이름 규칙에만 의존하지 않고, 실제 `image:` 값이 일치하는 manifest만
수정합니다. 같은 이미지가 여러 앱 디렉터리에서 재사용되면 모두 갱신됩니다.

## 기본값

아래 값들은 코드에 기본값으로 들어 있습니다.

```text
GITHUB_OWNER=wafflestudio
GITHUB_REPO=waffle-world-oci
GITHUB_BRANCH=main
GITHUB_COMMIT_MESSAGE=build: update {repository_path} to {tag}
GITHUB_APP_ID=2842871
GITHUB_APP_PRIVATE_KEY_SECRET_OCID=ocid1.vaultsecret.oc1.ap-chuncheon-1.amaaaaaat2m5lbqa2sn77mucconq5hgglwa7gflf6fx5rbt5lh3jbnrqavtq
MANIFEST_SCAN_ROOT=argocd
OCIR_REGISTRY=yny.ocir.io
OCIR_NAMESPACE=ax1dvc8vmenm
OCIR_CLEANUP_RETAIN_COUNT=3
```

현재 배포 기준으로는 function config를 별도로 넣지 않아도 동작하도록 맞춰져 있습니다.

## 선택 설정

필요하면 아래 값을 env/function config로 override할 수 있습니다.

```text
GITHUB_OWNER
GITHUB_REPO
GITHUB_BRANCH
GITHUB_COMMIT_NAME
GITHUB_COMMIT_EMAIL
GITHUB_COMMIT_MESSAGE
HTTP_TIMEOUT_SECONDS
GITHUB_APP_PRIVATE_KEY
GITHUB_APP_PRIVATE_KEY_SECRET_OCID
MANIFEST_SCAN_ROOT
OCIR_REGISTRY
OCIR_NAMESPACE
OCIR_CLEANUP_RETAIN_COUNT
```

주의:

- `GITHUB_APP_PRIVATE_KEY`와 `GITHUB_APP_PRIVATE_KEY_SECRET_OCID`가 둘 다 있으면 plain env가 우선합니다.
- `OCIR_CLEANUP_RETAIN_COUNT=0`이면 이미지 정리를 끕니다.

## 배포

이 프로젝트는 custom `Dockerfile`을 사용하므로 `func.yaml`은 `runtime: docker`
로 설정되어 있고, 실제 엔트리포인트는 Dockerfile의 `ENTRYPOINT`를 사용합니다.

```bash
fn -v deploy --app <your-functions-app>
```

## 로그

함수는 stdout/stderr로 한 줄 JSON 로그를 남깁니다. OCI Logging에서 Function
Invocation Logs를 켜면 다음 필드로 필터링할 수 있습니다.

- `event`
- `repository_path`
- `tag`
- `status`
- `updated_file_count`

주요 로그 이벤트:

- `invocation.started`
- `event.accepted`
- `github.config_loaded`
- `manifest.candidates_resolved`
- `manifest.file_updated`
- `manifest.update_complete`
- `ocir.cleanup_complete`
- `invocation.completed`

## OCI Events Rule

현재 함수는 broad rule을 전제로 동작합니다.

```json
{
  "eventType": "com.oraclecloud.artifacts.uploaddockerimage"
}
```

즉 OCIR push 이벤트 전체가 들어오고, 함수 내부에서 실제 처리 대상을 판단합니다.

## Vault / IAM

GitHub App private key는 OCI Vault에서 읽습니다. 함수는 resource principal로
접근하므로 dynamic group과 policy가 필요합니다.

예시 정책:

```text
Allow dynamic-group <functions-dynamic-group> to read secret-family in compartment <compartment-name>
Allow dynamic-group <functions-dynamic-group> to manage repos in compartment <compartment-name>
```

## 이미지 정리 정책

기본적으로 push된 repository에서 최신 `3`개의 unique image digest만 남기고,
그보다 오래된 이미지는 삭제합니다.

주의:

- manifest가 하나도 갱신되지 않아도 cleanup은 수행됩니다.
- 함수 자기 자신의 image push에도 cleanup이 동작할 수 있습니다.

## 로컬 테스트

```bash
nix develop -c python -m unittest discover -s tests
```

## GitHub Actions CI/CD

`.github/workflows/build-and-push-ocir.yml`은 `main` push 때 이 프로젝트 이미지를
빌드해서 OCIR로 올립니다.

- 빌드 방식: plain `docker build`
- 플랫폼: `linux/amd64`
- push tag: `<12-char git sha>`만 사용

기본 이미지 repository:

```text
yny.ocir.io/ax1dvc8vmenm/ocir-image-update
```

필요한 GitHub Actions secret:

```text
OCIR_USERNAME=<docker login username for OCIR>
OCIR_AUTH_TOKEN=<OCI auth token>
```

## 참고 문서

- Oracle Container Registry event types:
  https://docs.oracle.com/en-us/iaas/Content/Events/Reference/eventsproducers.htm
- Oracle Functions config parameters:
  https://docs.oracle.com/en-us/iaas/Content/Functions/Tasks/functionspassingconfigparams-about.htm
- GitHub App authentication:
  https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app
- GitHub Contents API:
  https://docs.github.com/en/rest/repos/contents
