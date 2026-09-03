"""Preflight inspection for scenario-driven research profile matrices."""

import datetime as dt
import re
from pathlib import Path

from tests.research.scenario_profiles import ProfileConfigError


LINE_RE = re.compile(r"^\s*(\d+):(\d+):(\d+(?:\.\d+)?)>(.*)$")


def parse_scenario(path):
    commands = []
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        text = raw.split("#", 1)[0].strip()
        if not text:
            continue
        match = LINE_RE.fullmatch(text)
        if match is None:
            raise ProfileConfigError(f"{path}:{number}: invalid scenario line")
        hours, minutes, seconds, command = match.groups()
        elapsed = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
        commands.append((elapsed, command.strip(), number))
    if not commands:
        raise ProfileConfigError(f"{path}: scenario has no commands")
    return commands


def scenario_contract(path):
    commands = parse_scenario(path)
    first_time, first_command, first_line = commands[0]
    if first_time != 0.0 or not first_command.upper().startswith("DATE "):
        raise ProfileConfigError(f"{path}:{first_line}: first command must set DATE at time zero")
    date_args = [part.strip() for part in first_command[5:].split(",")]
    if len(date_args) != 4:
        raise ProfileConfigError(f"{path}:{first_line}: DATE requires day,month,year,time")
    try:
        simulation_utc = dt.datetime.strptime(
            f"{date_args[2]}-{date_args[1]}-{date_args[0]}T{date_args[3]}",
            "%Y-%m-%dT%H:%M:%S",
        ).replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise ProfileConfigError(f"{path}:{first_line}: invalid DATE command") from exc
    safety_holds = [time for time, command, _ in commands if command.upper() == "HOLD"]
    if not safety_holds:
        raise ProfileConfigError(f"{path}: scenario requires a safety HOLD")
    arrival_holds = [command for _, command, _ in commands
                     if command.upper().startswith("ATDIST ") and
                     command.upper().endswith(", HOLD")]
    if not arrival_holds:
        raise ProfileConfigError(f"{path}: scenario requires an ATDIST arrival HOLD")
    positions = []
    for _, command, line in commands:
        upper = command.upper()
        if not (upper.startswith("CRE ") or upper.startswith("CREATE ") or
                upper.startswith("ADDWPT ")):
            continue
        fields = [field.strip() for field in command.split(",")]
        offset = 2 if upper.startswith(("CRE ", "CREATE ")) else 1
        try:
            positions.append((float(fields[offset]), float(fields[offset + 1])))
        except (IndexError, ValueError) as exc:
            raise ProfileConfigError(f"{path}:{line}: cannot determine route position") from exc
    return {
        "simulation_utc": simulation_utc,
        "safety_duration_s": max(safety_holds),
        "positions": positions,
        "command_count": len(commands),
    }


def check_bounds(positions, bounds):
    south, west, north, east = map(float, bounds)
    outside = [(lat, lon) for lat, lon in positions
               if not (south <= lat <= north and west <= lon <= east)]
    if outside:
        raise ProfileConfigError(
            f"weather bounds {bounds} exclude {len(outside)} route position(s); "
            f"first excluded position is {outside[0]}"
        )


def era5_targets(cache, start, duration_s, bounds):
    from bluesky.plugins.windecmwf import WindECMWF
    slot = start.replace(minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(seconds=duration_s)
    provider = object.__new__(WindECMWF)
    provider.cache = Path(cache)
    targets = []
    while slot <= end:
        targets.extend(provider._path(slot, bounds, part)
                       for part, _ in enumerate(provider._areas(bounds)))
        slot += dt.timedelta(hours=1)
    return targets


def validate_resources(contract, config, weather_cache):
    missing = []
    for name, profile in config["profiles"].items():
        atmosphere = profile["atmosphere"]
        if atmosphere["provider"] == "ISA":
            continue
        check_bounds(contract["positions"], atmosphere["bounds"])
        if atmosphere["provider"] == "ERA5":
            import bluesky as bs
            bs.settings.era5_region = atmosphere["region"]
            bs.settings.era5_pressure_levels = atmosphere["pressure_levels_hpa"]
            targets = era5_targets(
                Path(weather_cache) / "era5", contract["simulation_utc"],
                contract["safety_duration_s"], atmosphere["bounds"],
            )
            missing.extend((name, str(path)) for path in targets if not path.is_file())
    return missing
