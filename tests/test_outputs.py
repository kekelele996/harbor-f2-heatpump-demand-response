"""楼宇热泵蓄热日调度的验收测试。

每个 test_ 的 docstring 原样引用题面中的一句要求。被测程序以 runner 身份、
干净环境变量和独立进程组运行；隐藏工况在评分时临时生成。
"""

import json
import math
import os
import signal
import subprocess
import tempfile
from pathlib import Path

ENTRY = "/app/main.py"
PYTHON = "/usr/local/bin/python3"
CANDIDATE_USER = "runner"
CANDIDATE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/home/runner",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
}
CALL_TIMEOUT_SEC = 30
_CACHE = {}

COP_TABLE = [
    {"outdoor_c": -5.0, "supply_c": 35.0, "cop": 2.4},
    {"outdoor_c": 0.0, "supply_c": 35.0, "cop": 3.0},
    {"outdoor_c": -5.0, "supply_c": 55.0, "cop": 1.4},
    {"outdoor_c": 0.0, "supply_c": 55.0, "cop": 1.8},
]
FLAT_COP_TABLE = [
    {"outdoor_c": -5.0, "supply_c": 35.0, "cop": 2.0},
    {"outdoor_c": 5.0, "supply_c": 35.0, "cop": 2.0},
    {"outdoor_c": -5.0, "supply_c": 55.0, "cop": 2.0},
    {"outdoor_c": 5.0, "supply_c": 55.0, "cop": 2.0},
]
UNIT_COP_TABLE = [
    {"outdoor_c": -5.0, "supply_c": 35.0, "cop": 1.0},
    {"outdoor_c": 5.0, "supply_c": 35.0, "cop": 1.0},
    {"outdoor_c": -5.0, "supply_c": 55.0, "cop": 1.0},
    {"outdoor_c": 5.0, "supply_c": 55.0, "cop": 1.0},
]


def run_candidate(*args, timeout=CALL_TIMEOUT_SEC):
    """以 runner 身份运行入口；超时结束整个进程组并抛出 TimeoutError。"""
    as_root = os.getuid() == 0
    proc = subprocess.Popen(
        [PYTHON, ENTRY, *[str(a) for a in args]],
        cwd="/app",
        env=CANDIDATE_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        user=CANDIDATE_USER if as_root else None,
        group=CANDIDATE_USER if as_root else None,
        extra_groups=[] if as_root else None,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise TimeoutError(f"{ENTRY} 超过 {timeout}s")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def run_scenario(scenario):
    """把隐藏算例写成候选只读临时文件，运行并缓存结果。"""
    key = json.dumps(scenario, sort_keys=True, separators=(",", ":"))
    if key not in _CACHE:
        folder = Path(tempfile.mkdtemp(prefix="heat-"))
        folder.chmod(0o755)
        case = folder / "scenario.json"
        case.write_text(key, encoding="utf-8")
        case.chmod(0o644)
        proc = run_candidate(case)
        if proc.returncode != 0:
            raise AssertionError(f"exit={proc.returncode} stderr={proc.stderr[-500:]}")
        _CACHE[key] = json.loads(proc.stdout)
    return _CACHE[key]


def scenario(length, *, table=FLAT_COP_TABLE, stages=(0.0, 1.0), rated=4.0, supply=45.0):
    """构造字段完整的基础算例，再由各测试覆盖关键字段。"""
    return {
        "period_hours": 0.25,
        "outdoor_temp_c": [0.0] * length,
        "heat_demand_kw_th": [0.0] * length,
        "base_load_kw": [0.0] * length,
        "pv_kw": [0.0] * length,
        "energy_price_cny_per_kwh": [0.5] * length,
        "demand_charge_cny_per_kw_day": 0.0,
        "heat_pump": {
            "rated_electric_kw": rated,
            "supply_temp_c": supply,
            "stage_fractions": list(stages),
            "cop_table": table,
        },
        "storage": {
            "capacity_kwh_th": 10.0,
            "initial_kwh_th": 0.0,
            "terminal_kwh_th": 0.0,
            "terminal_tolerance_kwh": 0.05,
            "max_charge_kw_th": 10.0,
            "max_discharge_kw_th": 9.0,
            "charge_efficiency": 1.0,
            "discharge_efficiency": 1.0,
            "loss_rate_per_hour": 0.0,
        },
    }


def cop_case(outdoor, supply, demand=0.0):
    data = scenario(1, table=COP_TABLE, stages=(0.0, 1.0), rated=4.0, supply=supply)
    data["outdoor_temp_c"] = [outdoor]
    data["heat_demand_kw_th"] = [demand]
    data["storage"]["capacity_kwh_th"] = 1.0
    return data


def storage_trace_errors(data, plan):
    """按题面公式从候选返回的档位和制热量复算蓄热轨迹。"""
    storage = data["storage"]
    energy = storage["initial_kwh_th"]
    max_error = 0.0
    for row in plan["schedule"]:
        index = row["interval"]
        retained = energy * (1.0 - storage["loss_rate_per_hour"] * data["period_hours"])
        net_heat = row["hp_heat_kw"] - data["heat_demand_kw_th"][index]
        if net_heat >= 0.0:
            energy = retained + net_heat * data["period_hours"] * storage["charge_efficiency"]
        else:
            energy = retained + net_heat * data["period_hours"] / storage["discharge_efficiency"]
        max_error = max(max_error, abs(row["storage_kwh"] - energy))
    terminal_error = abs(energy - storage["terminal_kwh_th"])
    return max_error, terminal_error


# ---------- gate：入口与输出契约，不计分 ----------

def test_prints_required_json_shape():
    """「程序向标准输出写一个 JSON 对象，顶层只含 `schedule` 和 `summary` 两个字段。」"""
    got = run_scenario(scenario(1))
    assert set(got) == {"schedule", "summary"}
    assert len(got["schedule"]) == 1
    assert set(got["schedule"][0]) == {
        "interval", "stage_fraction", "cop", "hp_electric_kw", "hp_heat_kw",
        "storage_kwh", "grid_import_kw",
    }
    assert set(got["summary"]) == {
        "energy_cost_cny", "demand_cost_cny", "total_cost_cny", "peak_import_kw",
    }


def test_wrong_argument_count_exits_2():
    """「参数个数不对时退出码为 2，不输出 JSON。」"""
    proc = run_candidate()
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""


# ---------- 难点 1：COP 双线性插值与制热量 ----------

def test_cop_interpolation_uses_supply_axis():
    """「性能表内的 COP 按室外温度和供水温度所在网格四角进行线性插值。」"""
    got = run_scenario(cop_case(-2.5, 45.0))
    assert math.isclose(got["schedule"][0]["cop"], 2.15, abs_tol=1e-5)


def test_cop_interpolation_inside_grid_cell():
    """「性能表内的 COP 按室外温度和供水温度所在网格四角进行线性插值。」"""
    got = run_scenario(cop_case(-2.0, 40.0))
    assert math.isclose(got["schedule"][0]["cop"], 2.48, abs_tol=1e-5)


def test_hp_heat_uses_interpolated_cop():
    """「性能表内的 COP 按室外温度和供水温度所在网格四角进行线性插值。」"""
    got = run_scenario(cop_case(-2.5, 45.0, demand=8.6))
    row = got["schedule"][0]
    assert math.isclose(row["hp_heat_kw"], 8.6, abs_tol=1e-5)
    assert math.isclose(row["hp_electric_kw"], 4.0, abs_tol=1e-5)


# ---------- 难点 2：蓄热效率、散热损失和结束约束 ----------

def test_discharge_efficiency_trace():
    """「每个时段先扣除蓄热散热损失，再按充热或放热方向分别应用对应效率。」"""
    data = scenario(1)
    data["heat_demand_kw_th"] = [12.0]
    data["storage"].update({
        "capacity_kwh_th": 10.0,
        "initial_kwh_th": 6.0,
        "terminal_kwh_th": 4.6,
        "terminal_tolerance_kwh": 0.01,
        "charge_efficiency": 0.9,
        "discharge_efficiency": 0.8,
        "loss_rate_per_hour": 0.1,
    })
    error, terminal_error = storage_trace_errors(data, run_scenario(data))
    assert error < 1e-4
    assert terminal_error < 1e-4


def test_charge_efficiency_trace():
    """「每个时段先扣除蓄热散热损失，再按充热或放热方向分别应用对应效率。」"""
    data = scenario(1)
    data["energy_price_cny_per_kwh"] = [0.0]
    data["storage"].update({
        "capacity_kwh_th": 5.0,
        "terminal_kwh_th": 1.8,
        "terminal_tolerance_kwh": 0.01,
        "charge_efficiency": 0.9,
        "discharge_efficiency": 0.8,
    })
    error, terminal_error = storage_trace_errors(data, run_scenario(data))
    assert error < 1e-4
    assert terminal_error < 1e-4


def test_standing_loss_trace():
    """「每个时段先扣除蓄热散热损失，再按充热或放热方向分别应用对应效率。」"""
    data = scenario(2)
    data["heat_demand_kw_th"] = [4.0, 4.0]
    data["storage"].update({
        "capacity_kwh_th": 5.0,
        "initial_kwh_th": 1.0,
        "terminal_kwh_th": 2.6575,
        "terminal_tolerance_kwh": 0.01,
        "charge_efficiency": 0.9,
        "discharge_efficiency": 0.8,
        "loss_rate_per_hour": 0.2,
    })
    error, terminal_error = storage_trace_errors(data, run_scenario(data))
    assert error < 1e-4
    assert terminal_error < 1e-4


# ---------- 难点 3：全时段需量优化 ----------

def peak_case():
    data = scenario(8, table=UNIT_COP_TABLE, stages=(0.0, 0.5, 1.0))
    data["period_hours"] = 1.0
    data["heat_demand_kw_th"] = [0.0] * 4 + [4.0] * 4
    data["base_load_kw"] = [1.0] * 8
    data["energy_price_cny_per_kwh"] = [0.3] * 4 + [1.0] * 4
    data["demand_charge_cny_per_kw_day"] = 100.0
    data["storage"].update({
        "capacity_kwh_th": 8.0,
        "initial_kwh_th": 0.0,
        "terminal_kwh_th": 0.0,
        "terminal_tolerance_kwh": 0.01,
        "max_charge_kw_th": 8.0,
        "max_discharge_kw_th": 8.0,
    })
    return data


def test_global_plan_minimizes_site_peak():
    """「在满足热负荷、设备功率和结束蓄热约束的前提下，使总费用最小。」"""
    got = run_scenario(peak_case())
    assert math.isclose(got["summary"]["peak_import_kw"], 3.0, abs_tol=1e-5)


def test_global_plan_demand_cost():
    """「需量费用按全时段最大并网功率乘需量单价计算。」"""
    got = run_scenario(peak_case())
    assert math.isclose(got["summary"]["demand_cost_cny"], 300.0, abs_tol=1e-5)


def test_global_plan_total_cost():
    """「总费用是电度费用与需量费用之和。」"""
    got = run_scenario(peak_case())
    assert math.isclose(got["summary"]["total_cost_cny"], 315.6, abs_tol=1e-5)


def test_global_plan_uses_part_load_during_peak():
    """「在满足热负荷、设备功率和结束蓄热约束的前提下，使总费用最小。」"""
    got = run_scenario(peak_case())
    assert math.isclose(got["schedule"][4]["stage_fraction"], 0.5, abs_tol=1e-6)
