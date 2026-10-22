"""楼宇热泵蓄热日调度入口。

用法：python3 /app/main.py <scenario.json>
"""

from __future__ import annotations

import json
import math
import statistics
import sys


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数值")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} 必须是有限数值")
    return float(value)


def _cop_at(heat_pump, outdoor_c, supply_c):
    """从性能表中选择最接近供水温度的室外温度曲线。"""
    rows = heat_pump["cop_table"]
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
    raise ValueError("性能表没有可用的室外温度区间")


def _advance_storage(energy_kwh, net_heat_kw, period_hours, storage):
    """按等效往返效率更新蓄热量。"""
    eta = math.sqrt(storage["charge_efficiency"] * storage["discharge_efficiency"])
    if net_heat_kw >= 0.0:
        return energy_kwh + net_heat_kw * period_hours * eta
    return energy_kwh + net_heat_kw * period_hours / eta


def _case_inputs(scenario):
    period = _number(scenario["period_hours"], "period_hours")
    series_names = ("outdoor_temp_c", "heat_demand_kw_th", "base_load_kw", "pv_kw", "energy_price_cny_per_kwh")
    series = {name: scenario[name] for name in series_names}
    if not series["outdoor_temp_c"]:
        raise ValueError("时间序列不能为空")
    count = len(series["outdoor_temp_c"])
    if any(len(values) != count for values in series.values()):
        raise ValueError("时间序列长度必须一致")
    return period, count


def _stage_heat(heat_pump, stage, cop):
    return stage * _number(heat_pump["rated_electric_kw"], "rated_electric_kw") * cop


def _choose_stage(stage_values, desired_heat):
    for stage in stage_values:
        if stage >= desired_heat - 1e-12:
            return stage
    return stage_values[-1]


def _greedy_stages(scenario, count, period, cop_values):
    heat_pump = scenario["heat_pump"]
    storage = scenario["storage"]
    rated = _number(heat_pump["rated_electric_kw"], "rated_electric_kw")
    stages = sorted(_number(v, "stage_fraction") for v in heat_pump["stage_fractions"])
    if 0.0 not in stages:
        raise ValueError("stage_fractions 必须包含 0")
    capacity = _number(storage["capacity_kwh_th"], "capacity_kwh_th")
    energy = _number(storage["initial_kwh_th"], "initial_kwh_th")
    prices = [float(v) for v in scenario["energy_price_cny_per_kwh"]]
    cheap_limit = statistics.median(prices) * 0.95
    result = []
    for t in range(count):
        demand = float(scenario["heat_demand_kw_th"][t])
        surplus_room_kw = max(0.0, capacity * 0.85 - energy) / period
        desired_heat = demand
        if prices[t] <= cheap_limit:
            desired_heat += min(surplus_room_kw, rated * 0.8)
        target_stage = min(stages, key=lambda s: abs(_stage_heat(heat_pump, s, cop_values[t]) - desired_heat))
        net_heat = _stage_heat(heat_pump, target_stage, cop_values[t]) - demand
        energy = _advance_storage(energy, net_heat, period, storage)
        result.append(target_stage)
    return result


def _build_plan(scenario, stages, cop_values):
    period, count = _case_inputs(scenario)
    heat_pump = scenario["heat_pump"]
    storage = scenario["storage"]
    rated = _number(heat_pump["rated_electric_kw"], "rated_electric_kw")
    energy = _number(storage["initial_kwh_th"], "initial_kwh_th")
    supply = _number(heat_pump["supply_temp_c"], "supply_temp_c")
    schedule = []
    imports = []
    energy_cost = 0.0
    for t, stage in enumerate(stages):
        cop = _cop_at(heat_pump, float(scenario["outdoor_temp_c"][t]), supply)
        hp_electric = stage * rated
        hp_heat = hp_electric * cop
        demand = float(scenario["heat_demand_kw_th"][t])
        energy = _advance_storage(energy, hp_heat - demand, period, storage)
        grid_import = max(0.0, float(scenario["base_load_kw"][t]) + hp_electric - float(scenario["pv_kw"][t]))
        imports.append(grid_import)
        energy_cost += grid_import * float(scenario["energy_price_cny_per_kwh"][t]) * period
        schedule.append({
            "interval": t,
            "stage_fraction": round(stage, 6),
            "cop": round(cop, 6),
            "hp_electric_kw": round(hp_electric, 6),
            "hp_heat_kw": round(hp_heat, 6),
            "storage_kwh": round(energy, 6),
            "grid_import_kw": round(grid_import, 6),
        })
    peak = max(imports) if imports else 0.0
    demand_cost = peak * _number(scenario["demand_charge_cny_per_kw_day"], "demand_charge_cny_per_kw_day")
    return {
        "schedule": schedule,
        "summary": {
            "energy_cost_cny": round(energy_cost, 6),
            "demand_cost_cny": round(demand_cost, 6),
            "total_cost_cny": round(energy_cost + demand_cost, 6),
            "peak_import_kw": round(peak, 6),
        },
    }


def main(argv):
    if len(argv) != 2:
        print("usage: python3 /app/main.py <scenario.json>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "r", encoding="utf-8") as handle:
            scenario = json.load(handle)
        period, count = _case_inputs(scenario)
        supply = _number(scenario["heat_pump"]["supply_temp_c"], "supply_temp_c")
        cop_values = [
            _cop_at(scenario["heat_pump"], float(temp), supply)
            for temp in scenario["outdoor_temp_c"]
        ]
        stages = _greedy_stages(scenario, count, period, cop_values)
        plan = _build_plan(scenario, stages, cop_values)
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(plan, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
