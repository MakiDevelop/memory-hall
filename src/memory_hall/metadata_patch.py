from __future__ import annotations

from typing import Any

_AMH_PATCH_ALLOWED_KEYS = frozenset(
    {
        "amh_status",
        "amh_version",
        "revoked_by",
        "revoked_reason",
    }
)


def filter_amh_metadata_patch(metadata: dict[str, Any]) -> dict[str, Any]:
    """Allow only explicit AMH lifecycle keys on PATCH (integration:memhall-amh)."""
    return {key: value for key, value in metadata.items() if key in _AMH_PATCH_ALLOWED_KEYS}