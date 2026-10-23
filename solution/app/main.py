"""楼宇热泵蓄热系统辨识与需量响应参考实现。

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


def _valid_stage_fraction(value):
    return value >= 0.0 and value <= 1.0


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


def _robust_fit(rows, targets):
    """用确定性最小子集枚举找出多数一致样本，再只用内点拟合。"""
    size = len(rows[0])
    best = None
    if len(rows) <= 18:
        for subset in itertools.combinations(range(len(rows)), size):
            try:
                trial = _ols([rows[i] for i in subset], [targets[i] for i in subset])
            except ValueError:
                continue
            residuals = [
                abs(target - sum(coefficient * value for coefficient, value in zip(trial, row)))
                for row, target in zip(rows, targets)
            ]
            trim_count = max(size + 1, int(math.ceil(0.7 * len(residuals))))
            trimmed = sorted(residuals)[:trim_count]
            score = (-sum(trimmed), -trimmed[-1], -max(residuals))
            if best is None or score > best[0]:
                cutoff = max(trimmed[-1] * (1.0 + 1e-6), 1e-7)
                best = (score, [value <= cutoff for value in residuals])
    if best is not None and sum(best[1]) >= size + 1:
        mask = best[1]
        return _ols(
            [row for row, keep in zip(rows, mask) if keep],
            [target for target, keep in zip(targets, mask) if keep],
        )
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
    coefficients = _robust_fit(rows, targets)
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
    loss, charge_efficiency, inverse_discharge = _robust_fit(rows, targets)
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
    if not stages or not all(_valid_stage_fraction(value) for value in stages) or 0.0 not in stages:
        raise ValueError("stage_fractions 必须在 [0, 1] 内并包含 0")
    rated = _number(heat_pump["rated_electric_kw"], "rated_electric_kw")
    capacity = _number(storage["capacity_kwh_th"], "capacity_kwh_th")
    initial = _number(storage["initial_kwh_th"], "initial_kwh_th")
    terminal = _number(storage["terminal_kwh_th"], "terminal_kwh_th")
    if not 0.0 <= initial <= capacity or not 0.0 <= terminal <= capacity:
        raise ValueError("蓄热初始量或结束量超出容量")
    initial_stage = _number(heat_pump.get("initial_stage_fraction", 0.0), "initial_stage_fraction")
    if initial_stage not in stages:
        raise ValueError("initial_stage_fraction 必须来自 stage_fractions")
    initial_periods = _number(heat_pump.get("initial_segment_periods", 0), "initial_segment_periods")
    if not initial_periods.is_integer() or initial_periods < 0:
        raise ValueError("initial_segment_periods 必须是非负整数")
    return stages, rated, capacity, initial, terminal, initial_stage, int(initial_periods)


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


def _prune_frontier(items):
    items.sort(key=lambda item: (item[0], item[1]))
    kept = []
    best_cost = math.inf
    for peak, cost, path in items:
        if cost < best_cost - 1e-12:
            kept.append((peak, cost, path))
            best_cost = cost
    return kept


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


def _initial_stage_memory(heat_pump):
    min_on = int(_number(heat_pump.get("min_on_periods", 1), "min_on_periods"))
    min_off = int(_number(heat_pump.get("min_off_periods", 1), "min_off_periods"))
    previous = _number(heat_pump.get("initial_stage_fraction", 0.0), "initial_stage_fraction")
    raw_count = _number(heat_pump.get("initial_segment_periods", 0), "initial_segment_periods")
    if not raw_count.is_integer() or raw_count < 0:
        raise ValueError("initial_segment_periods 必须是非负整数")
    count = int(raw_count)
    if previous > 0.0:
        return previous, min(min_on, count), 0
    return 0.0, 0, min(min_off, count)


def _constrained_stage_step(heat_pump, stages, previous, stage, run_count, off_count, startup_cost):
    min_on = int(_number(heat_pump.get("min_on_periods", 1), "min_on_periods"))
    min_off = int(_number(heat_pump.get("min_off_periods", 1), "min_off_periods"))
    ramp = _number(heat_pump.get("max_stage_change", 1.0), "max_stage_change")
    if min_on < 1 or min_off < 0 or ramp < 0.0 or ramp > 1.0:
        raise ValueError("热泵启停或爬坡参数不合法")
    if abs(stage - previous) > ramp + 1e-12:
        return None
    if previous > 0.0 and stage == 0.0 and run_count < min_on:
        return None
    if previous == 0.0 and stage > 0.0 and off_count < min_off:
        return None
    added_startup = startup_cost if previous == 0.0 and stage > 0.0 else 0.0
    if stage > 0.0:
        next_run = min(min_on, run_count + 1 if previous > 0.0 else 1)
        next_off = 0
    else:
        next_run = 0
        next_off = min(min_off, off_count + 1 if previous == 0.0 else 1)
    return next_run, next_off, added_startup


def _optimize_path(scenario, cop_values, period, count, stages, rated, capacity, initial, terminal, initial_stage, initial_periods, storage_model, model):
    max_charge = _number(scenario["storage"]["max_charge_kw_th"], "max_charge_kw_th")
    max_discharge = _number(scenario["storage"]["max_discharge_kw_th"], "max_discharge_kw_th")
    heat_pump = scenario["heat_pump"]
    startup_cost = _number(heat_pump.get("startup_cost_cny", 0.0), "startup_cost_cny")
    min_off = int(_number(heat_pump.get("min_off_periods", 1), "min_off_periods"))
    limit_key = int(round(capacity / SOC_STEP_KWH))
    start_key = int(round(initial / SOC_STEP_KWH))
    # 状态：蓄热量网格、上一档位、连续运行时长、连续停机时长。
    initial_previous, initial_run, initial_off = _initial_stage_memory({
        **scenario["heat_pump"],
        "initial_stage_fraction": initial_stage,
        "initial_segment_periods": initial_periods,
    })
    states = {(start_key, initial_previous, initial_run, initial_off): [(0.0, 0.0, ())]}
    for t in range(count):
        demand = float(scenario["heat_demand_kw_th"][t])
        price = float(scenario["energy_price_cny_per_kwh"][t])
        expanded = {}
        for (key, previous, run_count, off_count), frontier in states.items():
            energy = key * SOC_STEP_KWH
            for stage in stages:
                step = _constrained_stage_step(
                    heat_pump, stages, previous, stage, run_count, off_count, startup_cost
                )
                if step is None:
                    continue
                next_run, next_off, added_startup = step
                hp_electric = stage * rated
                net_heat = hp_electric * cop_values[t] - demand
                if net_heat > max_charge + 1e-9 or net_heat < -max_discharge - 1e-9:
                    continue
                next_energy = _advance_storage(energy, net_heat, period, storage_model)
                if next_energy < -1e-8 or next_energy > capacity + 1e-8:
                    continue
                next_key = int(round(next_energy / SOC_STEP_KWH))
                if next_key < 0 or next_key > limit_key:
                    continue
                grid_import = _grid_import(scenario, t, hp_electric)
                increment = grid_import * price * period + added_startup
                state_key = (next_key, stage, next_run, next_off)
                bucket = expanded.setdefault(state_key, [])
                for peak, cost, path in frontier:
                    bucket.append((max(peak, grid_import), cost + increment, path + (stage,)))
        states = {key: _prune_frontier(items) for key, items in expanded.items()}
        if not states:
            raise ValueError("没有满足设备与蓄热约束的调度方案")
    target_key = int(round(terminal / SOC_STEP_KWH))
    tolerance = _number(scenario["storage"]["terminal_tolerance_kwh"], "terminal_tolerance_kwh")
    demand_rate = _number(scenario["demand_charge_cny_per_kw_day"], "demand_rate")
    candidates = []
    for (key, previous, run_count, off_count), frontier in states.items():
        if abs(key - target_key) * SOC_STEP_KWH > tolerance + SOC_STEP_KWH + 1e-9:
            continue
        for peak, cost, path in frontier:
            candidates.append((cost + peak * demand_rate, path))
    if not candidates:
        raise ValueError("没有满足结束蓄热量的调度方案")
    candidates.sort(key=lambda item: (item[0], item[1]))
    for _, path in candidates:
        try:
            _evaluate(scenario, path, cop_values, period, storage_model, model)
        except ValueError:
            continue
        return path
    raise ValueError("没有满足结束蓄热量容差的调度方案")


def _evaluate(scenario, path, cop_values, period, storage_model, model):
    heat_pump = scenario["heat_pump"]
    storage = scenario["storage"]
    rated = _number(heat_pump["rated_electric_kw"], "rated_electric_kw")
    capacity = _number(storage["capacity_kwh_th"], "capacity_kwh_th")
    energy = _number(storage["initial_kwh_th"], "initial_kwh_th")
    max_charge = _number(storage["max_charge_kw_th"], "max_charge_kw_th")
    max_discharge = _number(storage["max_discharge_kw_th"], "max_discharge_kw_th")
    heat_pump = scenario["heat_pump"]
    startup_cost = _number(heat_pump.get("startup_cost_cny", 0.0), "startup_cost_cny")
    schedule = []
    imports = []
    energy_cost = 0.0
    startup_total = 0.0
    previous, run_count, off_count = _initial_stage_memory(heat_pump)
    for t, stage in enumerate(path):
        step = _constrained_stage_step(
            heat_pump, [stage], previous, stage, run_count, off_count, startup_cost
        )
        if step is None:
            raise ValueError("调度违反热泵启停或爬坡约束")
        run_count, off_count, added_startup = step
        startup_total += added_startup
        previous = stage
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
            "startup_cost_cny": round(startup_total, 6),
            "total_cost_cny": round(energy_cost + demand_cost + startup_total, 6),
            "peak_import_kw": round(peak, 6),
        },
    }


def solve(scenario):
    period, count = _case_inputs(scenario)
    stages, rated, capacity, initial, terminal, initial_stage, initial_periods = _stage_data(scenario)
    model = _estimate_model(scenario)
    supply = _number(scenario["heat_pump"]["supply_temp_c"], "supply_temp_c")
    cop_values = [
        _cop_at(model, float(temp), supply)
        for temp in scenario["outdoor_temp_c"]
    ]
    path = _optimize_path(
        scenario, cop_values, period, count, stages, rated, capacity, initial, terminal,
        initial_stage, initial_periods, model["storage"], model,
    )
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
