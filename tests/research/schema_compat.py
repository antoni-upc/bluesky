"""Exact sample-schema policy for the active research baseline."""

SCHEMA_VERSION = "samples-v12"


def require_schema(metadata, errors, label="metadata"):
    """Accept only the active schema and append a precise error otherwise."""
    actual = metadata.get("schema_version")
    if actual == SCHEMA_VERSION:
        return True
    errors.append(f"{label} schema {actual!r} is not {SCHEMA_VERSION}")
    return False
