#!/usr/bin/env python3
"""Resolve the active hardware profile. Shared by the Python proxy and the
shell scripts, so both agree on which model and which cost basis is in play.

    python scripts/profile.py              # print the whole profile as JSON
    python scripts/profile.py name         # just the profile name
    python scripts/profile.py get target.file
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "config" / "profiles.yaml"


def installed_ram_gb() -> float:
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True, check=True)
        return int(out.stdout.strip()) / (1024 ** 3)
    except Exception:
        # Linux fallback, so this still works in CI or a container.
        try:
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 ** 2)
        except Exception:
            pass
    return 0.0


def resolve() -> tuple[str, dict]:
    cfg = yaml.safe_load(CFG.read_text())
    profiles = cfg["profiles"]

    name = os.environ.get("TOKENRATE_PROFILE") or cfg.get("default", "auto")

    if name != "auto":
        if name not in profiles:
            sys.exit(f"unknown profile '{name}'. Available: {', '.join(profiles)}")
        return name, profiles[name]

    ram = installed_ram_gb()
    for pname, p in profiles.items():
        if p.get("min_ram_gb", 0) <= ram <= p.get("max_ram_gb", 10 ** 6):
            return pname, p

    # Never guess silently about something that determines every cost number.
    sys.exit(
        f"No profile matches {ram:.0f}GB of RAM. Add one to config/profiles.yaml "
        f"or set TOKENRATE_PROFILE explicitly. Available: {', '.join(profiles)}"
    )


def main() -> int:
    name, prof = resolve()
    args = sys.argv[1:]

    if not args:
        print(json.dumps({"name": name, **prof}, indent=2))
    elif args[0] == "name":
        print(name)
    elif args[0] == "get" and len(args) > 1:
        node: object = prof
        for key in args[1].split("."):
            if not isinstance(node, dict) or key not in node:
                sys.exit(f"no such key: {args[1]}")
            node = node[key]
        print(node)
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
