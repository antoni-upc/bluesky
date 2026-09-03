"""Validated profile configuration and lossless-enough BlueSky scenario rendering."""

import hashlib
import json
import re
from pathlib import Path


SCHEMA_VERSION = "research-profiles-v1"
IMPLEMENTATIONS = {"OPENAP", "PYBADA3", "PYBADA4"}
PROFILE_SHAPES = {
    "baseline-recorder-free": ("OPENAP", "ISA", False),
    "baseline-recorder": ("OPENAP", "ISA", True),
    "meteo-recorder": ("OPENAP", "NWP", True),
    "pybada-recorder": ("PYBADA", "ISA", True),
    "combined-recorder": ("PYBADA", "NWP", True),
}
CREATE_RE = re.compile(
    r"^(?P<prefix>\s*[^>]+>\s*)(?P<command>CRE|CREATE)(?P<gap>\s+)(?P<args>[^#]*?)(?P<comment>\s*#.*)?$",
    re.IGNORECASE,
)
ROLE_RE = re.compile(r"^\{role:(?P<role>[A-Za-z][A-Za-z0-9_-]*)\}$")


class ProfileConfigError(ValueError):
    """Raised when a profile configuration cannot be used safely."""


def sha256_bytes(content):
    return hashlib.sha256(content).hexdigest()


def load_profile_config(path):
    path = Path(path)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileConfigError(f"Cannot read profile configuration {path}: {exc}") from exc
    validate_profile_config(config)
    return config


def validate_profile_config(config):
    if not isinstance(config, dict):
        raise ProfileConfigError("Profile configuration must be an object")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise ProfileConfigError(f"schema_version must be {SCHEMA_VERSION!r}")
    roles = config.get("aircraft_roles")
    if not isinstance(roles, dict) or not roles:
        raise ProfileConfigError("aircraft_roles must be a non-empty object")
    for role, mappings in roles.items():
        if not isinstance(role, str) or not role.strip():
            raise ProfileConfigError("Aircraft role names must be non-empty strings")
        if not isinstance(mappings, dict) or not mappings:
            raise ProfileConfigError(f"aircraft_roles.{role} must be a non-empty object")
        unknown = sorted(set(mappings) - IMPLEMENTATIONS)
        if unknown:
            raise ProfileConfigError(
                f"aircraft_roles.{role} has unknown implementations: {', '.join(unknown)}"
            )
        if any(not isinstance(value, str) or not value.strip()
               for value in mappings.values()):
            raise ProfileConfigError(
                f"aircraft_roles.{role} mappings must be non-empty strings"
            )
    profiles = config.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ProfileConfigError("profiles must be a non-empty object")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ProfileConfigError(f"profiles.{name} must be an object")
        performance = profile.get("performance")
        if not isinstance(performance, dict):
            raise ProfileConfigError(f"profiles.{name}.performance must be an object")
        implementation_key(profile)
        atmosphere = profile.get("atmosphere")
        recorder = profile.get("recorder")
        if not isinstance(atmosphere, dict):
            raise ProfileConfigError(f"profiles.{name}.atmosphere must be an object")
        atmosphere_provider = str(atmosphere.get("provider", "")).upper()
        if atmosphere_provider not in {"ISA", "ERA5", "GFS"}:
            raise ProfileConfigError(
                f"profiles.{name}.atmosphere.provider must be ISA, ERA5, or GFS"
            )
        if atmosphere_provider == "ERA5":
            region = atmosphere.get("region")
            levels = atmosphere.get("pressure_levels_hpa")
            if (not isinstance(region, str) or
                    not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", region)):
                raise ProfileConfigError(
                    f"profiles.{name}.atmosphere.region must be a safe lowercase label"
                )
            if (not isinstance(levels, list) or not levels or
                    any(isinstance(level, bool) or not isinstance(level, (int, float))
                        for level in levels) or len(set(levels)) != len(levels)):
                raise ProfileConfigError(
                    f"profiles.{name}.atmosphere.pressure_levels_hpa must be unique numbers"
                )
        if not isinstance(recorder, dict) or not isinstance(recorder.get("enabled"), bool):
            raise ProfileConfigError(
                f"profiles.{name}.recorder.enabled must be boolean"
            )
        if name in PROFILE_SHAPES:
            performance_class = str(performance.get("provider", "")).upper()
            atmosphere_class = "ISA" if atmosphere_provider == "ISA" else "NWP"
            actual = (performance_class, atmosphere_class, recorder["enabled"])
            if actual != PROFILE_SHAPES[name]:
                raise ProfileConfigError(
                    f"profiles.{name} requires {PROFILE_SHAPES[name]}, got {actual}"
                )
    return config


def implementation_key(profile):
    performance = profile.get("performance", {})
    provider = str(performance.get("provider", "")).upper()
    if provider == "OPENAP":
        return provider
    if provider == "PYBADA":
        family = str(performance.get("family", ""))
        if family in {"3", "4"}:
            return f"PYBADA{family}"
        raise ProfileConfigError("PYBADA performance requires family 3 or 4")
    raise ProfileConfigError("performance.provider must be OPENAP or PYBADA")


def render_scenario_text(source, roles, implementation):
    if implementation not in IMPLEMENTATIONS:
        raise ProfileConfigError(f"Unknown performance implementation {implementation!r}")
    rendered = []
    replacements = []
    for line_number, line in enumerate(source.splitlines(keepends=True), start=1):
        ending = "\n" if line.endswith("\n") else ""
        content = line[:-1] if ending else line
        match = CREATE_RE.match(content)
        if match is None:
            rendered.append(line)
            continue
        arguments = match.group("args").split(",")
        if len(arguments) < 2:
            raise ProfileConfigError(f"Line {line_number}: CRE requires an aircraft type")
        aircraft_type = arguments[1].strip()
        role_match = ROLE_RE.fullmatch(aircraft_type)
        if role_match is None:
            rendered.append(line)
            continue
        role = role_match.group("role")
        mapping = roles.get(role)
        if mapping is None:
            raise ProfileConfigError(
                f"Line {line_number}: aircraft role token {aircraft_type!r} is not configured"
            )
        target = mapping.get(implementation)
        if not target:
            raise ProfileConfigError(
                f"Line {line_number}: aircraft role {role!r} has no {implementation} mapping"
            )
        leading = arguments[1][:-len(arguments[1].lstrip())]
        trailing = arguments[1][len(arguments[1].rstrip()):]
        arguments[1] = f"{leading}{target}{trailing}"
        rendered.append(
            f"{match.group('prefix')}{match.group('command')}{match.group('gap')}"
            f"{','.join(arguments)}{match.group('comment') or ''}{ending}"
        )
        replacements.append({"line": line_number, "role": role, "aircraft_type": target})
    return "".join(rendered), replacements


def render_scenario(source_path, output_path, config, profile_name):
    source_path = Path(source_path)
    output_path = Path(output_path)
    try:
        source_bytes = source_path.read_bytes()
        source = source_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ProfileConfigError(f"Cannot read scenario {source_path}: {exc}") from exc
    try:
        profile = config["profiles"][profile_name]
    except KeyError as exc:
        raise ProfileConfigError(f"Unknown profile {profile_name!r}") from exc
    implementation = implementation_key(profile)
    text, replacements = render_scenario_text(
        source, config["aircraft_roles"], implementation
    )
    rendered_bytes = text.encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(rendered_bytes)
    return {
        "source": str(source_path.resolve()),
        "rendered": str(output_path.resolve()),
        "source_sha256": sha256_bytes(source_bytes),
        "rendered_sha256": sha256_bytes(rendered_bytes),
        "implementation": implementation,
        "replacements": replacements,
    }
