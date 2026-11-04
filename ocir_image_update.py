from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

OCIR_PUSH_EVENT_TYPE = "com.oraclecloud.artifacts.uploaddockerimage"
DEFAULT_OCIR_NAMESPACE = "ax1dvc8vmenm"
DEFAULT_OCIR_CLEANUP_RETAIN_COUNT = 3

LOG = logging.getLogger(__name__)


class ConfigError(ValueError):
    pass


class UnsupportedEventError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImagePushEvent:
    compartment_id: str | None
    repository_path: str
    resource_name: str
    tag: str
    digest: str | None


@dataclass(frozen=True)
class CleanupSettings:
    retain_count: int


@dataclass(frozen=True)
class RepositoryImage:
    image_id: str
    digest: str | None
    display_names: tuple[str, ...]
    time_created: datetime | None
    versions: tuple[str, ...]


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


def parse_ocir_push_event(payload: dict[str, Any]) -> ImagePushEvent:
    event_type = payload.get("eventType")
    if event_type != OCIR_PUSH_EVENT_TYPE:
        raise UnsupportedEventError(f"Unsupported OCI event type: {event_type}")

    data = payload.get("data") or {}
    details = data.get("additionalDetails") or {}
    compartment_id = data.get("compartmentId")
    repository_path = details.get("path")
    resource_name = data.get("resourceName")
    digest = details.get("digest")

    if not repository_path:
        raise ValueError("The event payload does not contain data.additionalDetails.path")
    if not resource_name:
        raise ValueError("The event payload does not contain data.resourceName")

    return ImagePushEvent(
        compartment_id=compartment_id,
        repository_path=repository_path,
        resource_name=resource_name,
        tag=parse_tag_from_resource_name(resource_name),
        digest=digest,
    )


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
    namespace = os.getenv("OCIR_NAMESPACE", DEFAULT_OCIR_NAMESPACE).strip("/")
    repository = strip_ocir_namespace(event.repository_path, namespace)
    cleanup_result = cleanup_pushed_repository_images(event, repository)
    if cleanup_result is None:
        cleanup_result = {"status": "disabled"}
    return {
        "status": cleanup_result["status"],
        "repository_path": event.repository_path,
        "ocir_repository": repository,
        "tag": event.tag,
        "image_cleanup": cleanup_result,
    }


def parse_tag_from_resource_name(resource_name: str) -> str:
    if "@" in resource_name:
        raise ValueError(f"Expected tag-based resource name but received digest reference: {resource_name}")

    last_colon = resource_name.rfind(":")
    if last_colon == -1:
        raise ValueError(f"Could not parse image tag from resource name: {resource_name}")

    return resource_name[last_colon + 1 :]


def strip_ocir_namespace(repository_path: str, ocir_namespace: str) -> str:
    normalized_path = repository_path.strip("/")
    if not ocir_namespace:
        return normalized_path

    prefix = f"{ocir_namespace}/"
    if normalized_path.startswith(prefix):
        return normalized_path[len(prefix) :]
    return normalized_path


def load_cleanup_settings() -> CleanupSettings | None:
    raw_value = os.getenv("OCIR_CLEANUP_RETAIN_COUNT", str(DEFAULT_OCIR_CLEANUP_RETAIN_COUNT)).strip()
    if not raw_value:
        return None

    retain_count = int(raw_value)
    if retain_count < 0:
        raise ConfigError("OCIR_CLEANUP_RETAIN_COUNT must be zero or a positive integer")
    if retain_count == 0:
        return None

    return CleanupSettings(retain_count=retain_count)


def cleanup_pushed_repository_images(
    event: ImagePushEvent,
    ocir_repository: str,
) -> dict[str, Any] | None:
    settings = load_cleanup_settings()
    if settings is None:
        emit_log(logging.INFO, "ocir.cleanup_disabled")
        return None

    if not event.compartment_id:
        emit_log(logging.WARNING, "ocir.cleanup_skipped", reason="missing_compartment_id")
        return {
            "status": "skipped",
            "reason": "OCI event payload does not contain data.compartmentId",
            "retain_count": settings.retain_count,
        }

    if not event.digest:
        emit_log(
            logging.WARNING,
            "ocir.cleanup_skipped",
            reason="missing_digest",
            repository_path=event.repository_path,
        )
        return {
            "status": "skipped",
            "reason": "OCI event payload does not contain data.additionalDetails.digest",
            "retain_count": settings.retain_count,
        }

    client = create_artifacts_client()
    repository = find_container_repository(client, event.compartment_id, event.repository_path, ocir_repository)
    if repository is None:
        emit_log(
            logging.WARNING,
            "ocir.cleanup_skipped",
            compartment_id=event.compartment_id,
            ocir_repository=ocir_repository,
            reason="repository_not_found",
            repository_path=event.repository_path,
        )
        return {
            "status": "skipped",
            "reason": "No matching OCIR repository found",
            "retain_count": settings.retain_count,
        }

    images = list_repository_images(client, event.compartment_id, repository.id)
    if not images:
        emit_log(
            logging.INFO,
            "ocir.cleanup_noop",
            ocir_repository=ocir_repository,
            reason="repository_has_no_images",
            retain_count=settings.retain_count,
        )
        return {
            "deleted_images": [],
            "retain_count": settings.retain_count,
            "repository_id": repository.id,
            "status": "noop",
        }

    protected_digests = {event.digest} if event.digest else None
    images_to_delete = select_images_to_delete(images, settings.retain_count, protected_digests)
    if not images_to_delete:
        emit_log(
            logging.INFO,
            "ocir.cleanup_noop",
            current_digest=event.digest,
            ocir_repository=ocir_repository,
            retain_count=settings.retain_count,
            unique_image_count=len(images),
        )
        return {
            "deleted_images": [],
            "retain_count": settings.retain_count,
            "repository_id": repository.id,
            "status": "noop",
        }

    deleted_images: list[dict[str, Any]] = []
    for image in images_to_delete:
        client.delete_container_image(image.image_id)
        deleted_images.append(repository_image_to_summary(image))
        emit_log(
            logging.INFO,
            "ocir.image_deleted",
            digest=image.digest,
            image_id=image.image_id,
            ocir_repository=ocir_repository,
            versions=list(image.versions),
        )

    emit_log(
        logging.INFO,
        "ocir.cleanup_complete",
        deleted_count=len(deleted_images),
        ocir_repository=ocir_repository,
        retain_count=settings.retain_count,
        unique_image_count=len(images),
    )
    return {
        "deleted_images": deleted_images,
        "deleted_count": len(deleted_images),
        "retain_count": settings.retain_count,
        "repository_id": repository.id,
        "status": "deleted",
    }


def repository_image_to_summary(image: RepositoryImage) -> dict[str, Any]:
    return {
        "digest": image.digest,
        "display_names": list(image.display_names),
        "id": image.image_id,
        "time_created": image.time_created.isoformat() if image.time_created else None,
        "versions": list(image.versions),
    }


def create_artifacts_client() -> Any:
    try:
        import oci
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("OCI Python SDK is required for OCIR cleanup") from exc

    signer = oci.auth.signers.get_resource_principals_signer()
    region = os.getenv("OCI_RESOURCE_PRINCIPAL_REGION")
    client_config = {"region": region} if region else {}
    return oci.artifacts.ArtifactsClient(client_config, signer=signer)


def find_container_repository(
    client: Any,
    compartment_id: str,
    repository_path: str,
    ocir_repository: str,
) -> Any | None:
    candidates: list[str] = []
    for name in (ocir_repository.strip("/"), repository_path.strip("/")):
        if name and name not in candidates:
            candidates.append(name)

    for candidate in candidates:
        repositories = list_all_results(
            client.list_container_repositories,
            compartment_id=compartment_id,
            display_name=candidate,
            limit=1000,
        )
        for repository in repositories:
            if getattr(repository, "display_name", None) == candidate:
                return repository

    return None


def list_repository_images(client: Any, compartment_id: str, repository_id: str) -> list[RepositoryImage]:
    raw_images = list_all_results(
        client.list_container_images,
        compartment_id=compartment_id,
        repository_id=repository_id,
        limit=1000,
    )
    return summarize_repository_images(raw_images)


def list_all_results(list_call: Any, **kwargs: Any) -> list[Any]:
    items: list[Any] = []
    page: str | None = None

    while True:
        call_kwargs = dict(kwargs)
        if page:
            call_kwargs["page"] = page
        response = list_call(**call_kwargs)
        items.extend(extract_response_items(response.data))
        page = response.headers.get("opc-next-page")
        if not page:
            return items


def extract_response_items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data

    items = getattr(data, "items", None)
    if items is not None:
        return list(items)

    raise ConfigError("Unexpected OCI list response shape while reading container registry data")


def summarize_repository_images(raw_images: list[Any]) -> list[RepositoryImage]:
    grouped: dict[str, dict[str, Any]] = {}

    for image in raw_images:
        image_id = getattr(image, "id", None)
        if not image_id:
            continue

        existing = grouped.setdefault(
            image_id,
            {
                "digest": getattr(image, "digest", None),
                "display_names": set(),
                "image_id": image_id,
                "time_created": getattr(image, "time_created", None),
                "versions": set(),
            },
        )

        display_name = getattr(image, "display_name", None)
        if display_name:
            existing["display_names"].add(display_name)

        version = getattr(image, "version", None)
        if version:
            existing["versions"].add(version)

        time_created = getattr(image, "time_created", None)
        if existing["time_created"] is None or (time_created and time_created > existing["time_created"]):
            existing["time_created"] = time_created

    images = [
        RepositoryImage(
            image_id=entry["image_id"],
            digest=entry["digest"],
            display_names=tuple(sorted(entry["display_names"])),
            time_created=entry["time_created"],
            versions=tuple(sorted(entry["versions"])),
        )
        for entry in grouped.values()
    ]
    return sorted(images, key=repository_image_sort_key, reverse=True)


def repository_image_sort_key(image: RepositoryImage) -> datetime:
    return image.time_created or datetime.min.replace(tzinfo=timezone.utc)


def select_images_to_delete(
    images: list[RepositoryImage],
    retain_count: int,
    protected_digests: set[str] | None = None,
) -> list[RepositoryImage]:
    keep_ids = {image.image_id for image in images[:retain_count]}
    for image in images:
        if protected_digests and image.digest in protected_digests:
            keep_ids.add(image.image_id)

    return [image for image in images if image.image_id not in keep_ids]
