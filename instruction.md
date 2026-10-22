# 楼宇热泵蓄热系统辨识与需量响应

商业综合体的热泵和蓄热罐刚完成调试，能源管理员要根据调试遥测建立运行模型，再按次日气象、热负荷和电价生成 96 个 15 分钟时段的运行单。调试记录里有少量短时传感器尖峰；现有 `/app/main.py` 能把记录算成一条曲线，但寒潮日的制热量、蓄热罐结束存量和需量峰值经常与现场复核单对不上。

## 要交付的内容

修好 `/app/main.py`。用法是 `python3 /app/main.py <scenario.json>`，输入是一个 JSON 对象：

- `calibration.cop_samples`：制热性能调试记录，每条含室外温度、供水温度、热泵电功率和制热量。
- `calibration.storage_samples`：蓄热调试记录，每条含时段开始存量、结束存量、净制热量和时段长度。
- `period_hours`：调度时段长度，单位 h。
- `outdoor_temp_c`、`heat_demand_kw_th`、`base_load_kw`、`pv_kw`、`energy_price_cny_per_kwh`：次日室外温度、建筑热负荷、基础用电负荷、光伏出力和电度电价，所有时序列长度相同。
- `demand_charge_cny_per_kw_day`：按日结算的需量单价。
- `heat_pump`：额定电功率、供水温度、允许的压缩机档位、最小连续运行和停机时段数、相邻时段最大档位变化以及每次启动费用。
- `storage`：蓄热容量、初始和结束蓄热量、充放热功率上限以及结束蓄热量容差。

程序向标准输出写一个 JSON 对象，顶层只含 `model`、`schedule` 和 `summary` 三个字段。

- `model.cop` 只含 `intercept`、`outdoor`、`supply_delta`、`interaction`。性能模型为 `intercept + outdoor*T_out + supply_delta*(T_supply-35) + interaction*T_out*(T_supply-35)`。
- `model.storage` 只含 `charge_efficiency`、`discharge_efficiency`、`loss_rate_per_hour`。
- `schedule` 与时间序列等长，每个时段只含 `interval`、`stage_fraction`、`cop`、`hp_electric_kw`、`hp_heat_kw`、`storage_kwh`、`grid_import_kw`；`storage_kwh` 是该时段结束时的蓄热量。
- `summary` 只含 `energy_cost_cny`、`demand_cost_cny`、`startup_cost_cny`、`total_cost_cny`、`peak_import_kw`。

所有连续数值保留 6 位小数。

## 计算口径

- 「调试记录中的尖峰读数不得改变拟合出的性能模型。」
- 「蓄热调试记录要分别保留充热、放热和静置三类信息。」蓄热更新先扣除 `loss_rate_per_hour * period_hours` 对应的存量损失，再在净制热量为正时乘充热效率，为负时除放热效率。
- 热泵电功率等于档位乘额定电功率，制热量等于热泵电功率乘当前 COP。
- 净制热量为正时允许充热，为负时由蓄热补足；任一方向的绝对值都不得超过对应功率上限。
- 「结束时的蓄热量与 `terminal_kwh_th` 的差值不得超过 `terminal_tolerance_kwh`。」
- 并网功率取 `max(0, base_load_kw + hp_electric_kw - pv_kw)`；光伏余电不售出。
- 电度费用是各时段并网电量乘该时段电价之和。需量费用按全时段最大并网功率乘需量单价计算。
- 「压缩机启动后的连续运行时长不得小于 `min_on_periods`。」停机时间不得小于 `min_off_periods`。
- 「相邻时段档位变化不得超过 `max_stage_change`。」
- 「每次从停机状态启动都要计入 `startup_cost_cny`。」
- 「总费用是电度费用、需量费用与启动费用之和。」
- 「在满足热负荷、设备功率和结束蓄热约束的前提下，使总费用最小。」压缩机只能取 `stage_fractions` 中的档位。

参数个数不对时退出码为 2，不输出 JSON。输入字段缺失、时序列长度不一致、调试记录无法辨识出物理参数或不存在满足约束的方案时，同样退出码为 2，不输出 JSON。

同一份输入重复执行，输出必须一致。不要访问网络。
