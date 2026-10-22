"""楼宇热泵蓄热日调度的参考实现。

用法：python3 /app/main.py <scenario.json>
"""

from __future__ import annotations

import json
import math
import statistics
import sys

SOC_STEP_KWH = 0.05


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数值")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} 必须是有限数值")
    return float(value)


def _bracket(values, point, name):
    if point < values[0] - 1e-9 or point > values[-1] + 1e-9:
        raise ValueError(f"{name} 超出性能表范围")
    if len(values) == 1:
        return 0, 0, 0.0
    for i in range(len(values) - 1):
        if values[i] - 1e-9 <= point <= values[i + 1] + 1e-9:
            span = values[i + 1] - values[i]
            return i, i + 1, 0.0 if span == 0 else (point - values[i]) / span
    raise ValueError(f"{name} 无法插值")


def _cop_at(heat_pump, outdoor_c, supply_c):
    rows = heat_pump["cop_table"]
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
    return low + fy * (high - low)


def _advance_storage(energy_kwh, net_heat_kw, period_hours, storage):
    loss_rate = _number(storage["loss_rate_per_hour"], "loss_rate_per_hour")
    eta_charge = _number(storage["charge_efficiency"], "charge_efficiency")
    eta_discharge = _number(storage["discharge_efficiency"], "discharge_efficiency")
    if not 0.0 < eta_charge <= 1.0 or not 0.0 < eta_discharge <= 1.0:
        raise ValueError("蓄热效率必须在 (0, 1] 内")
    if loss_rate < 0.0 or loss_rate * period_hours >= 1.0:
        raise ValueError("loss_rate_per_hour 不合法")
    retained = energy_kwh * (1.0 - loss_rate * period_hours)
    if net_heat_kw >= 0.0:
        return retained + net_heat_kw * period_hours * eta_charge
    return retained + net_heat_kw * period_hours / eta_discharge


def _case_inputs(scenario):
    period = _number(scenario["period_hours"], "period_hours")
    if period <= 0.0:
        raise ValueError("period_hours 必须为正数")
    names = ("outdoor_temp_c", "heat_demand_kw_th", "base_load_kw", "pv_kw", "energy_price_cny_per_kwh")
    series = {name: scenario[name] for name in names}
    if not series["outdoor_temp_c"]:
        raise ValueError("时间序列不能为空")
    count = len(series["outdoor_temp_c"])
    if any(len(values) != count for values in series.values()):
        raise ValueError("时间序列长度必须一致")
    return period, count


def _stage_data(scenario):
    heat_pump = scenario["heat_pump"]
    storage = scenario["storage"]
    stages = sorted({_number(v, "stage_fraction") for v in heat_pump["stage_fractions"]})
    if not stages or stages[0] < 0.0 or stages[-1] > 1.0 or 0.0 not in stages:
        raise ValueError("stage_fractions 必须在 [0, 1] 内并包含 0")
    rated = _number(heat_pump["rated_electric_kw"], "rated_electric_kw")
    capacity = _number(storage["capacity_kwh_th"], "capacity_kwh_th")
    initial = _number(storage["initial_kwh_th"], "initial_kwh_th")
    terminal = _number(storage["terminal_kwh_th"], "terminal_kwh_th")
    if not 0.0 <= initial <= capacity or not 0.0 <= terminal <= capacity:
        raise ValueError("蓄热初始量或结束量超出容量")
    return stages, rated, capacity, initial, terminal


def _grid_import(scenario, index, hp_electric, stage):
    return max(
        0.0,
        float(scenario["base_load_kw"][index]) + hp_electric - float(scenario["pv_kw"][index]),
    )


def _prune_frontier(items):
    items.sort(key=lambda item: (item[0], item[1]))
    kept = []
    best_cost = math.inf
    for peak, cost, path in items:
        if cost < best_cost - 1e-12:
            kept.append((peak, cost, path))
            best_cost = cost
    return kept


def _greedy_path(scenario, cop_values, period, count, stages, rated):
    """仅用于留一法退回的逐时规则；正式参考解走全时段搜索。"""
    storage = scenario["storage"]
    capacity = _number(storage["capacity_kwh_th"], "capacity_kwh_th")
    energy = _number(storage["initial_kwh_th"], "initial_kwh_th")
    prices = [float(v) for v in scenario["energy_price_cny_per_kwh"]]
    cheap_limit = statistics.median(prices) * 0.95
    path = []
    for t in range(count):
        demand = float(scenario["heat_demand_kw_th"][t])
        desired_heat = demand
        if prices[t] <= cheap_limit:
            room_kw = max(0.0, capacity * 0.85 - energy) / period
            desired_heat += min(room_kw, rated * 0.8)
        stage = stages[-1]
        for candidate in stages:
            if candidate * rated * cop_values[t] >= desired_heat - 1e-12:
                stage = candidate
                break
        path.append(stage)
        net_heat = stage * rated * cop_values[t] - demand
        energy = _advance_storage(energy, net_heat, period, storage)
    return tuple(path)


def _optimize_path(scenario, cop_values, period, count, stages, rated, capacity, initial, terminal):
    storage = scenario["storage"]
    max_charge = _number(storage["max_charge_kw_th"], "max_charge_kw_th")
    max_discharge = _number(storage["max_discharge_kw_th"], "max_discharge_kw_th")
    limit_key = int(round(capacity / SOC_STEP_KWH))
    start_key = int(round(initial / SOC_STEP_KWH))
    states = {start_key: [(0.0, 0.0, ())]}
    for t in range(count):
        demand = float(scenario["heat_demand_kw_th"][t])
        price = float(scenario["energy_price_cny_per_kwh"][t])
        expanded = {}
        for key, frontier in states.items():
            energy = key * SOC_STEP_KWH
            for stage in stages:
                hp_electric = stage * rated
                net_heat = hp_electric * cop_values[t] - demand
                if net_heat > max_charge + 1e-9 or net_heat < -max_discharge - 1e-9:
                    continue
                next_energy = _advance_storage(energy, net_heat, period, storage)
                if next_energy < -1e-8 or next_energy > capacity + 1e-8:
                    continue
                next_key = int(round(next_energy / SOC_STEP_KWH))
                if next_key < 0 or next_key > limit_key:
                    continue
                grid_import = _grid_import(scenario, t, hp_electric, stage)
                increment = grid_import * price * period
                bucket = expanded.setdefault(next_key, [])
                for peak, cost, path in frontier:
                    bucket.append((max(peak, grid_import), cost + increment, path + (stage,)))
        states = {key: _prune_frontier(items) for key, items in expanded.items()}
        if not states:
            raise ValueError("没有满足设备与蓄热约束的调度方案")
    target_key = int(round(terminal / SOC_STEP_KWH))
    terminal_tol = _number(storage["terminal_tolerance_kwh"], "terminal_tolerance_kwh")
    candidates = []
    for key, frontier in states.items():
        if abs(key - target_key) * SOC_STEP_KWH > terminal_tol + SOC_STEP_KWH + 1e-9:
            continue
        for peak, cost, path in frontier:
            candidates.append((cost + peak * _number(scenario["demand_charge_cny_per_kw_day"], "demand_rate"), path))
    if not candidates:
        raise ValueError("没有满足结束蓄热量的调度方案")
    # 网格状态只用于剪枝；最终在精确能量递推上复核结束蓄热量和费用。
    candidates.sort(key=lambda item: (item[0], item[1]))
    for _, path in candidates:
        try:
            _evaluate(scenario, path, cop_values, period)
        except ValueError:
            continue
        return path
    raise ValueError("没有满足结束蓄热量容差的调度方案")


def _evaluate(scenario, path, cop_values, period):
    heat_pump = scenario["heat_pump"]
    storage = scenario["storage"]
    rated = _number(heat_pump["rated_electric_kw"], "rated_electric_kw")
    capacity = _number(storage["capacity_kwh_th"], "capacity_kwh_th")
    energy = _number(storage["initial_kwh_th"], "initial_kwh_th")
    max_charge = _number(storage["max_charge_kw_th"], "max_charge_kw_th")
    max_discharge = _number(storage["max_discharge_kw_th"], "max_discharge_kw_th")
    schedule = []
    imports = []
    energy_cost = 0.0
    for t, stage in enumerate(path):
        hp_electric = stage * rated
        hp_heat = hp_electric * cop_values[t]
        demand = float(scenario["heat_demand_kw_th"][t])
        net_heat = hp_heat - demand
        if net_heat > max_charge + 1e-8 or net_heat < -max_discharge - 1e-8:
            raise ValueError("调度超出蓄热充放功率")
        energy = _advance_storage(energy, net_heat, period, storage)
        if energy < -1e-8 or energy > capacity + 1e-8:
            raise ValueError("调度超出蓄热容量")
        grid_import = _grid_import(scenario, t, hp_electric, stage)
        imports.append(grid_import)
        energy_cost += grid_import * float(scenario["energy_price_cny_per_kwh"][t]) * period
        schedule.append({
            "interval": t,
            "stage_fraction": round(stage, 6),
            "cop": round(cop_values[t], 6),
            "hp_electric_kw": round(hp_electric, 6),
            "hp_heat_kw": round(hp_heat, 6),
            "storage_kwh": round(energy, 6),
            "grid_import_kw": round(grid_import, 6),
        })
    terminal = _number(storage["terminal_kwh_th"], "terminal_kwh_th")
    terminal_tol = _number(storage["terminal_tolerance_kwh"], "terminal_tolerance_kwh")
    if abs(energy - terminal) > terminal_tol + 1e-8:
        raise ValueError("调度没有达到结束蓄热量容差")
    peak = max(imports) if imports else 0.0
    demand_cost = peak * _number(scenario["demand_charge_cny_per_kw_day"], "demand_rate")
    return {
        "schedule": schedule,
        "summary": {
            "energy_cost_cny": round(energy_cost, 6),
            "demand_cost_cny": round(demand_cost, 6),
            "total_cost_cny": round(energy_cost + demand_cost, 6),
            "peak_import_kw": round(peak, 6),
        },
    }


def solve(scenario):
    period, count = _case_inputs(scenario)
    stages, rated, capacity, initial, terminal = _stage_data(scenario)
    supply = _number(scenario["heat_pump"]["supply_temp_c"], "supply_temp_c")
    cop_values = [
        _cop_at(scenario["heat_pump"], float(temp), supply)
        for temp in scenario["outdoor_temp_c"]
    ]
    path = _optimize_path(
        scenario, cop_values, period, count, stages, rated, capacity, initial, terminal
    )
    return _evaluate(scenario, path, cop_values, period)


def main(argv):
    if len(argv) != 2:
        print("usage: python3 /app/main.py <scenario.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            scenario = json.load(handle)
        plan = solve(scenario)
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError, OverflowError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(plan, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
