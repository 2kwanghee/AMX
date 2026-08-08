"""Validate the AMX report JSON schemas, and the §6.5 sample payloads against them.

usage-report and switch-event both $ref into common.schema.json, so every
document must be registered before any reference can resolve. Run via
`make verify-schemas`.
"""

import json
import sys
from pathlib import Path

from jsonschema import FormatChecker
from jsonschema.validators import Draft202012Validator
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
NAMES = ["common", "usage-report", "switch-event"]

# Straight from docs/AMX-DESIGN.md §6.5 — the schemas exist to accept these.
SAMPLES = {
    "usage-report": {
        "schemaVersion": 1,
        "reportType": "usage",
        "agentId": "ama_x1",
        "generatedAt": "2026-08-07T09:00:00Z",
        "trigger": "schedule",
        "activeAccount": {"amsAccountId": "acc_3", "email": "a@x.io"},
        "poolSummary": {
            "total": 5,
            "active": 1,
            "eligible": 4,
            "quarantined": 0,
            "allExhausted": False,
            "maxUtilizationPct": 61.2,
        },
        "accounts": [
            {
                "amsAccountId": "acc_3",
                "email": "a@x.io",
                "allocationStatus": "active",
                "isCurrent": True,
                "usage": {
                    "fiveHour": {"pct": 61.2, "resetsAt": "2026-08-07T12:30:00Z"},
                    "sevenDay": {"pct": 44.0, "resetsAt": "2026-08-11T00:00:00Z"},
                },
                "usageFetchedAt": "2026-08-07T08:59:48Z",
            }
        ],
    },
    "switch-event": {
        "schemaVersion": 1,
        "reportType": "switch_event",
        "agentId": "ama_x1",
        "eventId": "evt_9",
        "occurredAt": "2026-08-07T09:12:00Z",
        "event": {
            "kind": "switch",
            "trigger": "at-limit",
            "from": {"email": "b@x.io"},
            "to": {"email": "a@x.io"},
        },
        "poolSummary": {"allExhausted": False, "maxUtilizationPct": 95.3},
    },
}


def main() -> int:
    docs = {n: json.loads((SCHEMA_DIR / f"{n}.schema.json").read_text()) for n in NAMES}
    registry = Registry().with_resources(
        (d["$id"], Resource.from_contents(d)) for d in docs.values()
    )

    failed = False
    for name, schema in docs.items():
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(
            schema, registry=registry, format_checker=FormatChecker()
        )
        sample = SAMPLES.get(name)
        if sample is None:
            print(f"schema ok: {name} (no sample)")
            continue
        errors = sorted(validator.iter_errors(sample), key=str)
        if errors:
            failed = True
            print(f"schema FAIL: {name}")
            for e in errors:
                print(f"  {list(e.absolute_path)}: {e.message}")
        else:
            print(f"schema ok: {name} (sample validates)")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
