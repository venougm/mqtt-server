"""EQNS/UNIT/PARM equation application helper.

A position+telemetry packet's `vals` is always exactly 5 raw analog readings.
`apply_equations()` applies the quadratic `value = a*x^2 + b*x + c` (coefficients
from `config['eqns_json'][i]`) to each `vals[i]` for `i in range(5)`, pairs the
result with `config['parm_json'][i]` (name) and `config['unit_json'][i]` (unit),
and returns `{"<name>": {"value": <float>, "unit": <str>}, ...}` for each of
those 5 indices. The 13-element `unit_json`/`parm_json` arrays' indices 5-12
(digital/bit channel names) are never read here -- a position packet has no
corresponding analog value to pair with them.
"""

from __future__ import annotations

from typing import Any


def apply_equations(vals: list[float], config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    eqns = config["eqns_json"]
    unit = config["unit_json"]
    parm = config["parm_json"]

    result: dict[str, dict[str, Any]] = {}
    for i in range(5):
        a, b, c = eqns[i]
        x = vals[i]
        value = a * (x ** 2) + b * x + c
        name = parm[i]
        result[name] = {"value": value, "unit": unit[i]}
    return result
