import io
import json
import logging

from fdk import response

from ocir_image_update import ConfigError, ManifestUpdateError, UnsupportedEventError, process_event


logging.getLogger().setLevel(logging.INFO)


def handler(ctx, data: io.BytesIO = None):
    raw_body = data.getvalue() if data is not None else b"{}"

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
        result = process_event(payload)
        status_code = 200 if result["status"] != "ignored" else 202
    except UnsupportedEventError as exc:
        status_code = 202
        result = {"status": "ignored", "reason": str(exc)}
    except (ConfigError, ManifestUpdateError, ValueError) as exc:
        logging.exception("Configuration or manifest update failure")
        status_code = 500
        result = {"status": "error", "reason": str(exc)}
    except Exception as exc:  # pragma: no cover
        logging.exception("Unhandled failure while processing event")
        status_code = 500
        result = {"status": "error", "reason": f"Unhandled error: {exc}"}

    return response.Response(
        ctx,
        response_data=json.dumps(result, sort_keys=True),
        headers={"Content-Type": "application/json"},
        status_code=status_code,
    )
