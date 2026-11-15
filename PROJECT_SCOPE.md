# Trading Bot: Project Scope

Status: draft v0.1 (2026-11-14)
Owners: Adam, plus a friend collaborating

## 0. Current step (v0.2, 2026-11-14): Bitcoin paper app

Before the AMP/Rithmic account exists, the bot paper-trades BTC-USD on free public data (Coinbase, Kraken) so the strategy, risk, logging, and dashboard pieces can be built and proven now. It runs locally, keeps strategy settings in git-ignored files, and every run can be rechecked with `python run.py verify`. See README.md for usage.

The components below are the same ones the futures bot will use; only the data feed and broker adapter change when Rithmic access arrives.

Milestone 2 has started: `python run.py backtest` replays CME futures (MNQ first) from Databento one-second OHLCV files through the same strategy interface and risk manager, with tick-based fills, per-contract commission, front-month rolls, and logs that `verify` rechecks against the data.

## 1. Goal

Build a first automated trading bot that executes roughly 5-10 trades per day through AMP Futures, using Rithmic for market data and orders. Start with paper trading and move to small live size only after it proves stable.

Success for v1 means:
- The bot runs a full trading day unattended in paper mode with no crashes or duplicate orders.
- Every order, fill, and decision is logged and can be reconciled against the broker's statements.
- Risk limits are enforced in code and cannot be bypassed by strategy logic.

## 2. Non-goals (for v1)

- High-frequency or latency-sensitive trading (5-10 trades/day does not need it).
- Multi-broker support, a UI, or multi-strategy portfolio management.
- Machine learning. Start with rule-based logic.

## 3. Broker

AMP Futures. The bot reaches the account through Rithmic, which AMP provisions.

AMP's own pages list these Rithmic interfaces for an AMP user id:

- **R|API and R|API+**: C++ and .NET libraries. Market data, orders, fills, and (with API+) custom bars plus server-side brackets and OCOs.
- **R|FIX API**: order entry only. Market data still needs R|API.
- **R|Diamond API**: colocated, sub-millisecond. Out of scope for 5-10 trades a day.

**R|Protocol API** (Protocol Buffers, usable from Python) is Rithmic's language-neutral interface to that same platform. AMP's site does not name it. A Python bot would request it from Rithmic, pass their conformance test, then log in with the AMP Rithmic user id.

Fees AMP publishes for the API add-on: $100 per month per user id, plus the $25 Rithmic user id, plus $0.10 per contract filled on live trades. Market data is billed separately. Confirm account minimums and the paper login when the account is opened. Production API use still requires Rithmic's test system and a conformance review.

## 4. Architecture

Components, kept small and separable:

1. **Market data**: connects to the broker feed, normalizes bars/ticks.
2. **Strategy**: pure function from market state to a signal. No broker or network calls inside it. Its rules and parameters are loaded from private config (see section 7).
3. **Risk manager**: gatekeeper between signals and orders. Enforces max position size, max daily loss, max trades/day, and a kill switch.
4. **Execution**: turns approved signals into orders, tracks order state, handles rejects, partial fills, and retries safely (idempotent).
5. **Broker adapter**: the only code that knows AMP and Rithmic specifics.
6. **State and logging**: persistent record of positions, orders, and decisions; structured logs.
7. **Backtester / paper runner**: same strategy code, simulated or paper broker.
8. **Monitoring**: alerts on errors, disconnects, loss limits hit, or unexpected positions.

## 5. Proposed repo layout

```
trading-bot/
  README.md
  PROJECT_SCOPE.md
  src/
    data/          # market data ingestion
    strategy/      # strategy interface + implementations
    risk/          # risk checks and kill switch
    execution/     # order management
    brokers/       # base interface, amp.py (Rithmic), paper.py
    backtest/
    utils/
  config/
    config.example.yaml   # committed, placeholders only
  tests/
  .gitignore              # must ignore real config, .env, logs, data
```

## 6. Milestones

| # | Milestone | Done when |
|---|---|---|
| 0 | Setup | Repo structure, `.gitignore` for secrets, language/tooling chosen, CI runs tests |
| 1 | Broker access | AMP account opened, Rithmic user id requested, Rithmic dev kit and conformance started |
| 2 | Data and backtest | Historical data loads; backtester runs a placeholder strategy end to end |
| 3 | Broker adapter | Can authenticate, stream data, place and cancel an order in the sandbox/paper environment |
| 4 | Risk and execution | Risk limits and kill switch covered by tests; order state machine handles reject/partial fill/disconnect |
| 5 | Strategy v1 | Real strategy plugged in via private config; backtest results reviewed honestly (include costs and slippage) |
| 6 | Paper trading | At least 2 weeks of paper trading with daily reconciliation, no unexplained discrepancies |
| 7 | Limited live | Minimum size, tight loss limit, human watching; scale only after clean results |

## 7. Secrets and privacy

The strategy rules are private and must not appear in this repo, in Claude conversations, or on GitHub.

- Real strategy logic and parameters live outside the repo (a local file or private store) and are loaded at runtime. The repo contains only the strategy *interface* and a dummy example strategy.
- Broker API keys and credentials live in environment variables or a secrets manager, never in code or config committed to git.
- `.gitignore` covers `.env`, real config files, private strategy modules, logs, and data exports. Add a pre-commit secret scanner.
- Open question: if the repo is public, treat everything committed as public. Consider making it private since a friend is collaborating.
- Open question: how the friend gets the private strategy files, if at all (they may not need them to work on infrastructure).

## 8. Risks

- Overfitting in backtests and ignoring fees/slippage.
- Duplicate or runaway orders from retry bugs or reconnects (mitigate: idempotent order IDs, max-orders limits, kill switch).
- Data or connection outages mid-position (mitigate: defined behavior on disconnect, alerts).
- Leaking credentials or private rules via git history (mitigate: ignore rules from day one; rewriting history is painful).
- Futures leverage can lose more than expected quickly (mitigate: small size, hard daily loss limit).

## 9. Open questions

1. Instruments to trade (which futures contracts), and session hours?
2. Language preference (Python is the common choice)?
3. Where will the bot run (laptop, cloud VM)? It needs to be always on during trading hours.
4. Division of work between you and your friend?
5. Starting capital and the max loss you can accept per day and in total?

## 10. Suggested first steps

1. Open the AMP account and request Rithmic API access (section 3).
2. Create the repo skeleton and `.gitignore` (section 5, section 7).
3. Answer the open questions in section 9.
4. Build the backtester and broker adapter in parallel (you and your friend can split these).
