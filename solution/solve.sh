#!/usr/bin/env bash
# 参考解：安装完整实现；HTB_SKIP 用精确片段把指定难点退回原写法。
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 0644 "$HERE/app/main.py" /app/main.py

SKIP="${HTB_SKIP:-}" /usr/local/bin/python3 - /app/main.py <<'PY'
import os
import sys
from pathlib import Path

REVERT = {
    "difficulty_1": (
        '''    rows = heat_pump["cop_table"]
    xs = sorted({float(row["outdoor_c"]) for row in rows})
    ys = sorted({float(row["supply_c"]) for row in rows})
    lookup = {
        (float(row["outdoor_c"]), float(row["supply_c"])): float(row["cop"])
        for row in rows
    }
    if len(lookup) != len(rows):
        raise ValueError("cop_table 存在重复网格点")
    ix0, ix1, fx = _bracket(xs, outdoor_c, "室外温度")
    iy0, iy1, fy = _bracket(ys, supply_c, "供水温度")
    x0, x1 = xs[ix0], xs[ix1]
    y0, y1 = ys[iy0], ys[iy1]
    c00 = lookup[(x0, y0)]
    c10 = lookup[(x1, y0)]
    c01 = lookup[(x0, y1)]
    c11 = lookup[(x1, y1)]
    low = c00 + fx * (c10 - c00)
    high = c01 + fx * (c11 - c01)
    return low + fy * (high - low)''',
        '''    rows = heat_pump["cop_table"]
    nearest_supply = min(
        {float(row["supply_c"]) for row in rows},
        key=lambda value: abs(value - supply_c),
    )
    points = sorted(
        (float(row["outdoor_c"]), float(row["cop"]))
        for row in rows
        if float(row["supply_c"]) == nearest_supply
    )
    if outdoor_c <= points[0][0]:
        return points[0][1]
    if outdoor_c >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= outdoor_c <= x1:
            ratio = (outdoor_c - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    raise ValueError("性能表没有可用的室外温度区间")''',
    ),
    "difficulty_2": (
        '''    retained = energy_kwh * (1.0 - loss_rate * period_hours)
    if net_heat_kw >= 0.0:
        return retained + net_heat_kw * period_hours * eta_charge
    return retained + net_heat_kw * period_hours / eta_discharge''',
        '''    eta = math.sqrt(eta_charge * eta_discharge)
    if net_heat_kw >= 0.0:
        return energy_kwh + net_heat_kw * period_hours * eta
    return energy_kwh + net_heat_kw * period_hours / eta''',
    ),
    "difficulty_3": (
        '''    path = _optimize_path(
        scenario, cop_values, period, count, stages, rated, capacity, initial, terminal
    )''',
        '''    path = _greedy_path(scenario, cop_values, period, count, stages, rated)''',
    ),
}
skip = [item for item in os.environ.get("SKIP", "").split(",") if item]
unknown = sorted(set(skip) - set(REVERT))
if unknown:
    sys.exit(f"HTB_SKIP 里有未知难点：{unknown}")
path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
for key in skip:
    fixed, original = REVERT[key]
    if text.count(fixed) != 1:
        sys.exit(f"{key} 的修复片段在参考实现里出现 {text.count(fixed)} 次，应为 1")
    text = text.replace(fixed, original)
    print(f"留一法：{key} 退回原写法", file=sys.stderr)
path.write_text(text, encoding="utf-8")
PY
chown runner:runner /app/main.py 2>/dev/null || true
