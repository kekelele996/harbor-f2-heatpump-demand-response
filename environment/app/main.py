"""楼宇热泵蓄热系统辨识与需量响应工具。

用法：python3 /app/main.py <scenario.json>
"""

from __future__ import annotations

import itertools
import json
import math
import statistics
import sys

SOC_STEP_KWH = 0.05
HUBER_LIMIT = 1.345


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数值")
    if not math.isfinite(float(value)):
        raise ValueError(f"{name} 必须是有限数值")
    return float(value)


def _solve_linear(matrix, vector):
    """带部分主元的高斯消元，返回方阵方程组的解。"""
    n = len(vector)
    a = [list(map(float, row)) + [float(vector[i])] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(a[row][col]))
        if abs(a[pivot][col]) < 1e-12:
            raise ValueError("调试记录不足以辨识模型参数")
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        for j in range(col, n + 1):
            a[col][j] /= scale
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            if factor == 0.0:
                continue
            for j in range(col, n + 1):
                a[row][j] -= factor * a[col][j]
    return [a[i][n] for i in range(n)]


def _weighted_fit(rows, targets, weights):
    size = len(rows[0])
    normal = [[0.0] * size for _ in range(size)]
    rhs = [0.0] * size
    for row, target, weight in zip(rows, targets, weights):
        for i in range(size):
            rhs[i] += weight * row[i] * target
            for j in range(size):
                normal[i][j] += weight * row[i] * row[j]
    return _solve_linear(normal, rhs)


def _ols(rows, targets):
    return _weighted_fit(rows, targets, [1.0] * len(rows))


def _fit_model(rows, targets):
    return _ols(rows, targets)


def _fit_cop(samples):
    rows, targets = [], []
    for sample in samples:
        electric = _number(sample["hp_electric_kw"], "hp_electric_kw")
        heat = _number(sample["hp_heat_kw"], "hp_heat_kw")
        if electric <= 0.0 or heat <= 0.0:
            raise ValueError("COP 调试样本必须包含正的制热量和电功率")
        outdoor = _number(sample["outdoor_c"], "outdoor_c")
        delta = _number(sample["supply_c"], "supply_c") - 35.0
        rows.append([1.0, outdoor, delta, outdoor * delta])
        targets.append(heat / electric)
    coefficients = _fit_model(rows, targets)
    return {
        "intercept": coefficients[0],
        "outdoor": coefficients[1],
        "supply_delta": coefficients[2],
        "interaction": coefficients[3],
    }


def _fit_storage(samples):
    rows, targets = [], []
    for sample in samples:
        start = _number(sample["start_kwh"], "start_kwh")
        end = _number(sample["end_kwh"], "end_kwh")
        net_heat = _number(sample["net_heat_kw"], "net_heat_kw")
        period = _number(sample["period_hours"], "period_hours")
        if start < 0.0 or period <= 0.0:
            raise ValueError("蓄热调试样本不合法")
        positive = max(net_heat, 0.0)
        negative = max(-net_heat, 0.0)
        rows.append([-start * period, positive * period, -negative * period])
        targets.append(end - start)
    loss, charge_efficiency, inverse_discharge = _fit_model(rows, targets)
    if not 0.0 <= loss < 1.0 or not 0.0 < charge_efficiency <= 1.0 + 1e-9 or inverse_discharge <= 0.0:
        raise ValueError("蓄热调试记录无法得到物理解")
    discharge_efficiency = 1.0 / inverse_discharge
    if discharge_efficiency > 1.0 + 1e-9:
        raise ValueError("放热效率超出物理范围")
    charge_efficiency = min(charge_efficiency, 1.0)
    discharge_efficiency = min(discharge_efficiency, 1.0)
    return {
        "charge_efficiency": charge_efficiency,
        "discharge_efficiency": discharge_efficiency,
        "loss_rate_per_hour": loss,
    }


def _estimate_model(scenario):
    calibration = scenario["calibration"]
    if not calibration.get("cop_samples") or not calibration.get("storage_samples"):
        raise ValueError("缺少 COP 或蓄热调试记录")
    return {
        "cop": _fit_cop(calibration["cop_samples"]),
        "storage": _fit_storage(calibration["storage_samples"]),
    }


def _cop_at(model, outdoor_c, supply_c):
    cop_model = model["cop"]
    delta = supply_c - 35.0
    value = (
        cop_model["intercept"]
        + cop_model["outdoor"] * outdoor_c
        + cop_model["supply_delta"] * delta
        + cop_model["interaction"] * outdoor_c * delta
    )
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("调试记录给出的 COP 模型无效")
    return value


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


def _advance_storage(energy_kwh, net_heat_kw, period_hours, storage_model):
    loss_rate = storage_model["loss_rate_per_hour"]
    eta_charge = storage_model["charge_efficiency"]
    eta_discharge = storage_model["discharge_efficiency"]
    retained = energy_kwh * (1.0 - loss_rate * period_hours)
    if net_heat_kw >= 0.0:
        return retained + net_heat_kw * period_hours * eta_charge
    return retained + net_heat_kw * period_hours / eta_discharge


def _grid_import(scenario, index, hp_electric):
    return max(
        0.0,
        float(scenario["base_load_kw"][index]) + hp_electric - float(scenario["pv_kw"][index]),
    )


def _greedy_path(scenario, cop_values, period, count, stages, rated, storage_model):
    """逐时规则；仅用于留一法退回。"""
    capacity = _number(scenario["storage"]["capacity_kwh_th"], "capacity_kwh_th")
    energy = _number(scenario["storage"]["initial_kwh_th"], "initial_kwh_th")
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
        energy = _advance_storage(energy, net_heat, period, storage_model)
    return tuple(path)


def _evaluate(scenario, path, cop_values, period, storage_model, model):
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
        energy = _advance_storage(energy, net_heat, period, storage_model)
        if energy < -1e-8 or energy > capacity + 1e-8:
            raise ValueError("调度超出蓄热容量")
        grid_import = _grid_import(scenario, t, hp_electric)
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
        "model": {
            "cop": {key: round(value, 6) for key, value in model["cop"].items()},
            "storage": {key: round(value, 6) for key, value in model["storage"].items()},
        },
        "schedule": schedule,
        "summary": {
            "energy_cost_cny": round(energy_cost, 6),
            "demand_cost_cny": round(demand_cost, 6),
            "startup_cost_cny": 0.0,
            "total_cost_cny": round(energy_cost + demand_cost, 6),
            "peak_import_kw": round(peak, 6),
        },
    }


def solve(scenario):
    period, count = _case_inputs(scenario)
    stages, rated, capacity, initial, terminal = _stage_data(scenario)
    model = _estimate_model(scenario)
    supply = _number(scenario["heat_pump"]["supply_temp_c"], "supply_temp_c")
    cop_values = [
        _cop_at(model, float(temp), supply)
        for temp in scenario["outdoor_temp_c"]
    ]
    path = _greedy_path(scenario, cop_values, period, count, stages, rated, model["storage"])
    return _evaluate(scenario, path, cop_values, period, model["storage"], model)


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
