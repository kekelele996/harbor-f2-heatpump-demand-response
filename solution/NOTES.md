# 参考解说明

## 三个难点

| 编号 | 错误认知 | 正确知识 | 对应测试（tests/scoring.json） |
|---|---|---|---|
| difficulty_1 | COP 只随室外温度变化，可直接选最接近的供水温度曲线 | COP 是室外温度与供水温度构成的二维网格，单元内按四角线性插值 | `test_cop_interpolation_*`、`test_hp_heat_uses_interpolated_cop` |
| difficulty_2 | 可用往返效率的平方根同时描述充热和放热，并忽略静置散热 | 每步先扣散热，再按方向分别使用 charge/discharge efficiency，结束量受容差约束 | `test_*_efficiency_trace`、`test_standing_loss_trace` |
| difficulty_3 | 按当前电价逐时决定档位即可，需量费用不会改变最优路径 | 目标函数同时含全周期最大并网功率，必须联合优化蓄热轨迹、部分负荷与需量峰值 | `test_global_plan_*` |

## 参考解文件

- `solution/app/main.py`：完整实现。先校验输入，再生成二维 COP，使用带 Pareto 剪枝的全时段动态规划，在蓄热状态网格上搜索费用最低路径，最后用精确递推复核轨迹。
- `solve.sh`：只安装参考实现；`HTB_SKIP=difficulty_1,difficulty_2,difficulty_3` 用精确片段分别退回原写法。
- `environment/app/main.py`：待修实现；当前按最近供水温度曲线算 COP、用等效往返效率更新蓄热，并采用逐时规则调度。

## 关键步骤

1. 读取并校验所有时间序列长度、设备参数和蓄热边界。
2. 对每个时段按室外温度和供水温度四角插值计算 COP。
3. 在每步先按散热率衰减，再根据净制热量方向应用充热或放热效率。
4. 动态规划状态记录蓄热量和累计需量峰值；对同一蓄热量的 `(peak, energy_cost)` 做 Pareto 剪枝。
5. 结束状态只接受 `terminal_tolerance_kwh` 内的候选，并用精确能量递推复核后再输出。

## 核心公式与独立复算

- COP：
  `c(x,y)=c00(1-fx)(1-fy)+c10 fx(1-fy)+c01(1-fx)fy+c11 fx fy`。
- 蓄热：
  `E' = E(1-loss*dt) + max(net,0)*dt*eta_c + min(net,0)*dt/eta_d`。
- 制热：`Q = stage*P_rated*COP`。
- 并网功率：`P_grid=max(0, P_base+P_hp-P_pv)`。
- 目标：`sum(P_grid*price*dt) + max(P_grid)*demand_rate`。
- 解析推导见 `research/derivations.md`；独立小算例见 `research/verify_energy.py`。

## 容差依据

- COP 与功率是输入表的线性插值，6 位小数输出下的正常差异小于 `1e-5`。
- 蓄热轨迹比较用 `1e-4 kWh`，远小于状态网格 `0.05 kWh`，又能区分错误的 0.1 以上偏差。
- 结束蓄热量由算例显式给出容差；小算例使用 `0.01 kWh`，96 时段实景样例使用 `0.5 kWh`。
- 典型错误解与参考解的峰值差为 2 kW、总费用差约为 207.4 CNY，远大于测试容差。

## 权重依据

- difficulty_1 合计 0.25：COP 是热泵电量与热量的转换基础，影响全部结果，但只有一条标量计算链。
- difficulty_2 合计 0.30：蓄热效率和散热直接决定可交付运行单是否满足结束状态。
- difficulty_3 合计 0.45：需量峰值改变整条 96 时段调度路径，是本题的主要优化难点。
- gate 测试不计分，只检查输出结构和参数错误退出。

## 数据来源

- 基础用电负荷：OpenEI submission 515 的 Chicago `MediumOfficeNew2004` 30 分钟商业参考建筑负荷，按面积比例缩放。
- 室外温度与水平辐照：EnergyPlus Weather 的 Chicago O'Hare TMY3 EPW。
- 算例日期为 3 月 15 日；负荷与 TMY 气象用于构造可复核的 96 时段场景，原始文件哈希、下载地址和变换规则见 `research/sources.md` 与 `research/generate_scenario.py`。
- 热泵性能表是用于接口验证的公开样本级映射；数值不参与回归到某个厂家型号的承诺。

## 答案隔离

- [x] 镜像内没有 `tests/`、`solution/`、`.git/`、缓存和备份
- [x] 题面只描述输入输出和计算口径，不点出待修函数或修复步骤
- [x] Dockerfile 不先复制正确版本再改坏
