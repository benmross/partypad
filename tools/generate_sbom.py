#!/usr/bin/env python3
"""Generate a small CycloneDX SBOM from installed PartyPad runtime dependencies."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path

from packaging.requirements import Requirement
with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as project_file:
    PROJECT_VERSION = tomllib.load(project_file)["project"]["version"]


def runtime_components() -> list[dict[str, str]]:
    pending = ["partypad", "aiortc", "keyring"]
    seen: set[str] = set()
    components: list[dict[str, str]] = [
        {
            "type": "application",
            "name": "PartyPad",
            "version": PROJECT_VERSION,
            "purl": f"pkg:github/benmross/partypad@{PROJECT_VERSION}",
        }
    ]
    while pending:
        requested = pending.pop()
        try:
            distribution = importlib.metadata.distribution(requested)
        except importlib.metadata.PackageNotFoundError:
            continue
        name = distribution.metadata["Name"]
        normalized = name.lower().replace("_", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        version = distribution.version
        components.append(
            {
                "type": "library" if normalized != "partypad" else "application",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{normalized}@{version}",
            }
        )
        for value in distribution.requires or []:
            requirement = Requirement(value)
            if requirement.marker is None or requirement.marker.evaluate({"extra": "online"}):
                pending.append(requirement.name)
    return sorted(components, key=lambda item: item["name"].lower())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    args = parser.parse_args()
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {"timestamp": datetime.now(timezone.utc).isoformat()},
        "components": runtime_components(),
    }
    with open(args.output, "w", encoding="utf-8") as output:
        json.dump(document, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
