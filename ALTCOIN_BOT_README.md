# OKX Altcoin Scalper (independent bot)

- Entry point: `altcoin_bot.py`
- Workflow: `.github/workflows/okx-altcoin-live-bot.yml`
- Separate state: `altcoin_swap_state.json`
- USDT-margined swaps only; allowlist: SOL, XRP, DOGE, ADA, LINK, AVAX, DOT, SUI.
- Isolated margin, 5x leverage.
- Maximum two simultaneous positions.
- Per-position margin budget: 5 USDT.
- Take-profit trigger: 2.5%; stop-loss trigger: 1.2%.
- Signal: completed 5m EMA(5/13) + RSI(14) + 5m quote-volume expansion (latest closed candle >= 1.10x prior 20-candle average), filtered by completed 15m EMA(20).
- RSI entry band: 52-68 for long and 32-48 for short, avoiding the most extended momentum readings.
- Offline unit tests: `test_altcoin_bot_offline.py`; included in the shared offline test workflow.
- Runs every five minutes and can also be started manually.

## Safety gate

Live trading is disabled unless both GitHub Actions secrets are explicitly set:
- `ALTCOIN_LIVE_TRADING=true`
- `CONFIRM_ALTCOIN_5X_LIVE=YES`

The bot uses the existing `OKX_API_KEY`, `OKX_API_SECRET`, and `OKX_PASSPHRASE` secrets. Never paste API secrets into chat. Use an OKX API key with trading permission only; withdrawals should be disabled.

This is an independent implementation with its own state file and workflow. It does not modify `bot.py`, the existing workflow, or `swap_state.json`. It has not been live-traded or independently validated against your OKX account settings. Do not enable live trading until the strategy, API responses, contract minimum sizes, and protection orders have been verified.
