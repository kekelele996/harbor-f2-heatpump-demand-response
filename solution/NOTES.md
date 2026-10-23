# 参考解说明

## 三个难点

| 编号 | 错误认知 | 正确知识 | 对应测试 |
|---|---|---|---|
| difficulty_1 | 调试记录可用普通最小二乘一次拟合 | 成组尖峰会拉偏参数，必须保留多数一致内点后再拟合四参数 COP 曲面 | `test_cop_fit_*`、`test_hp_heat_uses_identified_cop` |
| difficulty_2 | 蓄热罐只用一个往返效率并忽略静置损失 | 静置、充热、放热三类记录共同约束 loss、eta_c、eta_d，且要分别应用 | `test_storage_fit_*` |
| difficulty_3 | 逐时按电价选档即可 | 需量按全周期峰值结算，启动费用和最小启停时长使档位历史进入状态；初始已持续时长计入周期开头，周期末不强制补足未完成时段 | `test_global_plan_*`、`test_initial_off_*`、`test_horizon_end_*` |

## 参考解文件

- `solution/app/main.py`：输入校验、最小截断平方参数辨识、带启停历史的 Pareto 动态规划、精确轨迹复核和 JSON 输出。
- `solve.sh`：只安装参考实现；`HTB_SKIP=difficulty_1,difficulty_2,difficulty_3` 用精确片段分别退回普通最小二乘、几何平均蓄热效率和贪心调度。

## 关键步骤

1. 对 COP 记录构造 `[1, T_out, T_supply-35, T_out*(T_supply-35)]` 特征。
2. 用确定性最小子集枚举求多数一致内点，再用内点做普通最小二乘。
3. 对蓄热记录构造 `[-E_start*dt, pos*dt, -neg*dt]` 特征，同时辨识 loss、eta_c、1/eta_d。
4. 调度状态包含蓄热网格、上一档位、连续运行时长和连续停机时长；周期初从 `initial_stage_fraction` 与 `initial_segment_periods` 恢复历史，周期末不人为延长最后一段。
5. 对每个状态保留 `(并网峰值, 累计费用)` Pareto 前沿，最后用精确能量递推复核结束蓄热和约束。

## 核心公式与独立复算

- COP：`a + b*T_out + c*(T_supply-35) + d*T_out*(T_supply-35)`。
- 蓄热：`E' = E*(1-loss*dt) + pos*dt*eta_c - neg*dt/eta_d`。
- 并网功率：`max(0, base+hp_electric-pv)`。
- 目标：电度费用 + `peak*demand_rate` + 启动费用。
- 独立脚本：`python3 -I research/verify_energy.py`，同时用线性复算和 3^8 穷举核对。
- 详细推导：`research/derivations.md`。

## 容差依据

- COP 和蓄热参数在无尖峰内点上可恢复到 `1e-8`，测试容差取 `5e-4` 或 `5e-5`。
- 小算例结束蓄热容差为 `0.01 kWh`，输出用精确递推复核；状态网格只用于搜索。
- 费用、峰值测试容差 `1e-5`；典型贪心解与最优解相差数十 CNY。

## 权重依据

- difficulty_1 合计 0.25：COP 模型直接决定制热量与电量，是主要物理量。
- difficulty_2 合计 0.30：蓄热参数决定计划是否可执行，且三类记录必须联合使用。
- difficulty_3 合计 0.45：由 6 个测试均分，每项 0.075；覆盖全周期峰值、启动费用、最小运行时长、初始停机补足和周期末截断。
- gate 测试不计分。

## 数据来源

- 负荷：OpenEI submission 515，Chicago `MediumOfficeNew2004`，1998-03-15 的 30 分钟数据。
- 气象：EnergyPlus Weather Chicago O'Hare TMY3，3 月 15 日。
- 变换：负荷乘 0.20；热负荷按 `0.48*(18-T_out)`；PV 按 `8 kWp*GHI/1000*0.82`。
- 摘录和 SHA-256：`research/sources.md`、`research/source_snapshots/`。
- 复现脚本：`research/generate_scenario.py`。

## 答案隔离

- [x] 镜像内没有 `tests/`、`solution/`、`.git/`、缓存和备份
- [x] 题面没有待修函数名、参数真值或测试断言
- [x] Dockerfile 不先复制正确版本再改坏
