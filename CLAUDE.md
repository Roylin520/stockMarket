# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A daily Taiwan stock market analysis that runs on GitHub Actions. `analyze.py` is the whole app: standard library only, Python 3.11+ (needs `tomllib`; CI uses 3.12). The repo owner edits only `config.toml`, so user-facing text (config comments, validation errors, report page, email) is in Traditional Chinese. Keep it that way. Taiwan colour convention: red is up, green is down.

## Commands

There are no tests, linter or build step.

```sh
python -u analyze.py pre                  # pre-market run: US close + TAIEX futures night session
python -u analyze.py post                 # post-market run: TAIEX close, institutional flows, TW watchlist
FORCE=true python -u analyze.py post      # run on a holiday/weekend too (otherwise it exits early)
TEST_EMAIL=true python -u analyze.py pre  # also send a test email
python -u probe.py                        # check every data source is reachable
```

Without an argument the mode follows the clock (before noon is `pre`). Running locally writes to `data/` and `docs/index.html`, the same files CI commits. Email is sent only on scheduled runs or with `TEST_EMAIL=true`, and only if `SMTP_USER`, `SMTP_PASS` and `MAIL_TO` are set (Gmail SSL 465 by default, `SMTP_HOST`/`SMTP_PORT` override).

## How it runs

`.github/workflows/analyze.yml` runs at 08:00 and 15:30 Taipei on weekdays, with a retry slot 20 minutes / 1 hour later, on pushes that touch `config.toml` or `analyze.py`, and on demand (`mode`, `test_email`, `scheduled` inputs; cron-job.org can trigger it with `scheduled=true`). After each run it commits `docs/` and `data/` back to `main` as "Update analysis …", so pull with `git pull --rebase` before pushing. `docs/index.html` is served by GitHub Pages; the email links to it via `GITHUB_REPOSITORY`.

`data/latest.json` holds the last `pre` and `post` result (score, label, per-indicator signals, errors), stock names, and the run bookkeeping: `tries` (scheduled attempts today per mode) and `emailed` (date each mode was last emailed). A scheduled run skips when that mode already finished today without errors; a run with missing data waits for the retry slot before emailing; each mode emails at most once a day.

## Pipeline in `analyze.py`

1. `load_config()` validates `config.toml` (`[watch]` tw/us lists, `[ai_tw]` groups of Taiwan AI stocks with quoted Chinese group names, `[weights]`, `[label]`) and raises `ConfigError` with Chinese messages. `cfg["tw_all"]` is every Taiwan code to fetch (watch list + AI groups, de-duplicated).
2. `is_trading_day()` uses the TWSE OpenAPI holiday list; entries named "…交易日" (first/last trading day around holidays) are open days.
3. `run_pre()` fetches Yahoo chart API daily bars (`US_INDEX` + `us` watchlist) and the TAIFEX real-time quote API night session (`MarketType=1`, nearest contract). `run_post()` fetches TWSE website JSON (`www.twse.com.tw/rwd/zh/...`, throttled 2 s per call, since TWSE blocks clients calling more than ~3 times in 5 s): index OHLC + volume per month and `BFI82U` institutional flows per day. Stock closes come from two whole-market calls in `quotes_today()`: TWSE `MI_INDEX` (listed) and TPEx `dailyQuotes` (OTC), which also tell which market a code is on. A stock with under 25 days of history backfills ~3 months per month via `STOCK_DAY` (listed) or TPEx `tradingStock` (OTC, volume/value in thousands); the first run with ~20 stocks takes about 2 minutes. Institutional flows are filled for the last 10 days if missing. The TWSE OpenAPI lags a day, so it is not used for prices.
4. `upsert()` merges into `data/history_<name>.csv` (utf-8-sig) keyed by date (and symbol/session/mode), so re-runs never duplicate a day and never blank out values a new row lacks.
5. `score_pre()` / `score_post()` read the histories and score each indicator −2..+2 (mostly `band()` thresholds); `summarize()` weights them into −100..+100 and a label. Results go to `latest.json` and `history_signals.csv`; `hit_rate()` judges `pre` against the same day's TAIEX move and `post` against the next day's, ignoring neutral calls.
6. `write_report()` generates the whole page from one f-string with CSS inside it, so CSS braces are doubled (`{{ }}`). The chart is inline SVG.
7. `notify()` sends one plain-text email per mode.

TAIFEX returns Big5 text mislabelled as UTF-8 in some fields (`TradingSession`, `DispCName`); don't rely on them.
