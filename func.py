import io
import json
import logging
import time

from fdk import response

from ocir_image_update import (
    ConfigError,
    ManifestUpdateError,
    UnsupportedEventError,
    emit_log,
    process_event,
    summarize_event_payload,
)


logging.basicConfig(level=logging.INFO, format="%(message)s")
logging.getLogger().setLevel(logging.INFO)


def handler(ctx, data: io.BytesIO = None):
    started_at = time.time()
    raw_body = data.getvalue() if data is not None else b"{}"
    invocation_fields = {"body_bytes": len(raw_body)}

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
        invocation_fields.update(summarize_event_payload(payload))
        emit_log(logging.INFO, "invocation.started", **invocation_fields)
        result = process_event(payload)
        status_code = 200 if result["status"] != "ignored" else 202
    except UnsupportedEventError as exc:
        status_code = 202
        result = {"status": "ignored", "reason": str(exc)}
        emit_log(
            logging.INFO,
            "invocation.ignored",
            duration_ms=int((time.time() - started_at) * 1000),
            error_type=type(exc).__name__,
            reason=str(exc),
            **invocation_fields,
        )
    except (ConfigError, ManifestUpdateError, ValueError) as exc:
        status_code = 500
        result = {"status": "error", "reason": str(exc)}
        emit_log(
            logging.ERROR,
            "invocation.failed",
            duration_ms=int((time.time() - started_at) * 1000),
            error_type=type(exc).__name__,
            reason=str(exc),
            **invocation_fields,
        )
        logging.exception("Configuration or manifest update failure")
    except Exception as exc:  # pragma: no cover
        status_code = 500
        result = {"status": "error", "reason": f"Unhandled error: {exc}"}
        emit_log(
            logging.ERROR,
            "invocation.failed",
            duration_ms=int((time.time() - started_at) * 1000),
            error_type=type(exc).__name__,
            reason=str(exc),
            **invocation_fields,
        )
        logging.exception("Unhandled failure while processing event")
    else:
        emit_log(
            logging.INFO,
            "invocation.completed",
            duration_ms=int((time.time() - started_at) * 1000),
            result_status=result.get("status"),
            status_code=status_code,
            updated_file_count=len(result.get("updated_files", [])),
            **invocation_fields,
        )

    return response.Response(
        ctx,
        response_data=json.dumps(result, sort_keys=True),
        headers={"Content-Type": "application/json"},
        status_code=status_code,
    )
