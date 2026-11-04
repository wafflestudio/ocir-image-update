# OCIR Image Cleanup Function

OCI Container Registry의 이미지 push 이벤트
`com.oraclecloud.artifacts.uploaddockerimage`를 받아 오래된 이미지를 정리하는
Oracle Function입니다. 기존 manifest 이미지 태그 갱신 기능은 제거했습니다.
이미지 태그와 배포 설정은 별도 Kustomize 워크플로에서 관리합니다.

## 동작 방식

1. OCI Events가 OCIR push 이벤트를 함수로 전달합니다.
2. 이벤트의 repository 경로로 OCIR repository를 찾습니다.
3. 생성 시각 기준 최신 3개 unique image ID를 보존하고 나머지를 삭제합니다.
4. 이벤트의 digest는 별도로 보호하므로 보존 개수가 3개를 초과할 수 있습니다.

`compartmentId` 또는 `digest`가 없는 이벤트는 삭제를 건너뜁니다.
이벤트를 받는 모든 repository가 정리 대상이며 함수 자체 이미지도 포함됩니다.
현재 배포 중인 이미지나 Kustomize 설정은 조회하지 않습니다.

## 설정

환경 변수 또는 Function config로 변경할 수 있습니다.

```text
OCIR_NAMESPACE=ax1dvc8vmenm
OCIR_CLEANUP_RETAIN_COUNT=3
```

`OCIR_CLEANUP_RETAIN_COUNT=0`이면 이미지 정리를 끕니다.
GitHub App, Vault secret, manifest 경로와 관련된 기존 설정은 더 이상 사용하지 않습니다.

## 배포

기존 함수 이름과 이미지 repository는 유지합니다.
Custom Dockerfile의 FDK entrypoint를 사용합니다.

```bash
fn -v deploy --app <your-functions-app>
```

## OCI Events Rule

```json
{
  "eventType": "com.oraclecloud.artifacts.uploaddockerimage"
}
```

## IAM

함수는 resource principal로 OCIR에 접근합니다.
Dynamic group에 다음 정책이 필요합니다.

```text
Allow dynamic-group <functions-dynamic-group> to manage repos in compartment <compartment-name>
```

이 함수의 GitHub App 인증과 Vault 접근은 더 이상 필요하지 않습니다.
기존 권한과 secret을 제거하기 전에는 다른 사용처가 있는지 확인하세요.

## 응답 및 로그

응답의 `status`는 `deleted`, `noop`, `skipped`, `disabled` 중 하나이며,
`image_cleanup`에 삭제 결과를 담습니다. 지원하지 않는 이벤트는 `ignored`입니다.

stdout/stderr에 한 줄 JSON 로그를 남깁니다. 주요 이벤트는
`invocation.started`, `event.accepted`, `ocir.image_deleted`,
`ocir.cleanup_complete`, `invocation.completed`입니다.

## 로컬 테스트

```bash
python3 -m unittest discover -s tests
```

또는 Linux의 Nix 환경에서 실행합니다.

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
OCI_AUTH_TOKEN=<members/waffle-deployer 사용자의 OCI Auth Token>
```

OCIR 로그인은 `ax1dvc8vmenm/members/waffle-deployer` 계정을 사용합니다.
`OCI_AUTH_TOKEN`에는 해당 계정의 OCI Auth Token을 등록해야 합니다.
GitHub App private key와 OCI API 서명용 PEM 키는 사용할 수 없습니다.
기존 `OCIR_USERNAME`, `OCIR_AUTH_TOKEN` secret은 더 이상 사용하지 않습니다.

이 workflow는 이미지를 OCIR에 push하는 단계까지만 수행합니다.
운영 Function에 적용하려면 해당 Function의 이미지 태그를 별도로 갱신해야 합니다.
