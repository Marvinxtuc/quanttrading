# ma_ensemble_v1 信号翻转注入测试（卖出路径）证据

- 日期：2026-11-02（UTC+8）
- 对象：`state/paper_sma_basket.sqlite` 的**临时副本**（真实状态文件未改动，mtime 仍为 2026-11-01 08:21:40 UTC+8）
- 方法：在副本上注入合成日线（300 天 80000 平盘 → 11-01 收 68000（-15%，低于三条 SMA）→ 11-02 收 88000（高于三条 SMA）），端到端运行 `run_step` 两次。
- 对应 pytest：`tests/test_ma_ensemble_flip_injection.py`（合成账本 + 真实状态副本两个用例）

## 结果

| 检查项 | LONG→CASH | CASH→LONG |
|---|---|---|
| 成交笔数 | 3（A/B/C 全卖） | 3（A/B/C 全买） |
| 成交类型 | limit @ 67966（=68000×(1-5bps)） | limit @ 88044（=88000×(1+5bps)） |
| 每笔手续费 | 0.2205 USD（40 bps） | 0.2187 USD（40 bps） |
| 实际成本 | 44.98 bps/边 | 45.02 bps/边（<60，成本闸门未触发） |
| 卖后现金 | 每子账户 54.89，btc=0 | 现金≈0，btc=0.000621 |
| 对账 mismatches | 0 | 0 |
| orders_sent | 0 | 0 |

## 原始输出

```
pre-state (copy): {"A": {"cash": 0.0, "btc": 0.0008109169810217098}, "B": {"cash": 0.0, "btc": 0.0008109169810217098}, "C": {"cash": 0.0, "btc": 0.0008109169810217098}}
last processed signal_day: 2026-10-31

--- flip LONG->CASH ---
[ma_ensemble_v1 paper] signal day 2026-11-01 close=68000.00  exec 2026-11-02 @ 68000.00
  A SMA150=79920.0  signal=CASH  btc=0.00000000  cash=54.89  equity=54.89
  B SMA200=79940.0  signal=CASH  btc=0.00000000  cash=54.89  equity=54.89
  C SMA250=79952.0  signal=CASH  btc=0.00000000  cash=54.89  equity=54.89
  total equity=164.68  peak=204.41  dd=19.43%  halted=False
  trades today=3  A sell 0.00081092 @ 67966.0 (limit); B sell 0.00081092 @ 67966.0 (limit); C sell 0.00081092 @ 67966.0 (limit)
  reconciliation mismatches=0  orders_sent=0

--- flip CASH->LONG ---
[ma_ensemble_v1 paper] signal day 2026-11-02 close=88000.00  exec 2026-11-03 @ 88000.00
  A SMA150=79973.33  signal=LONG  btc=0.00062100  cash=0.00  equity=54.65
  B SMA200=79980.0  signal=LONG  btc=0.00062100  cash=0.00  equity=54.65
  C SMA250=79984.0  signal=LONG  btc=0.00062100  cash=0.00  equity=54.65
  total equity=163.94  peak=204.41  dd=19.79%  halted=False
  trades today=3  A buy 0.000621 @ 88044.0 (limit); B buy 0.000621 @ 88044.0 (limit); C buy 0.000621 @ 88044.0 (limit)
  reconciliation mismatches=0  orders_sent=0

fees sell: [0.2205, 0.2205, 0.2205] cost_bps: [44.98, 44.98, 44.98]
fees buy: [0.2187, 0.2187, 0.2187] cost_bps: [45.02, 45.02, 45.02]```
