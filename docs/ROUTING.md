# Strategy Routing

This note documents the current signal-routing split in `quant-python`.

## Main path

- `TrendFollowingStrategy` is the primary entry strategy for bull and range regimes.
- `MeanReversionStrategy` is only considered in range regimes.
- `BreakoutStrategy` is considered in range and bull regimes.
- `DefensiveStrategy` is only considered in bear regimes.
- `StrategyRouter` merges all candidate signals and resolves conflicts by action priority:
  `SELL > REDUCE > ADD > BUY`.

## Runtime flow

1. `StrategyEngine.run_daily_scan()` builds the candidate pool.
2. `StrategyRouter.route_signals()` generates regime-specific signals.
3. Router conflict resolution keeps one highest-priority signal per symbol.
4. Exit signals, T signals, and risk alerts are merged afterwards.

## Compatibility

- `StrategyEngine.generate_buy_signals()` remains as a compatibility entry point.
- The live daily-scan path no longer builds trend entry signals directly inside `StrategyEngine`.
