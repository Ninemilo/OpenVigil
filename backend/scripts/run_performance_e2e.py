"""Run a bounded isolated software or services profile without paid model calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from windops_backend.operations.load_contracts import parse_load_profile
from windops_backend.operations.load_services import execute_services
from windops_backend.operations.load_services_inputs import parse_services_inputs
from windops_backend.operations.load_software import execute_software


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument(
        "--services-inputs",
        type=Path,
        help="Private JSON input manifest, required only in services mode",
    )
    args = parser.parse_args()
    try:
        with args.profile.open("rb") as stream:
            profile = parse_load_profile(stream.read(65537))
        if profile.mode == "services":
            if args.services_inputs is None:
                raise ValueError("services inputs required")
            with args.services_inputs.open("rb") as stream:
                inputs = parse_services_inputs(stream.read(65537))
            path, report = asyncio.run(execute_services(profile, inputs))
        else:
            if args.services_inputs is not None:
                raise ValueError("services inputs are not used in software mode")
            path, report = asyncio.run(execute_software(profile))
    except (ValueError, OSError):
        print(
            json.dumps(
                {
                    "status": "not_run",
                    "qualified": False,
                    "code": "INVALID_OR_UNSUPPORTED_LOAD_INPUT",
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "qualified": report["qualified"],
                "report": str(path),
                "failure_codes": report["failure_codes"],
            }
        )
    )
    if report["qualified"]:
        return 0
    return 3 if report["status"] == "measurement_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
