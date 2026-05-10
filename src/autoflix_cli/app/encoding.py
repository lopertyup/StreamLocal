import base64
import json
from typing import Any, Dict


def encode_payload(payload: Dict[str, Any]) -> str:
    """Encode a JSON payload into a URL-safe token without padding."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_payload(token: str) -> Dict[str, Any]:
    """Decode a token created by encode_payload."""
    padding = "=" * (-len(token) % 4)
    raw = base64.urlsafe_b64decode((token + padding).encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Encoded payload is not an object.")
    return data


def make_content_key(provider_id: str, content_id: str) -> str:
    return f"{provider_id}:{content_id}"

