# ma_ensemble_v1 规则（2026-10-20 定稿，上线后不调参）

## 标的与数据
- Kraken BTC/USD 现货，日线（UTC 00:00 收盘）。

## 资金
- 纸交易本金 200 USD，三等分为 A/B/C 三个子账户，各约 66.67 USD，互不挪用。

## 信号（每个子账户独立）
- A：150 日 SMA；B：200 日 SMA；C：250 日 SMA。
- 日收盘价 > SMA → 目标仓位 100% BTC；收盘价 ≤ SMA → 目标仓位 0%（现金）。
- 只做多，不加杠杆，不做空。

## 执行
- 收盘出信号，次日按限价单执行：挂在当前最优价附近（挂单），2 小时内未成交就改价重挂一次，当日 23:00 UTC 仍未成交则按市价补齐。
- 只在目标仓位变化时下单；单笔低于 Kraken 最小下单量则跳过并记日志。
- 成本假设：每边 40 bps（回测中档）；纸交易记录实际成交价，用来对比滑点。

## 风控
- 组合总回撤（相对峰值）超过 60% → 硬停，全部转现金，等人工复核。
- 实际成交成本连续明显高于 60 bps → 暂停，人工复核。
- 沿用现有闸门：熔断可拒单、进程异常告警。

## 纸交易验收（2 周，墙钟时间）
- 定时任务每天在 UTC 00:05 后跑，14 天不漏跑；
- 每天对账：信号、目标仓位、模拟成交和账本一致，mismatches=0；
- 至少手动注入一次信号翻转（或用历史回放单测）以验证下单路径；
- orders_sent=0（纸交易不得触达真单）。

## 实盘前置
- Marvin 确认接受最大可能约 50% 回撤；只放现金入场；交易 Key 只开交易权限，不开提现。
- 每季度复盘；违反风控条件立即停。

## 2026-11-02 补充裁定（量化交易员）
- 成本超标暂停：连续 3 笔实际成本 > 60 bps 即暂停。
- 成本超标暂停期间保持现有仓位、不下新单，人工确认后恢复（与 60% 回撤硬停转现金不同）。
- 盘中限价重挂（2h 重挂、23:00 UTC 市价补齐）只用于实盘；纸交易保持日线一次性撮合。

## ETH variant (paper only)
Same SMA150/200/250 basket, 200 USD split into thirds, 40 bps fee + 5 bps slippage, same kill switch and cost guard.
Pair: Kraken XETHZUSD (ETHUSD) public daily OHLC. Min order: 0.002 ETH (Kraken AssetPairs `ordermin`, checked 2026-11-02).
State: `state/paper_eth_basket.sqlite` / `.log`. Daily: `quanttrading paper-basket --pair ETHUSD --once`.
