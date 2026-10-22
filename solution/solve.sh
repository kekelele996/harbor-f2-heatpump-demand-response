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
        '''    coefficients = _robust_fit(rows, targets)
    return {
        "intercept": coefficients[0],''',
        '''    coefficients = _ols(rows, targets)
    return {
        "intercept": coefficients[0],''',
    ),
    "difficulty_2": (
        '''    loss, charge_efficiency, inverse_discharge = _robust_fit(rows, targets)''',
        '''    loss, charge_efficiency, inverse_discharge = _ols(rows, targets)''',
    ),
    "difficulty_3": (
        '''    path = _optimize_path(
        scenario, cop_values, period, count, stages, rated, capacity, initial, terminal,
        model["storage"], model,
    )
    return _evaluate(scenario, path, cop_values, period, model["storage"], model)''',
        '''    path = _greedy_path(scenario, cop_values, period, count, stages, rated, model["storage"])
    return _evaluate(scenario, path, cop_values, period, model["storage"], model)''',
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
