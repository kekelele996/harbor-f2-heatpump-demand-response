# 楼宇热泵蓄热需量响应

商业综合体把热泵、蓄热罐和楼宇负荷接入同一套日调度程序。能源管理员每天按次日气象、热负荷和电价生成 96 个 15 分钟时段的运行单，交班时要用其中的峰值和费用核对需量结算。现有 `/app/main.py` 在寒潮天气和需量高峰附近经常给出与结算单对不上的结果，有时次日开始时蓄热罐的存量也对不上。

## 要交付的内容

修好 `/app/main.py`。用法是 `python3 /app/main.py <scenario.json>`，输入是一个 JSON 对象，所有时序列长度相同：

- `period_hours`：每个时段的长度，单位 h。
- `outdoor_temp_c`、`heat_demand_kw_th`、`base_load_kw`、`pv_kw`、`energy_price_cny_per_kwh`：室外温度、建筑热负荷、基础用电负荷、光伏出力和电度电价。
- `demand_charge_cny_per_kw_day`：按日结算的需量单价。
- `heat_pump`：额定电功率、供水温度、允许的压缩机档位、制热性能表。性能表给出 `outdoor_c`、`supply_c` 和对应的 `cop`。
- `storage`：蓄热容量、初始和结束蓄热量、充放热功率上限、充放热效率、每小时散热率和结束蓄热量容差。

程序向标准输出写一个 JSON 对象，顶层只含 `schedule` 和 `summary` 两个字段。`schedule` 与时间序列等长，每个时段只含 `interval`、`stage_fraction`、`cop`、`hp_electric_kw`、`hp_heat_kw`、`storage_kwh`、`grid_import_kw`；`storage_kwh` 是该时段结束时的蓄热量。`summary` 只含 `energy_cost_cny`、`demand_cost_cny`、`total_cost_cny`、`peak_import_kw`。所有连续数值保留 6 位小数。

## 计算口径

- 「性能表内的 COP 按室外温度和供水温度所在网格四角进行线性插值。」
- 热泵电功率等于档位乘额定电功率，制热量等于热泵电功率乘当前 COP。
- 「每个时段先扣除蓄热散热损失，再按充热或放热方向分别应用对应效率。」净制热量为正时允许充热，为负时由蓄热补足；任一方向的绝对值都不得超过对应功率上限。
- 「结束时的蓄热量与 `terminal_kwh_th` 的差值不得超过 `terminal_tolerance_kwh`。」
- 并网功率取 `max(0, base_load_kw + hp_electric_kw - pv_kw)`；光伏余电不售出。
- 电度费用是各时段并网电量乘该时段电价之和。需量费用按全时段最大并网功率乘需量单价计算。总费用是电度费用与需量费用之和。
- 「在满足热负荷、设备功率和结束蓄热约束的前提下，使总费用最小。」压缩机只能取 `stage_fractions` 中的档位。

参数个数不对时退出码为 2，不输出 JSON。输入字段缺失、时序列长度不一致、温度超出性能表范围或不存在满足约束的方案时，同样退出码为 2，不输出 JSON。

同一份输入重复执行，输出必须一致。不要访问网络。
