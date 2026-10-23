# 商场热泵蓄热日运行单

商场的能源管理员每天下午排次日热泵和蓄热罐的班。刚做完调试，手上有一批带短时传感器尖峰的记录。现有 `/app/main.py` 能跑出一张运行单，但寒潮日的制热量、下班时的蓄热存量、以及需量峰值常和现场复核单对不上。运行单是给夜班操作员看的，每个时段都要能对上设备动作、罐内存量和电网取电，不能只凑出一张看似完整的表。

## 程序怎么跑

用法是 `python3 /app/main.py <scenario.json>`。输入文件里有：

- `calibration.cop_samples`：调试时的室外温度、供水温度、热泵电功率和制热量。
- `calibration.storage_samples`：每段的开始存量、结束存量、净制热量和时段长度。
- `period_hours`：调度时段长度，单位 h。
- `outdoor_temp_c`、`heat_demand_kw_th`、`base_load_kw`、`pv_kw`、`energy_price_cny_per_kwh`：次日各时段的气象和负荷，长度必须一致。
- `demand_charge_cny_per_kw_day`：需量单价。
- `heat_pump`：额定电功率、供水温度、可用的压缩机档位、最小连续运行和停机时段数、相邻时段允许的档位变化、每次启动费用。`initial_stage_fraction` 是周期开始前最后一个时段的档位，`initial_segment_periods` 是这个档位在周期开始前已经连续保持的时段数；两者共同决定周期开头的启停状态。
- `storage`：容量、初始和结束存量、充放热功率上限、结束存量容差。

「程序向标准输出写一个 JSON 对象，顶层只有 model、schedule 和 summary。」`model.cop` 只放 `intercept`、`outdoor`、`supply_delta`、`interaction` 四个数，模型写成 `intercept + outdoor*T_out + supply_delta*(T_supply-35) + interaction*T_out*(T_supply-35)`。`model.storage` 只放 `charge_efficiency`、`discharge_efficiency`、`loss_rate_per_hour`。

`schedule` 的每个时段只放 `interval`、`stage_fraction`、`cop`、`hp_electric_kw`、`hp_heat_kw`、`storage_kwh`、`grid_import_kw`，其中蓄热存量是时段结束时的值。`summary` 放 `energy_cost_cny`、`demand_cost_cny`、`startup_cost_cny`、`total_cost_cny`、`peak_import_kw`。所有连续量保留 6 位小数。

## 模型和运行约束

「短时尖峰不能把拟合出来的性能曲线带偏。」拟合时把明显偏离主体记录的读数排除，但不能把正常的低温性能变化当成故障丢掉。

「蓄热记录要分别保留充热、放热和静置三类信息。」每个时段先扣散热损失，再按净制热量的正负分别乘充热效率或除放热效率。净制热量超过对应充放功率上限、存量越过 0 到容量边界、或结束量与 `terminal_kwh_th` 的差距超过 `terminal_tolerance_kwh`，都算运行单不合格。

热泵电功率是档位乘额定电功率，制热量再乘当前 COP。压缩机只能选给定档位。「压缩机启动后至少连续运行 min_on_periods 个时段。」停机也至少持续 `min_off_periods` 个时段。「相邻时段的档位变化不能超过 max_stage_change。」「每次从停机状态启动都要计入 startup_cost_cny。」

周期开始前的连续运行或停机时段要计入 `initial_segment_periods`。「初始停机时长没达到 min_off_periods 时，启动要等到补足后再进行。」若初始档位不是 0，同理要补足 `min_on_periods` 后才能停机。「周期末尾没有后续时段时，不要求补足尚未完成的运行或停机时长。」

并网功率取 `max(0, base_load_kw + hp_electric_kw - pv_kw)`，光伏余电不卖。电度费是各时段并网电量乘该时段电价的和。需量费看整个周期里最高的并网功率，再乘合同单价。总费用是电度费、需量费和启动费三项之和。

「在热负荷、功率和结束存量都能满足的前提下，总费用要取最低。」

「参数个数不对时退出码为 2，不输出 JSON。」字段缺失、时序列长度不一致、调试记录不足或没有可行运行单时也这样处理。同一份输入重复运行必须给出相同结果，程序不要访问网络。字段为空、记录互相矛盾或约束打架时，把问题写进错误信息，不要静默套默认值。交班前把这张表和现场表逐项碰一下，发现不一致就回头查输入，不要改结果。
