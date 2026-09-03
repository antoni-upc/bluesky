"""Deliberate compatibility policy for historical research sample schemas."""

SCHEMA_ORDER = (
    "samples-v7", "samples-v8", "samples-v9", "samples-v10"
)
SCHEMA_INDEX = {schema: index for index, schema in enumerate(SCHEMA_ORDER)}

ENVELOPE_SCHEMAS = SCHEMA_ORDER
HORIZONTAL_DYNAMICS_SCHEMAS = SCHEMA_ORDER[1:]
ENERGY_SCHEMAS = SCHEMA_ORDER[2:]
PROPULSION_LOAD_SCHEMAS = SCHEMA_ORDER[3:]


def compatible_schemas(minimum):
    """Return known schemas whose semantics contain ``minimum`` fields."""
    try:
        return SCHEMA_ORDER[SCHEMA_INDEX[minimum]:]
    except KeyError as exc:
        raise ValueError(f"Unknown minimum sample schema: {minimum!r}") from exc


def require_schema(metadata, minimum, errors, label="metadata"):
    """Append a precise compatibility error and return whether schema passed."""
    actual = metadata.get("schema_version")
    supported = compatible_schemas(minimum)
    if actual in supported:
        return True
    versions = "/".join(schema.removeprefix("samples-") for schema in supported)
    errors.append(
        f"{label} schema {actual!r} is not compatible; requires samples-{versions}"
    )
    return False
