"""楼宇热泵蓄热系统辨识与需量响应的验收测试。"""

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

COP_TRUE = {
    "intercept": 2.8,
    "outdoor": 0.06,
    "supply_delta": -0.035,
    "interaction": 0.0012,
}
STORAGE_TRUE = {
    "charge_efficiency": 0.88,
    "discharge_efficiency": 0.93,
    "loss_rate_per_hour": 0.012,
}


def run_candidate(*args, timeout=CALL_TIMEOUT_SEC):
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


def run_scenario(data):
    key = json.dumps(data, sort_keys=True, separators=(",", ":"))
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


def cop_value(coefficients, outdoor, supply):
    delta = supply - 35.0
    return (
        coefficients["intercept"]
        + coefficients["outdoor"] * outdoor
        + coefficients["supply_delta"] * delta
        + coefficients["interaction"] * outdoor * delta
    )


def cop_calibration():
    samples = []
    for outdoor in (-5.0, 0.0, 5.0):
        for supply in (35.0, 45.0, 55.0):
            cop = cop_value(COP_TRUE, outdoor, supply)
            samples.append({
                "outdoor_c": outdoor,
                "supply_c": supply,
                "hp_electric_kw": 4.0,
                "hp_heat_kw": round(4.0 * cop, 9),
            })
    samples.extend([
        {"outdoor_c": -5.0, "supply_c": 35.0, "hp_electric_kw": 4.0, "hp_heat_kw": 17.4},
        {"outdoor_c": 5.0, "supply_c": 55.0, "hp_electric_kw": 4.0, "hp_heat_kw": 19.6},
        {"outdoor_c": 0.0, "supply_c": 45.0, "hp_electric_kw": 4.0, "hp_heat_kw": 5.8},
    ])
    return {"cop_samples": samples, "storage_samples": ideal_storage_samples()}


def storage_calibration():
    samples = []
    loss = STORAGE_TRUE["loss_rate_per_hour"]
    eta_c = STORAGE_TRUE["charge_efficiency"]
    eta_d = STORAGE_TRUE["discharge_efficiency"]

    def add(start, net, period, outlier=0.0):
        end = start * (1.0 - loss * period)
        if net >= 0.0:
            end += net * period * eta_c
        else:
            end += net * period / eta_d
        samples.append({
            "start_kwh": start,
            "end_kwh": round(end + outlier, 9),
            "net_heat_kw": net,
            "period_hours": period,
        })

    add(8.0, 0.0, 1.0)
    add(12.0, 0.0, 0.5)
    add(5.0, 0.0, 0.25)
    add(2.0, 6.0, 0.5)
    add(5.0, 4.0, 0.25)
    add(1.0, 8.0, 0.5)
    add(6.0, -3.0, 0.5)
    add(8.0, -5.0, 0.25)
    add(4.0, -2.0, 1.0)
    add(3.0, 5.0, 0.5, outlier=0.9)
    add(7.0, 0.0, 0.5, outlier=-0.8)
    add(2.0, -4.0, 0.5, outlier=0.75)
    return {"cop_samples": ideal_cop_samples(), "storage_samples": samples}


def ideal_cop_samples(coefficient=2.0):
    return [
        {"outdoor_c": -5.0, "supply_c": 35.0, "hp_electric_kw": 4.0, "hp_heat_kw": 4.0 * coefficient},
        {"outdoor_c": 5.0, "supply_c": 35.0, "hp_electric_kw": 4.0, "hp_heat_kw": 4.0 * coefficient},
        {"outdoor_c": -5.0, "supply_c": 55.0, "hp_electric_kw": 4.0, "hp_heat_kw": 4.0 * coefficient},
        {"outdoor_c": 5.0, "supply_c": 55.0, "hp_electric_kw": 4.0, "hp_heat_kw": 4.0 * coefficient},
    ]


def ideal_storage_samples():
    return [
        {"start_kwh": 5.0, "end_kwh": 5.0, "net_heat_kw": 0.0, "period_hours": 1.0},
        {"start_kwh": 2.0, "end_kwh": 4.0, "net_heat_kw": 4.0, "period_hours": 0.5},
        {"start_kwh": 6.0, "end_kwh": 5.0, "net_heat_kw": -2.0, "period_hours": 0.5},
    ]


def scenario(length, *, calibration=None, stages=(0.0, 1.0), rated=4.0, supply=45.0):
    return {
        "period_hours": 0.25,
        "outdoor_temp_c": [0.0] * length,
        "heat_demand_kw_th": [0.0] * length,
        "base_load_kw": [0.0] * length,
        "pv_kw": [0.0] * length,
        "energy_price_cny_per_kwh": [0.5] * length,
        "demand_charge_cny_per_kw_day": 0.0,
        "calibration": calibration or {"cop_samples": ideal_cop_samples(), "storage_samples": ideal_storage_samples()},
        "heat_pump": {
            "rated_electric_kw": rated,
            "supply_temp_c": supply,
            "stage_fractions": list(stages),
            "initial_stage_fraction": 0.0,
            "initial_segment_periods": 1,
        },
        "storage": {
            "capacity_kwh_th": 10.0,
            "initial_kwh_th": 0.0,
            "terminal_kwh_th": 0.0,
            "terminal_tolerance_kwh": 0.05,
            "max_charge_kw_th": 10.0,
            "max_discharge_kw_th": 9.0,
        },
    }


def cop_case(outdoor, supply, demand=0.0):
    data = scenario(1, calibration=cop_calibration(), stages=(0.0, 1.0), rated=4.0, supply=supply)
    data["outdoor_temp_c"] = [outdoor]
    data["heat_demand_kw_th"] = [demand]
    data["storage"]["capacity_kwh_th"] = 1.0
    return data


# ---------- gate：入口与输出契约，不计分 ----------

def test_prints_required_json_shape():
    """「程序向标准输出写一个 JSON 对象，顶层只有 model、schedule 和 summary。」"""
    got = run_scenario(scenario(1))
    assert set(got) == {"model", "schedule", "summary"}
    assert set(got["model"]) == {"cop", "storage"}
    assert set(got["model"]["cop"]) == {"intercept", "outdoor", "supply_delta", "interaction"}
    assert set(got["model"]["storage"]) == {"charge_efficiency", "discharge_efficiency", "loss_rate_per_hour"}
    assert len(got["schedule"]) == 1
    assert set(got["schedule"][0]) == {
        "interval", "stage_fraction", "cop", "hp_electric_kw", "hp_heat_kw",
        "storage_kwh", "grid_import_kw",
    }
    assert set(got["summary"]) == {
        "energy_cost_cny", "demand_cost_cny", "startup_cost_cny",
        "total_cost_cny", "peak_import_kw",
    }


def test_wrong_argument_count_exits_2():
    """「参数个数不对时退出码为 2，不输出 JSON。」"""
    proc = run_candidate()
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""


# ---------- 难点 1：从含尖峰的调试记录辨识 COP ----------

def test_cop_fit_recovers_supply_sensitivity():
    """「短时尖峰不能把拟合出来的性能曲线带偏。」"""
    got = run_scenario(cop_case(-2.5, 45.0))
    assert math.isclose(got["schedule"][0]["cop"], 2.27, abs_tol=1e-4)


def test_cop_fit_recovers_interaction_term():
    """「短时尖峰不能把拟合出来的性能曲线带偏。」"""
    got = run_scenario(cop_case(-2.0, 40.0))
    assert math.isclose(got["schedule"][0]["cop"], 2.493, abs_tol=1e-4)


def test_hp_heat_uses_identified_cop():
    """「短时尖峰不能把拟合出来的性能曲线带偏。」"""
    got = run_scenario(cop_case(-2.5, 45.0, demand=9.08))
    row = got["schedule"][0]
    assert math.isclose(row["hp_heat_kw"], 9.08, abs_tol=1e-4)
    assert math.isclose(row["hp_electric_kw"], 4.0, abs_tol=1e-6)


# ---------- 难点 2：从充放与静置记录辨识蓄热参数 ----------

def test_storage_fit_recovers_charge_efficiency():
    """「蓄热记录要分别保留充热、放热和静置三类信息。」"""
    got = run_scenario(scenario(1, calibration=storage_calibration()))
    assert math.isclose(got["model"]["storage"]["charge_efficiency"], 0.88, abs_tol=5e-4)


def test_storage_fit_recovers_discharge_efficiency():
    """「蓄热记录要分别保留充热、放热和静置三类信息。」"""
    got = run_scenario(scenario(1, calibration=storage_calibration()))
    assert math.isclose(got["model"]["storage"]["discharge_efficiency"], 0.93, abs_tol=5e-4)


def test_storage_fit_recovers_standing_loss():
    """「蓄热记录要分别保留充热、放热和静置三类信息。」"""
    got = run_scenario(scenario(1, calibration=storage_calibration()))
    assert math.isclose(got["model"]["storage"]["loss_rate_per_hour"], 0.012, abs_tol=5e-5)


# ---------- 难点 3：全时段需量优化 ----------

def peak_case():
    data = scenario(8, calibration=storage_calibration(), stages=(0.0, 0.5, 1.0))
    data["calibration"] = {"cop_samples": ideal_cop_samples(1.0), "storage_samples": ideal_storage_samples()}
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
    """「在热负荷、功率和结束存量都能满足的前提下，总费用要取最低。」"""
    got = run_scenario(peak_case())
    assert math.isclose(got["summary"]["peak_import_kw"], 3.0, abs_tol=1e-5)


def test_global_plan_demand_cost():
    """「需量费看整个周期里最高的并网功率，再乘合同单价。」"""
    got = run_scenario(peak_case())
    assert math.isclose(got["summary"]["demand_cost_cny"], 300.0, abs_tol=1e-5)


def startup_case():
    data = scenario(8, calibration=storage_calibration(), stages=(0.0, 0.5, 1.0))
    data["calibration"] = {"cop_samples": ideal_cop_samples(1.0), "storage_samples": ideal_storage_samples()}
    data["period_hours"] = 1.0
    data["heat_demand_kw_th"] = [0.0, 0.0, 4.0, 0.0, 0.0, 4.0, 0.0, 0.0]
    data["energy_price_cny_per_kwh"] = [0.1, 0.1, 2.0, 0.1, 0.1, 2.0, 0.1, 0.1]
    data["heat_pump"].update({
        "min_on_periods": 2,
        "min_off_periods": 2,
        "max_stage_change": 1.0,
        "startup_cost_cny": 30.0,
        "initial_stage_fraction": 0.0,
        "initial_segment_periods": 2,
    })
    data["storage"].update({
        "capacity_kwh_th": 4.0,
        "initial_kwh_th": 0.0,
        "terminal_kwh_th": 0.0,
        "terminal_tolerance_kwh": 0.01,
        "max_charge_kw_th": 4.0,
        "max_discharge_kw_th": 4.0,
    })
    return data


def test_global_plan_total_cost_with_startup():
    """「每次从停机状态启动都要计入 startup_cost_cny。」"""
    got = run_scenario(startup_case())
    assert math.isclose(got["summary"]["total_cost_cny"], 34.6, abs_tol=1e-5)


def test_global_plan_respects_minimum_run_and_startup():
    """「压缩机启动后至少连续运行 min_on_periods 个时段。」"""
    got = run_scenario(startup_case())
    assert math.isclose(got["summary"]["startup_cost_cny"], 30.0, abs_tol=1e-6)
    assert math.isclose(got["schedule"][4]["stage_fraction"], 0.0, abs_tol=1e-6)

def initial_off_case():
    data = scenario(4, calibration=storage_calibration(), stages=(0.0, 0.5, 1.0))
    data["calibration"] = {"cop_samples": ideal_cop_samples(1.0), "storage_samples": ideal_storage_samples()}
    data["period_hours"] = 1.0
    data["heat_demand_kw_th"] = [0.0, 0.0, 4.0, 0.0]
    data["energy_price_cny_per_kwh"] = [0.5] * 4
    data["heat_pump"].update({
        "min_on_periods": 2,
        "min_off_periods": 2,
        "max_stage_change": 1.0,
        "startup_cost_cny": 0.0,
        "initial_stage_fraction": 0.0,
        "initial_segment_periods": 0,
    })
    data["storage"].update({
        "capacity_kwh_th": 4.0,
        "initial_kwh_th": 0.0,
        "terminal_kwh_th": 4.0,
        "terminal_tolerance_kwh": 0.01,
        "max_charge_kw_th": 4.0,
        "max_discharge_kw_th": 4.0,
    })
    return data


def end_off_case():
    data = scenario(2, calibration=storage_calibration(), stages=(0.0, 1.0))
    data["calibration"] = {"cop_samples": ideal_cop_samples(1.0), "storage_samples": ideal_storage_samples()}
    data["period_hours"] = 1.0
    data["heat_demand_kw_th"] = [4.0, 0.0]
    data["energy_price_cny_per_kwh"] = [0.0, 0.0]
    data["heat_pump"].update({
        "min_on_periods": 1,
        "min_off_periods": 2,
        "max_stage_change": 1.0,
        "startup_cost_cny": 0.0,
        "initial_stage_fraction": 0.0,
        "initial_segment_periods": 2,
    })
    data["storage"].update({
        "capacity_kwh_th": 1.0,
        "initial_kwh_th": 0.0,
        "terminal_kwh_th": 0.0,
        "terminal_tolerance_kwh": 0.01,
        "max_charge_kw_th": 4.0,
        "max_discharge_kw_th": 4.0,
    })
    return data


def test_initial_off_must_finish_before_start():
    """「初始停机时长没达到 min_off_periods 时，启动要等到补足后再进行。」"""
    got = run_scenario(initial_off_case())
    assert math.isclose(got["schedule"][0]["stage_fraction"], 0.0, abs_tol=1e-6)
    assert math.isclose(got["schedule"][1]["stage_fraction"], 0.0, abs_tol=1e-6)
    assert got["schedule"][2]["stage_fraction"] > 0.0


def test_horizon_end_need_not_complete_min_off():
    """「周期末尾没有后续时段时，不要求补足尚未完成的运行或停机时长。」"""
    got = run_scenario(end_off_case())
    assert got["schedule"][0]["stage_fraction"] > 0.0
    assert math.isclose(got["schedule"][1]["stage_fraction"], 0.0, abs_tol=1e-6)

