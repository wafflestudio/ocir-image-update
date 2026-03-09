from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import quote

import jwt
import requests


GITHUB_API_VERSION = "2022-11-28"
OCIR_PUSH_EVENT_TYPE = "com.oraclecloud.artifacts.uploaddockerimage"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_GITHUB_OWNER = "wafflestudio"
DEFAULT_GITHUB_REPO = "waffle-world-oci"
DEFAULT_GITHUB_BRANCH = "main"
DEFAULT_GITHUB_COMMIT_MESSAGE = "build: update {repository_path} to {tag}"
DEFAULT_GITHUB_APP_PRIVATE_KEY_SECRET_OCID = (
    "ocid1.vaultsecret.oc1.ap-chuncheon-1.amaaaaaat2m5lbqa2sn77mucconq5hgglwa7gflf6fx5rbt5lh3jbnrqavtq"
)

LOG = logging.getLogger(__name__)


class ConfigError(ValueError):
    pass


class ManifestUpdateError(RuntimeError):
    pass


class UnsupportedEventError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImagePushEvent:
    repository_path: str
    resource_name: str
    tag: str
    digest: str | None


@dataclass(frozen=True)
class GitHubConfig:
    token: str
    owner: str
    repo: str
    api_url: str
    branch: str
    commit_name: str
    commit_email: str
    commit_message_template: str
    timeout_seconds: int


@dataclass(frozen=True)
class UpdateTarget:
    ocir_repository: str
    image_repository: str
    manifest_directory: str


def emit_log(level: int, event: str, **fields: Any) -> None:
    record = {"event": event}
    for key, value in fields.items():
        if value is not None:
            record[key] = value
    LOG.log(level, json.dumps(record, sort_keys=True, default=str))


def summarize_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data") or {}
    details = data.get("additionalDetails") or {}
    return {
        "event_id": payload.get("eventID") or payload.get("eventId") or payload.get("id"),
        "event_time": payload.get("eventTime"),
        "event_type": payload.get("eventType"),
        "repository_path": details.get("path"),
        "resource_name": data.get("resourceName"),
        "source": payload.get("source"),
    }


def process_event(payload: dict[str, Any]) -> dict[str, Any]:
    event = parse_ocir_push_event(payload)
    emit_log(
        logging.INFO,
        "event.accepted",
        digest=event.digest,
        repository_path=event.repository_path,
        resource_name=event.resource_name,
        tag=event.tag,
    )
    github_config = load_github_config()
    target = load_update_target(event)
    client = GitHubContentsClient(github_config)
    emit_log(
        logging.INFO,
        "manifest.target_resolved",
        branch=github_config.branch,
        image_repository=target.image_repository,
        manifest_directory=target.manifest_directory,
        ocir_repository=target.ocir_repository,
    )

    try:
        file_paths = client.list_yaml_files(target.manifest_directory)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        emit_log(
            logging.INFO,
            "manifest.directory_missing",
            branch=github_config.branch,
            manifest_directory=target.manifest_directory,
            repository_path=event.repository_path,
        )
        return {
            "status": "ignored",
            "branch": github_config.branch,
            "manifest_directory": target.manifest_directory,
            "repository_path": event.repository_path,
            "reason": "No matching manifest directory",
        }

    emit_log(
        logging.INFO,
        "manifest.files_listed",
        branch=github_config.branch,
        file_count=len(file_paths),
        manifest_directory=target.manifest_directory,
    )
    updated_files: list[dict[str, str]] = []

    for file_path in file_paths:
        updated = update_manifest_file(
            client=client,
            file_path=file_path,
            image_repository=target.image_repository,
            tag=event.tag,
            commit_message=github_config.commit_message_template.format(
                repository_path=target.ocir_repository,
                tag=event.tag,
                digest=event.digest or "",
                image_repository=target.image_repository,
                manifest_path=file_path,
            ),
        )
        if updated is not None:
            updated_files.append(updated)

    result = {
        "status": "updated" if updated_files else "noop",
        "branch": github_config.branch,
        "manifest_directory": target.manifest_directory,
        "repository_path": event.repository_path,
        "ocir_repository": target.ocir_repository,
        "image_repository": target.image_repository,
        "tag": event.tag,
        "updated_files": updated_files,
    }
    emit_log(
        logging.INFO,
        "manifest.update_complete",
        branch=result["branch"],
        manifest_directory=result["manifest_directory"],
        repository_path=result["repository_path"],
        status=result["status"],
        tag=result["tag"],
        updated_file_count=len(updated_files),
    )
    return result


def update_manifest_file(
    client: "GitHubContentsClient",
    file_path: str,
    image_repository: str,
    tag: str,
    commit_message: str,
) -> dict[str, str] | None:
    current_content, current_sha = client.get_file(file_path)
    updated_content, replacements = replace_image_tag_in_text(current_content, image_repository, tag)
    if replacements == 0:
        emit_log(
            logging.INFO,
            "manifest.file_skipped",
            image_repository=image_repository,
            path=file_path,
            reason="image_reference_not_found",
        )
        return None

    try:
        commit_sha = client.update_file(file_path, current_sha, updated_content, commit_message)
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 409:
            raise

        emit_log(logging.WARNING, "manifest.file_conflict_retry", path=file_path)
        current_content, current_sha = client.get_file(file_path)
        updated_content, replacements = replace_image_tag_in_text(current_content, image_repository, tag)
        if replacements == 0:
            emit_log(
                logging.INFO,
                "manifest.file_skipped",
                image_repository=image_repository,
                path=file_path,
                reason="image_reference_not_found_after_retry",
            )
            return None
        commit_sha = client.update_file(file_path, current_sha, updated_content, commit_message)

    emit_log(
        logging.INFO,
        "manifest.file_updated",
        commit_sha=commit_sha,
        path=file_path,
        replacements=replacements,
    )
    return {"path": file_path, "commit_sha": commit_sha}


def parse_ocir_push_event(payload: dict[str, Any]) -> ImagePushEvent:
    event_type = payload.get("eventType")
    if event_type != OCIR_PUSH_EVENT_TYPE:
        raise UnsupportedEventError(f"Unsupported OCI event type: {event_type}")

    data = payload.get("data") or {}
    details = data.get("additionalDetails") or {}
    repository_path = details.get("path")
    resource_name = data.get("resourceName")
    digest = details.get("digest")

    if not repository_path:
        raise ValueError("The event payload does not contain data.additionalDetails.path")
    if not resource_name:
        raise ValueError("The event payload does not contain data.resourceName")

    return ImagePushEvent(
        repository_path=repository_path,
        resource_name=resource_name,
        tag=parse_tag_from_resource_name(resource_name),
        digest=digest,
    )


def parse_tag_from_resource_name(resource_name: str) -> str:
    if "@" in resource_name:
        raise ValueError(f"Expected tag-based resource name but received digest reference: {resource_name}")

    last_colon = resource_name.rfind(":")
    if last_colon == -1:
        raise ValueError(f"Could not parse image tag from resource name: {resource_name}")

    return resource_name[last_colon + 1 :]


def load_github_config() -> GitHubConfig:
    owner = os.getenv("GITHUB_OWNER", DEFAULT_GITHUB_OWNER)
    repo = os.getenv("GITHUB_REPO", DEFAULT_GITHUB_REPO)
    api_url = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    branch = os.getenv("GITHUB_BRANCH", DEFAULT_GITHUB_BRANCH)
    commit_name = os.getenv("GITHUB_COMMIT_NAME", "ocir-image-updater")
    commit_email = os.getenv("GITHUB_COMMIT_EMAIL", "ocir-image-updater@example.com")
    commit_message_template = os.getenv("GITHUB_COMMIT_MESSAGE", DEFAULT_GITHUB_COMMIT_MESSAGE)
    timeout_seconds = int(os.getenv("HTTP_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))

    token = resolve_config_value("GITHUB_TOKEN")
    auth_mode = "token"
    if not token:
        token = generate_github_app_installation_token(owner, repo, api_url, timeout_seconds)
        auth_mode = "github_app"

    emit_log(
        logging.INFO,
        "github.config_loaded",
        api_url=api_url,
        auth_mode=auth_mode,
        branch=branch,
        owner=owner,
        repo=repo,
        timeout_seconds=timeout_seconds,
    )

    return GitHubConfig(
        token=token,
        owner=owner,
        repo=repo,
        api_url=api_url,
        branch=branch,
        commit_name=commit_name,
        commit_email=commit_email,
        commit_message_template=commit_message_template,
        timeout_seconds=timeout_seconds,
    )


def load_update_target(event: ImagePushEvent) -> UpdateTarget:
    manifest_scan_root = required_env("MANIFEST_SCAN_ROOT").strip("/")
    ocir_registry = required_env("OCIR_REGISTRY").rstrip("/")
    ocir_namespace = os.getenv("OCIR_NAMESPACE", "").strip("/")

    ocir_repository = strip_ocir_namespace(event.repository_path, ocir_namespace)
    manifest_directory = derive_manifest_directory(ocir_repository, manifest_scan_root)
    image_repository = build_image_repository(ocir_registry, event.repository_path, ocir_namespace)

    return UpdateTarget(
        ocir_repository=ocir_repository,
        image_repository=image_repository,
        manifest_directory=manifest_directory,
    )


def generate_github_app_installation_token(
    owner: str,
    repo: str,
    api_url: str,
    timeout_seconds: int,
) -> str:
    app_id = resolve_config_value("GITHUB_APP_ID")
    if not app_id:
        raise ConfigError("Set GITHUB_TOKEN or GITHUB_APP_ID with a private key")

    private_key = load_github_app_private_key()
    jwt_token = build_github_app_jwt(app_id, private_key)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {jwt_token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": "ocir-image-update-function",
    }

    installation_response = requests.get(
        f"{api_url}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation",
        headers=headers,
        timeout=timeout_seconds,
    )
    GitHubContentsClient.raise_for_status(
        installation_response,
        f"Unable to resolve GitHub App installation for {owner}/{repo}",
    )
    installation_id = installation_response.json()["id"]
    emit_log(
        logging.INFO,
        "github.app_installation_resolved",
        installation_id=installation_id,
        owner=owner,
        repo=repo,
    )

    token_response = requests.post(
        f"{api_url}/app/installations/{installation_id}/access_tokens",
        headers=headers,
        json={"repositories": [repo], "permissions": {"contents": "write"}},
        timeout=timeout_seconds,
    )
    GitHubContentsClient.raise_for_status(
        token_response,
        f"Unable to create GitHub App installation token for {owner}/{repo}",
    )
    emit_log(logging.INFO, "github.app_token_created", owner=owner, repo=repo)
    return token_response.json()["token"]


def load_github_app_private_key() -> str:
    private_key = resolve_config_value("GITHUB_APP_PRIVATE_KEY")
    if private_key:
        return private_key.replace("\\n", "\n").strip()

    private_key_base64 = os.getenv("GITHUB_APP_PRIVATE_KEY_BASE64")
    if private_key_base64:
        return base64.b64decode(private_key_base64).decode("utf-8").strip()

    raise ConfigError("Missing GitHub App private key: set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_BASE64")


def build_github_app_jwt(app_id: str, private_key: str) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": app_id}
    return jwt.encode(payload, private_key, algorithm="RS256")


def resolve_config_value(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value

    secret_ocid = os.getenv(f"{name}_SECRET_OCID") or default_secret_ocid(name)
    if secret_ocid:
        return fetch_vault_secret(secret_ocid.strip())

    return None


def default_secret_ocid(name: str) -> str | None:
    if name == "GITHUB_APP_PRIVATE_KEY":
        return DEFAULT_GITHUB_APP_PRIVATE_KEY_SECRET_OCID
    return None


@lru_cache(maxsize=16)
def fetch_vault_secret(secret_ocid: str) -> str:
    try:
        import oci
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("OCI Python SDK is required for Vault-backed secrets") from exc

    signer = oci.auth.signers.get_resource_principals_signer()
    region = os.getenv("OCI_RESOURCE_PRINCIPAL_REGION")
    client_config = {"region": region} if region else {}
    client = oci.secrets.SecretsClient(client_config, signer=signer)

    try:
        bundle = client.get_secret_bundle(secret_ocid).data
        content = bundle.secret_bundle_content.content
    except Exception as exc:  # pragma: no cover
        raise ConfigError(f"Unable to read Vault secret {secret_ocid}: {exc}") from exc

    try:
        return base64.b64decode(content).decode("utf-8").strip()
    except Exception as exc:  # pragma: no cover
        raise ConfigError(f"Vault secret {secret_ocid} is not valid base64 text content") from exc


def strip_ocir_namespace(repository_path: str, ocir_namespace: str) -> str:
    normalized_path = repository_path.strip("/")
    if not ocir_namespace:
        return normalized_path

    prefix = f"{ocir_namespace}/"
    if normalized_path.startswith(prefix):
        return normalized_path[len(prefix) :]
    return normalized_path


def derive_manifest_directory(ocir_repository: str, manifest_scan_root: str) -> str:
    repository_directory = posixpath.dirname(ocir_repository)
    if repository_directory in ("", "."):
        return manifest_scan_root
    return posixpath.join(manifest_scan_root, repository_directory)


def build_image_repository(ocir_registry: str, repository_path: str, ocir_namespace: str) -> str:
    normalized_path = repository_path.strip("/")
    if ocir_namespace and normalized_path.startswith(f"{ocir_namespace}/"):
        return f"{ocir_registry}/{normalized_path}"
    if ocir_namespace:
        return f"{ocir_registry}/{ocir_namespace}/{normalized_path}"
    return f"{ocir_registry}/{normalized_path}"


def replace_image_tag_in_text(content: str, image_repository: str, new_tag: str) -> tuple[str, int]:
    pattern = re.compile(
        rf'(^\s*(?:-\s*)?image:\s*["\']?{re.escape(image_repository)}:)([^"\'\s#]+)(["\']?\s*(?:#.*)?$)',
        re.MULTILINE,
    )

    def replace_match(match: re.Match[str]) -> str:
        return f"{match.group(1)}{new_tag}{match.group(3)}"

    return pattern.subn(replace_match, content)


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


class GitHubContentsClient:
    def __init__(self, config: GitHubConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {config.token}",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": "ocir-image-update-function",
            }
        )

    def get_file(self, path: str) -> tuple[str, str]:
        response = self.session.get(
            self._contents_url(path),
            params={"ref": self.config.branch},
            timeout=self.config.timeout_seconds,
        )
        self.raise_for_status(response, f"Unable to fetch {path} from branch {self.config.branch}")

        body = response.json()
        encoded_content = body.get("content", "")
        if body.get("encoding") != "base64":
            raise ManifestUpdateError(
                f"Expected base64-encoded GitHub content for {path}, received {body.get('encoding')}"
            )

        return base64.b64decode(encoded_content).decode("utf-8"), body["sha"]

    def list_yaml_files(self, path: str) -> list[str]:
        response = self.session.get(
            self._contents_url(path),
            params={"ref": self.config.branch},
            timeout=self.config.timeout_seconds,
        )
        self.raise_for_status(response, f"Unable to list directory {path} from branch {self.config.branch}")

        body = response.json()
        if not isinstance(body, list):
            raise ManifestUpdateError(f"Expected {path} to be a GitHub directory listing")

        return [
            item["path"]
            for item in body
            if item.get("type") == "file" and item.get("name", "").endswith((".yaml", ".yml"))
        ]

    def update_file(self, path: str, sha: str, content: str, message: str) -> str:
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "sha": sha,
            "branch": self.config.branch,
            "committer": {
                "name": self.config.commit_name,
                "email": self.config.commit_email,
            },
            "author": {
                "name": self.config.commit_name,
                "email": self.config.commit_email,
            },
        }
        response = self.session.put(
            self._contents_url(path),
            json=payload,
            timeout=self.config.timeout_seconds,
        )
        self.raise_for_status(response, f"Unable to update {path} on branch {self.config.branch}")
        return response.json()["commit"]["sha"]

    def _contents_url(self, path: str) -> str:
        return (
            f"{self.config.api_url}/repos/"
            f"{quote(self.config.owner, safe='')}/"
            f"{quote(self.config.repo, safe='')}/contents/"
            f"{quote(path, safe='/')}"
        )

    @staticmethod
    def raise_for_status(response: requests.Response, message: str) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            details = ""
            try:
                details = response.json().get("message", "")
            except ValueError:
                details = response.text.strip()

            raise requests.HTTPError(
                f"{message}: HTTP {response.status_code} {details}".strip(),
                response=response,
            ) from exc
