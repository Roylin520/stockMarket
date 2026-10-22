"""Taiwan stock market daily analysis (GitHub Actions edition).

Two runs per trading day, picked by the first argument (or by the clock if omitted):
  pre   08:00  US close (indices + AI watchlist from Yahoo) and TAIEX futures night session (TAIFEX)
  post  15:30  TAIEX close, volume, institutional net buy/sell and the Taiwan watchlist (TWSE),
               each stock read on technicals, chips and fundamentals
Each run:
1. Appends to the history CSVs in data/.
2. Scores each indicator -2..+2, weights them (config.toml [weights]) into -100..+100 and a label.
3. Saves the result to data/latest.json and data/history_signals.csv (used for the hit rate).
4. Regenerates docs/index.html (GitHub Pages) from those files.
5. On scheduled runs, emails the result once per day and mode.
Days the TWSE holiday schedule marks as closed are skipped.
"""
import csv, json, os, re, smtplib, sys, time, tomllib, traceback, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.toml"
DATA = HERE / "data"
LATEST = DATA / "latest.json"   # {"pre": result, "post": result, "names": {code: name}}
FUND = DATA / "fundamentals.json"   # {code: monthly revenue and valuation}, refreshed by post runs
REPORT = HERE / "docs" / "index.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

HOLIDAYS_API = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
TWSE = "https://www.twse.com.tw/rwd/zh/{path}?response=json&{query}"
TPEX = "https://www.tpex.org.tw/www/zh-tw/{path}?response=json&{query}"
TAIFEX_QUOTES = "https://mis.taifex.com.tw/futures/api/getQuoteList"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"
# Monthly revenue and P/E, P/B, yield: listed (TWSE OpenAPI) and OTC (TPEx OpenAPI). A day behind, fine here.
REVENUE_API = ("https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
               "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap05_O")
PE_TSE = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
PE_OTC = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
US_INDEX = {"^SOX": "費城半導體", "^IXIC": "那斯達克", "^GSPC": "S&P 500", "^DJI": "道瓊",
            "^VIX": "VIX 恐慌指數", "TWD=X": "美元兌台幣"}
W_PRE = ("night_futures", "sox", "tsm_adr", "nasdaq", "ai_stocks", "vix", "usdtwd")
W_POST = ("taiex_change", "ai_tw", "ma", "volume", "foreign", "trust", "foreign_streak")

TAIEX_COLS = ["date", "open", "high", "low", "close", "change", "volume", "value", "transactions",
              "foreign_net", "trust_net", "dealer_net"]
# foreign/trust/dealer: net buy in shares; margin/short: margin purchase and short sale balance in lots (張).
STOCK_COLS = ["date", "open", "high", "low", "close", "change", "volume", "value",
              "foreign", "trust", "dealer", "margin", "short"]
US_COLS = ["date", "symbol", "close", "prev_close", "change_pct"]
FUT_COLS = ["date", "session", "contract", "last", "ref", "change", "change_pct", "volume"]
SIGNAL_COLS = ["date", "mode", "score", "label"]


class ConfigError(Exception):
    pass


class NotReady(Exception):
    """The source has not published today's data yet."""


def load_config():
    """Read config.toml and validate, with messages the user can act on."""
    try:
        with open(CONFIG, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config.toml 格式錯誤：{e}") from None
    try:
        w, lab = raw["watch"], raw["label"]
        cfg = {"tw": [str(c).strip() for c in w["tw"]], "us": [str(s).strip().upper() for s in w["us"]],
               "ai": {str(g): [str(c).strip() for c in codes] for g, codes in raw.get("ai_tw", {}).items()},
               "weights": {k: float(raw["weights"][k]) for k in W_PRE + W_POST},
               "bullish": float(lab["bullish"]), "bearish": float(lab["bearish"])}
    except KeyError as k:
        raise ConfigError(f"config.toml 缺少設定項目：{k}") from None
    except (AttributeError, TypeError, ValueError) as err:
        raise ConfigError(f"config.toml 數值格式錯誤：{err}（股票清單要寫成 [\"代號\", ...]，權重要填數字）") from None
    cfg["ai_codes"] = list(dict.fromkeys(c for codes in cfg["ai"].values() for c in codes))
    cfg["tw_all"] = list(dict.fromkeys(cfg["tw"] + cfg["ai_codes"]))   # every Taiwan stock to fetch

    problems = []
    for c in cfg["tw_all"]:
        if not re.fullmatch(r"[0-9A-Z]{4,6}", c):
            problems.append(f"台股代號要是 4～6 碼（目前是「{c}」）")
    for s in cfg["us"]:
        if not re.fullmatch(r"[A-Z][A-Z.\-]{0,9}", s):
            problems.append(f"us 的代號要是英文字母（目前是「{s}」）")
    for key in ("tw", "us"):
        dup = sorted({x for x in cfg[key] if cfg[key].count(x) > 1})
        if dup:
            problems.append(f'{key} 有重複的代號：{"、".join(dup)}')
    neg = [k for k, v in cfg["weights"].items() if v < 0]
    if neg:
        problems.append(f'[weights] 不能是負數：{"、".join(neg)}')
    if not -100 < cfg["bearish"] < 0 < cfg["bullish"] < 100:
        problems.append("[label] 要符合 -100 < bearish < 0 < bullish < 100")
    if problems:
        raise ConfigError("；".join(problems))
    return cfg


# ---------- fetching ----------

def get_json(url, data=None, retries=2):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=30) as r:
                return json.load(r)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(3 * (attempt + 1))


def num(s):
    """'1,234.50' / '+15.00' / 'X0.00' (ex-dividend mark) / '--' -> float or None."""
    s = re.sub(r"[^0-9.\-]", "", str(s))
    try:
        return float(s)
    except ValueError:
        return None


def roc(s):
    """'115/10/21' or '1151021' (ROC calendar) -> date."""
    s = s.replace("/", "")
    return date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))


def is_trading_day(d):
    if d.weekday() >= 5:
        return False
    for h in get_json(HOLIDAYS_API):
        # The list also announces the first/last trading day around long holidays; those are open.
        if roc(h["Date"]) == d and "交易日" not in h["Name"]:
            print(f'{d} 休市：{h["Name"]}')
            return False
    return True


def twse(path, **query):
    """TWSE website JSON. Throttled, because TWSE blocks clients that call more than ~3 times in 5 s."""
    time.sleep(2)
    data = get_json(TWSE.format(path=path, query=urllib.parse.urlencode(query)))
    if data.get("stat") != "OK":
        raise NotReady(f'{path} {query}: {data.get("stat")}')
    return data


def taiex_month(d):
    """Index OHLC and market volume for d's month -> {date: row}."""
    rows = {}
    for r in twse("TAIEX/MI_5MINS_HIST", date=d.strftime("%Y%m01"))["data"]:
        rows[roc(r[0])] = {"date": roc(r[0]), "open": num(r[1]), "high": num(r[2]), "low": num(r[3]),
                           "close": num(r[4])}
    for r in twse("afterTrading/FMTQIK", date=d.strftime("%Y%m01"))["data"]:
        if roc(r[0]) in rows:
            rows[roc(r[0])] |= {"volume": num(r[1]), "value": num(r[2]), "transactions": num(r[3]),
                                "change": num(r[5])}
    return rows


def institutional(d):
    """Net buy/sell in NT$ by foreign investors, investment trusts and dealers on day d."""
    net = {"foreign_net": 0, "trust_net": 0, "dealer_net": 0}
    for name, _, _, diff in twse("fund/BFI82U", type="day", dayDate=d.strftime("%Y%m%d"))["data"]:
        if name.startswith("外資"):        # 外資及陸資 + 外資自營商
            net["foreign_net"] += num(diff)
        elif name == "投信":
            net["trust_net"] += num(diff)
        elif name.startswith("自營商"):    # 自行買賣 + 避險
            net["dealer_net"] += num(diff)
    return net


def tpex(path, **query):
    """TPEx (OTC market) website JSON."""
    time.sleep(1)
    data = get_json(TPEX.format(path=path, query=urllib.parse.urlencode(query)))
    if str(data.get("stat")).lower() != "ok" or not data.get("tables"):
        raise NotReady(f'{path} {query}: {data.get("stat")}')
    return data


def quotes_today(d, errors):
    """Every listed (TWSE) and OTC (TPEx) stock's bar for day d, two calls in all -> {code: (name, row)}."""
    out = {}
    data = twse("afterTrading/MI_INDEX", date=d.strftime("%Y%m%d"), type="ALLBUT0999")
    table = next(t for t in data["tables"] if t.get("fields") and t["fields"][0] == "證券代號")
    for r in table["data"]:
        # The sign is a separate HTML snippet like '<p style= color:green>-</p>'.
        sign = -1 if "-" in r[9] else 1
        out[r[0]] = (r[1].strip(), "tse", {"date": d, "volume": num(r[2]), "value": num(r[4]), "open": num(r[5]),
                                           "high": num(r[6]), "low": num(r[7]), "close": num(r[8]),
                                           "change": sign * (num(r[10]) or 0)})
    try:
        for r in tpex("afterTrading/dailyQuotes", date=d.strftime("%Y/%m/%d"), type="EW")["tables"][0]["data"]:
            out.setdefault(r[0], (r[1].strip(), "otc", {
                "date": d, "close": num(r[2]), "change": num(r[3]), "open": num(r[4]), "high": num(r[5]),
                "low": num(r[6]), "volume": num(r[8]), "value": num(r[9])}))
    except Exception as e:
        errors.append(f"上櫃股票收盤：{type(e).__name__}: {e}")
    # A stock that didn't trade has no close; leave it out rather than write a blank day.
    return {code: q for code, q in out.items() if q[2]["close"] is not None}


def stock_month(code, d):
    """Listed stock's daily bars for d's month -> {date: row}."""
    rows = {}
    for r in twse("afterTrading/STOCK_DAY", date=d.strftime("%Y%m01"), stockNo=code)["data"]:
        rows[roc(r[0])] = {"date": roc(r[0]), "volume": num(r[1]), "value": num(r[2]), "open": num(r[3]),
                           "high": num(r[4]), "low": num(r[5]), "close": num(r[6]), "change": num(r[7])}
    return rows


def otc_month(code, d):
    """OTC stock's daily bars for d's month -> {date: row}. TPEx reports volume and value in thousands."""
    rows = {}
    for r in tpex("afterTrading/tradingStock", code=code, date=d.strftime("%Y/%m/01"))["tables"][0]["data"]:
        rows[roc(r[0])] = {"date": roc(r[0]), "volume": num(r[1]) * 1000, "value": num(r[2]) * 1000,
                           "open": num(r[3]), "high": num(r[4]), "low": num(r[5]), "close": num(r[6]),
                           "change": num(r[7])}
    return rows


def stock_flows(d):
    """Every listed and OTC stock's institutional net buy in shares on day d -> {code: row}."""
    out = {}
    for r in twse("fund/T86", date=d.strftime("%Y%m%d"), selectType="ALLBUT0999")["data"]:
        # 外陸資 + 外資自營商, 投信, 自營商 (自行買賣 + 避險)
        out[r[0].strip()] = {"foreign": num(r[4]) + num(r[7]), "trust": num(r[10]), "dealer": num(r[11])}
    try:
        for r in tpex("insti/dailyTrade", type="Daily", sect="EW", date=d.strftime("%Y/%m/%d"))["tables"][0]["data"]:
            out.setdefault(r[0].strip(), {"foreign": num(r[10]), "trust": num(r[13]), "dealer": num(r[22])})
    except NotReady:
        pass   # OTC not out yet; the next run fills the day in
    return out


def margins(d):
    """Every listed and OTC stock's margin purchase and short sale balance (lots) on day d -> {code: row}."""
    data = twse("marginTrading/MI_MARGN", date=d.strftime("%Y%m%d"), selectType="ALL")
    table = next(t for t in data["tables"] if t.get("fields") and t["fields"][0] == "代號")
    out = {r[0].strip(): {"margin": num(r[6]), "short": num(r[12])} for r in table["data"]}
    try:
        for r in tpex("margin/balance", date=d.strftime("%Y/%m/%d"))["tables"][0]["data"]:
            out.setdefault(r[0].strip(), {"margin": num(r[6]), "short": num(r[14])})
    except NotReady:
        pass
    return out


def stock_chips(codes, days, today, errors):
    """Per-stock flows and margin balances for the given recent days where any stock lacks them
    -> {code: {date: row}}. Margin balances come out in the evening, so today's wait for tomorrow."""
    hist = {c: {r["date"]: r for r in read_history(c)} for c in codes}
    out = {c: {} for c in codes}
    for d in days:
        for col, fetch in (("foreign", stock_flows), ("margin", margins)):
            missing = any(not h[d.isoformat()].get(col) for h in hist.values() if d.isoformat() in h)
            if (d == today and col == "margin") or not (d == today or missing):
                continue
            try:
                for c, row in fetch(d).items():
                    if c in out:
                        out[c].setdefault(d, {"date": d}).update(row)
            except NotReady as e:
                if d == today:
                    errors.append(f"個股法人買賣超尚未公布：{e}")
    return out


def fundamentals(codes):
    """Latest monthly revenue and valuation of the given stocks -> {code: {...}}. ETFs have none."""
    out = {}
    for url in REVENUE_API:
        for r in get_json(url):
            if r["公司代號"] in codes:
                out[r["公司代號"]] = {"month": roc(r["資料年月"] + "01").isoformat()[:7],
                                     "yoy": num(r["營業收入-去年同月增減(%)"]),
                                     "mom": num(r["營業收入-上月比較增減(%)"]),
                                     "ytd": num(r["累計營業收入-前期比較增減(%)"])}
    for r in get_json(PE_TSE):
        if r["Code"] in codes:
            out.setdefault(r["Code"], {}).update(pe=num(r["PEratio"]), pb=num(r["PBratio"]),
                                                 dy=num(r["DividendYield"]))
    for r in get_json(PE_OTC):
        if (c := r["SecuritiesCompanyCode"].strip()) in codes:
            out.setdefault(c, {}).update(pe=num(r["PriceEarningRatio"]), pb=num(r["PriceBookRatio"]),
                                         dy=num(r["YieldRatio"]))
    return out


def months_back(d, n):
    """First day of d's month and the n months before it, oldest first."""
    out, m = [], d.replace(day=1)
    for _ in range(n + 1):
        out.append(m)
        m = (m - timedelta(days=1)).replace(day=1)
    return out[::-1]


def yahoo_last(sym):
    """Latest completed daily bar: (exchange-local date, close, previous close, short name)."""
    res = get_json(YAHOO.format(sym=urllib.parse.quote(sym)))["chart"]["result"][0]
    meta = res["meta"]
    tz = timezone(timedelta(seconds=meta["gmtoffset"]))
    bars = [(datetime.fromtimestamp(t, tz).date(), c)
            for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]) if c is not None]
    # For stocks Yahoo often leaves the latest day's close empty for hours after the close;
    # the real close is only in meta.regularMarketPrice.
    if meta.get("regularMarketPrice") and meta.get("regularMarketTime"):
        last = datetime.fromtimestamp(meta["regularMarketTime"], tz).date()
        if last > bars[-1][0]:
            bars.append((last, meta["regularMarketPrice"]))
    (_, prev), (d, close) = bars[-2], bars[-1]
    return d, close, prev, meta.get("shortName") or sym


def futures_quotes(night):
    """TAIEX futures (TX) quotes from TAIFEX real time API; night=True for the after-hours session.
    Returns (spot index reference, nearest-month contract row)."""
    data = get_json(TAIFEX_QUOTES, {"MarketType": "1" if night else "0", "SymbolType": "F", "KindID": "1",
                                    "CID": "TXF", "ExpireMonth": "", "RowSize": "全部", "PageNo": "",
                                    "SortColumn": "", "AscDesc": "A"})
    quotes = data["RtData"]["QuoteList"]
    spot = next(q for q in quotes if q["SymbolID"] in ("TXF-S", "TXF-P"))
    # Contract rows look like TXFK6-M (night) / TXFK6-F (day); the first one traded is the nearest month.
    near = next((q for q in quotes if re.fullmatch(r"TXF[A-L]\d-[FM]", q["SymbolID"]) and num(q["CLastPrice"])),
                None)
    if near is None:
        raise NotReady("台指期" + ("夜盤" if night else "日盤") + "沒有成交資料")
    return spot, near


# ---------- history ----------

def history_path(name):
    return DATA / f"history_{name}.csv"


def read_history(name):
    path = history_path(name)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def upsert(name, cols, rows, keys=("date",)):
    """Merge rows into data/history_<name>.csv, replacing rows with the same keys, sorted by key.
    A re-run on the same day overwrites that day instead of duplicating it."""
    merged = {tuple(r[k] for k in keys): r for r in read_history(name)}
    for r in rows:
        r = {c: fmt(r.get(c)) for c in cols}
        old = merged.get(tuple(r[k] for k in keys), {})
        # Keep values the new row lacks, e.g. institutional data on a backfilled month.
        merged[tuple(r[k] for k in keys)] = {c: r[c] if r[c] != "" else old.get(c, "") for c in cols}
    DATA.mkdir(exist_ok=True)
    with open(history_path(name), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, cols)
        w.writeheader()
        w.writerows(merged[k] for k in sorted(merged))


def fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.0f}" if v.is_integer() else f"{v:.4f}".rstrip("0")
    return str(v)


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load_latest():
    try:
        return json.loads(LATEST.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


# ---------- runs ----------

def run_pre(cfg, today, errors, names):
    """Before the open: how did the US close, and where did the TAIEX night session end?"""
    us = []
    for sym in [*US_INDEX, *cfg["us"]]:
        try:
            d, close, prev, name = yahoo_last(sym)
            names[sym] = US_INDEX.get(sym, name)
            us.append({"date": d, "symbol": sym, "close": round(close, 4), "prev_close": round(prev, 4),
                       "change_pct": round((close / prev - 1) * 100, 2)})
            print(f'  {names[sym][:14]:<14} {d}  {close:>12,.2f}  {us[-1]["change_pct"]:+.2f}%')
        except Exception as e:
            errors.append(f"美股 {sym}：{type(e).__name__}: {e}")
    upsert("us", US_COLS, us, keys=("date", "symbol"))

    try:
        spot, q = futures_quotes(night=True)
        row = {"date": datetime.strptime(q["CDate"], "%Y%m%d").date(), "session": "night",
               "contract": q["DispEName"], "last": num(q["CLastPrice"]), "ref": num(q["CRefPrice"]),
               "change": num(q["CDiff"]), "change_pct": num(q["CDiffRate"]), "volume": num(q["CTotalVolume"])}
        upsert("futures", FUT_COLS, [row], keys=("date", "session"))
        print(f'  台指期夜盤 {row["contract"]}  {row["last"]:,.0f}  {row["change_pct"]:+.2f}%  '
              f'與加權指數收盤 {num(spot["CRefPrice"]):,.2f} 價差 {row["last"] - num(spot["CRefPrice"]):+,.0f}')
    except Exception as e:
        errors.append(f"台指期夜盤：{type(e).__name__}: {e}")


def run_post(cfg, today, errors, names):
    """After the close: index, volume, institutional flows and the Taiwan watchlist."""
    # The first run backfills three months so moving averages work from day one.
    n = 0 if len(read_history("taiex")) >= 25 else 2
    try:
        rows = {}
        for m in months_back(today, n):
            rows |= taiex_month(m)
        if today not in rows:
            raise NotReady(f"證交所還沒有 {today} 的加權指數收盤")
        try:
            rows[today] |= institutional(today)
        except NotReady as e:
            errors.append(f"三大法人尚未公布：{e}")
        # Fill institutional flows for recent days that lack them (first run, or a day it was late),
        # so the foreign buying streak is right.
        have = {r["date"] for r in read_history("taiex") if r["foreign_net"]}
        for d in sorted(rows)[-10:-1]:
            if d.isoformat() not in have:
                try:
                    rows[d] |= institutional(d)
                except NotReady:
                    pass
        upsert("taiex", TAIEX_COLS, rows.values())
        t = rows[today]
        print(f'  加權指數 {t["close"]:,.2f}  {t["change"]:+,.2f}  成交 {t["value"] / 1e8:,.0f} 億')
        if "foreign_net" in t:
            print(f'  三大法人  外資 {t["foreign_net"] / 1e8:+,.1f} 億  投信 {t["trust_net"] / 1e8:+,.1f} 億  '
                  f'自營商 {t["dealer_net"] / 1e8:+,.1f} 億')
    except Exception as e:
        errors.append(f"加權指數：{type(e).__name__}: {e}")

    try:
        quotes = quotes_today(today, errors)
    except Exception as e:
        errors.append(f"個股收盤：{type(e).__name__}: {e}")
        return
    # Per-stock institutional flows for the last 5 trading days (today included) and margin balances.
    days = sorted({date.fromisoformat(r["date"]) for r in read_history("taiex")[-5:]} | {today})[-5:]
    try:
        chips = stock_chips(cfg["tw_all"], days, today, errors)
    except Exception as e:
        errors.append(f"個股籌碼：{type(e).__name__}: {e}")
        chips = {}
    # Monthly revenue and valuation change at most daily; on failure the last saved ones stay.
    try:
        fund = json.loads(FUND.read_text(encoding="utf-8")) if FUND.exists() else {}
        FUND.write_text(json.dumps(fund | fundamentals(cfg["tw_all"]), ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except Exception as e:
        errors.append(f"月營收與本益比：{type(e).__name__}: {e}")
    for code in cfg["tw_all"]:
        try:
            if code not in quotes:
                raise NotReady("今天的收盤資料裡沒有這個代號（代號錯誤或今天沒有成交）")
            names[code], market, t = quotes[code]
            rows = {today: t}
            # A stock new to the list (or with a short history) backfills three months before this one,
            # one call per month, so the 60-day average works.
            h = read_history(code)
            if not h or h[0]["date"][:7] > months_back(today, 3)[0].isoformat()[:7]:
                for m in months_back(today, 3):
                    rows = (stock_month if market == "tse" else otc_month)(code, m) | rows
            have = {r["date"] for r in h}
            for d, row in chips.get(code, {}).items():
                if d in rows or d.isoformat() in have:
                    rows[d] = rows.get(d, {}) | row
            upsert(code, STOCK_COLS, rows.values())
            print(f'  {code} {names[code]:<6} {t["close"]:>10,.2f}  {t["change"]:+,.2f}'
                  f'  成交 {t["volume"] / 1000:,.0f} 張{"（上櫃）" if market == "otc" else ""}')
        except Exception as e:
            errors.append(f"{code}：{type(e).__name__}: {e}")


# ---------- scoring ----------

def band(x, small, big):
    """-2..+2 by how far x is from zero: beyond ±big is ±2, beyond ±small is ±1."""
    if x is None:
        return None
    return 2 if x >= big else 1 if x >= small else -2 if x <= -big else -1 if x <= -small else 0


def pct_txt(p):
    return "—" if p is None else f"{p:+.2f}%"


def bars(code):
    """A Taiwan stock's history with numbers parsed, prices back-adjusted for splits and capital
    reductions: a gap beyond the ±10% daily limit can only be one (e.g. 7800 -> 2610)."""
    rows = [{k: fnum(v) if k != "date" else v for k, v in r.items()}
            for r in read_history(code) if fnum(r["close"])]
    f = 1
    for i in range(len(rows) - 1, -1, -1):
        r = rows[i]
        r.update({k: r[k] * f for k in ("open", "high", "low", "close") if r.get(k)})
        if i and abs((gap := r["close"] / f / rows[i - 1]["close"]) - 1) > 0.11:
            f *= gap
    return rows


def stock_stats(code):
    """Latest close of a Taiwan stock with its change, 5-day change and moving averages, or None."""
    h = bars(code)
    if len(h) < 2:
        return None
    c = [r["close"] for r in h]
    chg = h[-1]["change"] or 0
    return {"date": h[-1]["date"], "close": c[-1], "change": chg, "pct": chg / (c[-1] - chg) * 100,
            "pct5": (c[-1] / c[-6] - 1) * 100 if len(c) > 5 else None,
            "ma5": mean(c[-5:]), "ma20": mean(c[-20:]),
            "volume": h[-1]["volume"] or 0, "value": h[-1]["value"] or 0}


def ema(xs, n):
    k, out = 2 / (n + 1), [xs[0]]
    for x in xs[1:]:
        out.append(out[-1] + k * (x - out[-1]))
    return out


def rsi(c, n=14):
    """Wilder's RSI of the closes."""
    up = [max(b - a, 0) for a, b in zip(c, c[1:])]
    dn = [max(a - b, 0) for a, b in zip(c, c[1:])]
    au, ad = mean(up[:n]), mean(dn[:n])
    for u, d in zip(up[n:], dn[n:]):
        au, ad = (au * (n - 1) + u) / n, (ad * (n - 1) + d) / n
    return 100 if ad == 0 else 100 - 100 / (1 + au / ad)


def kd(h, n=9):
    """Taiwan-style stochastic (9, 3, 3) -> [(K, D)] per day from the n-th."""
    k = d = 50
    out = []
    for i in range(n - 1, len(h)):
        hi, lo = max(r["high"] or r["close"] for r in h[i - n + 1:i + 1]), min(r["low"] or r["close"] for r in h[i - n + 1:i + 1])
        rsv = 50 if hi == lo else (h[i]["close"] - lo) / (hi - lo) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        out.append((k, d))
    return out


def clamp(x):
    return max(-2, min(2, x))


def stock_view(code, fund):
    """A Taiwan stock read from the angles in code_artifact.md: technicals, chips and fundamentals,
    each scored -2..+2 with a short note (None when there's no data), plus an overall label and
    the same read in plain words for readers new to stocks."""
    h = bars(code)
    if len(h) < 30:
        return None
    c = [r["close"] for r in h]
    view = {}

    # Technicals: price against the 5/10/20/60-day averages, plus KD, RSI and MACD as context.
    mas = {n: mean(c[-n:]) for n in (5, 10, 20, 60) if len(c) >= n}
    above = [n for n, m in mas.items() if c[-1] > m]
    below = [n for n in mas if n not in above]
    pos = ("站上全部均線" if not below else "跌破全部均線" if not above else
           f'站上 {"、".join(map(str, above))} 日線，跌破 {"、".join(map(str, below))} 日線')
    (pk, pd), (k, d) = kd(h)[-2:]
    kd_txt = f"KD {k:.0f}/{d:.0f}" + (" 黃金交叉" if k > d and pk <= pd else " 死亡交叉" if k < d and pk >= pd else "")
    kd_txt += "（超買）" if k > 80 else "（超賣）" if k < 20 else ""
    dif = [a - b for a, b in zip(ema(c, 12), ema(c, 26))]
    osc = [a - b for a, b in zip(dif, ema(dif, 9))]
    macd = ("MACD 紅柱" if osc[-1] > 0 else "MACD 綠柱") + ("擴大" if abs(osc[-1]) > abs(osc[-2]) else "縮小")
    q = f"，季線 {mas[60]:,.2f}" if 60 in mas else ""
    view["tech"] = (round(4 * len(above) / len(mas)) - 2, f"{pos}{q}；{kd_txt}、RSI {rsi(c):.0f}、{macd}")
    a20, a60 = c[-1] > mas[20], c[-1] > mas.get(60, mas[20])
    plain = ["股價在 1 個月和 3 個月平均之上，走勢強" if a20 and a60 else
             "短線拉回，但仍在 3 個月平均之上" if a60 else
             "短線轉強，但還在 3 個月平均之下" if a20 else
             "最近幾天止穩，但還在 1 個月和 3 個月平均之下" if c[-1] > mas[5] else
             "股價跌到 1 個月和 3 個月平均以下，走勢弱"]

    # Chips: foreign + trust net buy over the last 5 days as a share of the volume, then margin.
    f5 = [r for r in h if r.get("foreign") is not None][-5:]
    if f5:
        fo, tr = sum(r["foreign"] for r in f5), sum(r["trust"] or 0 for r in f5)
        vol = sum(r["volume"] or 0 for r in f5) or 1
        share = (fo + tr) / vol * 100
        score = band(share, 3, 10)
        streak, sign = 0, 1 if f5[-1]["foreign"] > 0 else -1
        for r in reversed(f5):
            if r["foreign"] * sign <= 0:
                break
            streak += 1
        notes = [f"外資 {len(f5)} 日 {fo / 1000:+,.0f} 張、投信 {tr / 1000:+,.0f} 張（占成交量 {share:+.0f}%）"]
        if streak >= 3:
            notes.append(f'外資連{"買" if sign > 0 else "賣"} {streak} 天')
        m = [r for r in h if r.get("margin") is not None][-2:]
        if len(m) == 2:
            dm = m[1]["margin"] - m[0]["margin"]
            notes.append(f"融資 {dm:+,.0f} 張")
            # Retail buying on margin into a falling price: the chips are getting messy.
            if m[0]["margin"] and dm / m[0]["margin"] > 0.02 and (m[1]["change"] or 0) < 0:
                notes[-1] += "，股價跌融資增，籌碼凌亂"
                score -= 1
            if m[1]["margin"] and m[1]["short"] / m[1]["margin"] >= 0.3:
                notes.append(f'券資比 {m[1]["short"] / m[1]["margin"]:.0%}，留意軋空')
        view["chips"] = (clamp(score), "；".join(notes))
        plain.append(f'外資連 {streak} 天{"買進" if sign > 0 else "賣出"}' if streak >= 3 else
                     "法人近 5 天買進" if share >= 3 else "法人近 5 天賣出" if share <= -3 else "法人買賣不明顯")
        if "籌碼凌亂" in view["chips"][1]:
            plain.append("借錢買股的散戶變多，籌碼較亂")
    else:
        view["chips"] = (None, "尚無個股法人資料")

    # Fundamentals: monthly revenue growth, marked down for a rich valuation.
    if fund and fund.get("yoy") is not None:
        score = band(fund["yoy"], 10, 30)
        txt = f'{int(fund["month"][5:])} 月營收年增 {fund["yoy"]:+.1f}%（月增 {fund["mom"]:+.1f}%）'
        if fund.get("pe"):
            txt += f'，本益比 {fund["pe"]:.1f}'
            if fund["pe"] >= 60:
                txt += "，估值偏高"
                score -= 1
        view["fund"] = (clamp(score), txt)
        y, mon = fund["yoy"], int(fund["month"][5:])
        plain.append(f"{mon} 月營收比去年多 {y:.0f}%，成長強" if y >= 30 else f"{mon} 月營收比去年多 {y:.0f}%"
                     if y >= 10 else f"{mon} 月營收和去年差不多" if y > -10 else f"{mon} 月營收比去年少 {-y:.0f}%")
        if fund.get("pe") and fund["pe"] >= 60:
            plain[-1] += f'，但股價偏貴（本益比 {fund["pe"]:.0f} 倍）'
    else:
        view["fund"] = (None, "沒有營收資料（ETF 或尚未公布）")

    used = [sc for sc, _ in view.values() if sc is not None]
    avg = mean(used)
    view["label"] = "偏強" if avg >= 1 else "偏弱" if avg <= -1 else "中性"
    view["tone"] = 1 if avg >= 1 else -1 if avg <= -1 else 0
    # The overall call can disagree with the price; say so rather than leave a beginner puzzled.
    t = view["tech"][0]
    view["headline"] = view["label"] + ("，但股價還沒跟上" if avg >= 1 and t < 0 else
                                        "，但股價還撐著" if avg <= -1 and t > 0 else
                                        "，但跌深了，可能反彈" if k < 20 and avg < 1 else
                                        "，但漲多了，小心回檔" if k > 80 and avg > -1 else "")
    view["plain"] = "；".join(plain) + "。"
    return view


def latest_us():
    """{symbol: latest row}; rows are sorted by (date, symbol) so later dates win."""
    return {r["symbol"]: r for r in read_history("us")}


def score_pre(cfg, today):
    us = latest_us()
    sigs = []

    def add(key, name, value, score, note=""):
        sigs.append({"key": key, "name": name, "value": value, "score": score,
                     "weight": cfg["weights"][key], "note": note})

    def pct(sym):
        return fnum(us[sym]["change_pct"]) if sym in us else None

    def close(sym):
        return fnum(us[sym]["close"]) if sym in us else None

    def asof(sym):
        return f'{close(sym):,.2f}（{us[sym]["date"][5:].replace("-", "/")} 收盤）' if sym in us else "沒有資料"

    night = [r for r in read_history("futures") if r["session"] == "night"]
    taiex = read_history("taiex")
    if night:
        f, p = night[-1], fnum(night[-1]["change_pct"])
        note = f'{f["contract"]} {fnum(f["last"]):,.0f} 點'
        if taiex:
            note += f'，比加權指數收盤 {fnum(f["last"]) - fnum(taiex[-1]["close"]):+,.0f} 點'
        add("night_futures", "台指期夜盤", pct_txt(p), band(p, 0.3, 1.0), note)
    else:
        add("night_futures", "台指期夜盤", "—", None, "沒有資料")

    add("sox", "費城半導體", pct_txt(pct("^SOX")), band(pct("^SOX"), 0.5, 1.5), asof("^SOX"))

    note = asof("TSM")
    tw2330 = read_history("2330")
    if close("TSM") and close("TWD=X") and tw2330:
        # One ADR = 5 ordinary shares.
        adr = close("TSM") * close("TWD=X") / 5
        note += f'，換算 {adr:,.0f} 元，比 2330 收盤溢價 {(adr / fnum(tw2330[-1]["close"]) - 1) * 100:+.1f}%'
    add("tsm_adr", "台積電 ADR", pct_txt(pct("TSM")), band(pct("TSM"), 0.5, 1.5), note)

    add("nasdaq", "那斯達克", pct_txt(pct("^IXIC")), band(pct("^IXIC"), 0.5, 1.5), asof("^IXIC"))

    ai = sorted((pct(s), s) for s in cfg["us"] if pct(s) is not None)
    if ai:
        avg = mean(p for p, _ in ai)
        add("ai_stocks", "美股 AI 股平均", pct_txt(avg), band(avg, 0.5, 1.5),
            f"最強 {ai[-1][1]} {ai[-1][0]:+.2f}%，最弱 {ai[0][1]} {ai[0][0]:+.2f}%")
    else:
        add("ai_stocks", "美股 AI 股平均", "—", None, "沒有資料")

    vix, p = close("^VIX"), pct("^VIX")
    if vix is not None:
        s = -2 if vix >= 30 else -1 if vix >= 22 else 1 if vix <= 15 else 0
        if p is not None and p >= 10:   # a fear spike counts even from a low level
            s = min(s, -1)
        add("vix", "VIX 恐慌指數", f"{vix:.2f}", s, f"{pct_txt(p)}；15 以下安定、22 以上緊張、30 以上恐慌")
    else:
        add("vix", "VIX 恐慌指數", "—", None, "沒有資料")

    p = pct("TWD=X")
    add("usdtwd", "美元兌台幣", f'{close("TWD=X"):.3f}' if close("TWD=X") else "—", band(None if p is None else -p, 0.2, 0.5),
        "—" if p is None else f'{pct_txt(p)}，台幣{"升值" if p < 0 else "貶值"}')
    return sigs


def score_post(cfg, today, names):
    rows = read_history("taiex")
    if not rows or rows[-1]["date"] != today.isoformat():
        raise NotReady(f"沒有 {today} 的加權指數收盤，這次不評分")
    sigs = []

    def add(key, name, value, score, note=""):
        sigs.append({"key": key, "name": name, "value": value, "score": score,
                     "weight": cfg["weights"][key], "note": note})

    t = rows[-1]
    closes = [fnum(r["close"]) for r in rows]
    close, chg = closes[-1], fnum(t["change"])
    p = chg / (close - chg) * 100
    add("taiex_change", "加權指數", pct_txt(p), band(p, 0.5, 1.5), f"{close:,.2f}（{chg:+,.2f}）")

    ai = sorted((s["pct"], code) for code in cfg["ai_codes"]
                if (s := stock_stats(code)) and s["date"] == today.isoformat())
    if ai:
        avg = mean(p for p, _ in ai)
        up, down = sum(p > 0 for p, _ in ai), sum(p < 0 for p, _ in ai)
        add("ai_tw", "台股 AI 概念股", pct_txt(avg), band(avg, 0.5, 1.5),
            f"{len(ai)} 檔平均，上漲 {up}、下跌 {down}；最強 {names.get(ai[-1][1], ai[-1][1])} {ai[-1][0]:+.2f}%，"
            f"最弱 {names.get(ai[0][1], ai[0][1])} {ai[0][0]:+.2f}%")
    else:
        add("ai_tw", "台股 AI 概念股", "—", None, "沒有資料" if cfg["ai_codes"] else "config.toml 沒有設定 [ai_tw]")

    ma5, ma20 = mean(closes[-5:]), mean(closes[-20:])
    above = [n for n, m in (("5 日", ma5), ("20 日", ma20)) if close > m]
    add("ma", "均線位置", "站上 " + "、".join(above) if above else "跌破兩條均線",
        (1 if close > ma5 else -1) + (1 if close > ma20 else -1), f"5 日線 {ma5:,.0f}，20 日線 {ma20:,.0f}")

    values = [fnum(r["value"]) for r in rows]
    ratio = values[-1] / mean(values[-6:-1])
    add("volume", "量能", f"{ratio:.2f} 倍", ((1 if chg > 0 else -1) if ratio >= 1.2 else 0),
        f"成交 {values[-1] / 1e8:,.0f} 億，前 5 日平均 {mean(values[-6:-1]) / 1e8:,.0f} 億；1.2 倍以上算放量")

    for key, name, col, small, big in (("foreign", "外資", "foreign_net", 50, 200),
                                       ("trust", "投信", "trust_net", 20, 80)):
        v = fnum(t[col])
        add(key, name, "—" if v is None else f"{v / 1e8:+,.1f} 億", band(None if v is None else v / 1e8, small, big),
            "尚未公布" if v is None else f"買超或賣超 {small} 億以上 ±1、{big} 億以上 ±2")

    nets = [fnum(r["foreign_net"]) for r in rows]
    if nets[-1]:
        sign, n = (1 if nets[-1] > 0 else -1), 0
        for v in reversed(nets):
            if v is None or v * sign <= 0:
                break
            n += 1
        add("foreign_streak", "外資連續", f'連{"買" if sign > 0 else "賣"} {n} 天',
            sign * (2 if n >= 5 else 1 if n >= 3 else 0), "連 3 天以上 ±1、5 天以上 ±2")
    else:
        add("foreign_streak", "外資連續", "—", None, "尚未公布")
    return sigs


def summarize(cfg, sigs):
    """Weighted sum of the scored indicators -> (-100..+100, label)."""
    used = [s for s in sigs if s["score"] is not None and s["weight"] > 0]
    top = sum(2 * s["weight"] for s in used)
    score = round(100 * sum(s["score"] * s["weight"] for s in used) / top) if top else 0
    if score >= cfg["bullish"]:
        label = "強烈偏多" if score >= max(60, cfg["bullish"]) else "偏多"
    elif score <= cfg["bearish"]:
        label = "強烈偏空" if score <= min(-60, cfg["bearish"]) else "偏空"
    else:
        label = "中性"
    return score, label


def hit_rate(last_n=20):
    """How often a non-neutral call got the TAIEX direction right, over the last last_n calls.
    pre is judged against the same day's close, post against the next trading day's."""
    rows = read_history("taiex")
    move = {r["date"]: fnum(r["change"]) for r in rows}
    nxt = {a["date"]: b["date"] for a, b in zip(rows, rows[1:])}
    out = {}
    for mode in ("pre", "post"):
        calls = []
        for s in read_history("signals"):
            target = s["date"] if mode == "pre" else nxt.get(s["date"])
            if s["mode"] == mode and "中性" not in s["label"] and move.get(target):
                calls.append(fnum(s["score"]) * move[target] > 0)
        calls = calls[-last_n:]
        out[mode] = (sum(calls), len(calls))
    return out


# ---------- report ----------

def tone(x):
    return "up" if x and x > 0 else "down" if x and x < 0 else "flat"


def fmt_day(s):
    d = date.fromisoformat(s)
    return f'{d.month}/{d.day}（{"一二三四五六日"[d.weekday()]}）'


def gauge(score):
    left = (score + 100) / 2
    return (f'<div class="gauge"><div class="g-track"></div><div class="g-mark {tone(score)}" '
            f'style="left:{left:.1f}%"></div></div><div class="g-scale"><span>偏空 −100</span><span>0</span>'
            f'<span>+100 偏多</span></div>')


def signal_table(sigs):
    rows = []
    for s in sigs:
        sc = "—" if s["score"] is None else f'{s["score"]:+d}'
        w = f'{s["weight"]:g}' if s["weight"] else "不採用"
        rows.append(f'<tr><td class="name">{escape(s["name"])}</td>'
                    f'<td class="num {tone(s["score"])}">{escape(s["value"])}</td>'
                    f'<td><span class="dot {tone(s["score"])}">{sc}</span></td><td class="num mute">{w}</td>'
                    f'<td class="note">{escape(s["note"])}</td></tr>')
    return ('<div class="tbl"><table><thead><tr><th>指標</th><th>數值</th><th>評分</th><th>權重</th><th>說明</th>'
            f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>')


def chart(rows, n=60):
    """Inline SVG of the TAIEX close with its 20-day average over the last n trading days."""
    closes = [fnum(r["close"]) for r in rows]
    ma = [mean(closes[max(0, i - 19):i + 1]) if i >= 19 else None for i in range(len(closes))]
    closes, ma, rows = closes[-n:], ma[-n:], rows[-n:]
    vals = closes + [m for m in ma if m]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 1
    lo, hi = lo - pad, hi + pad
    W, H = 1000, 260

    def pts(series):
        return " ".join(f"{i * W / (len(series) - 1):.1f},{H - (v - lo) / (hi - lo) * H:.1f}"
                        for i, v in enumerate(series) if v)

    up = closes[-1] >= closes[0]
    return (f'<div class="chart"><div class="c-legend"><span><i class="ln"></i>收盤</span>'
            f'<span><i class="ln ma"></i>20 日均線</span><span class="mute">高 {max(closes):,.0f}・低 {min(closes):,.0f}</span></div>'
            f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" aria-label="加權指數近 {len(closes)} 日走勢">'
            f'<polyline class="ma" points="{pts(ma)}"/>'
            f'<polyline class="px {"up" if up else "down"}" points="{pts(closes)}"/></svg>'
            f'<div class="c-axis"><span>{fmt_day(rows[0]["date"])}</span><span>{fmt_day(rows[-1]["date"])}</span></div></div>')


def tw_row(code, names):
    s = stock_stats(code)
    name = f'<td class="name">{code} <span class="mute">{escape(names.get(code, ""))}</span></td>'
    if not s:
        return f'<tr>{name}<td colspan="6" class="mute">沒有資料</td></tr>'
    d20 = (s["close"] / s["ma20"] - 1) * 100
    return (f'<tr>{name}<td class="num">{s["close"]:,.2f}</td><td class="num {tone(s["change"])}">{s["change"]:+,.2f}</td>'
            f'<td class="num {tone(s["pct"])}">{pct_txt(s["pct"])}</td>'
            f'<td class="num {tone(s["pct5"])}">{pct_txt(s["pct5"])}</td>'
            f'<td class="num {tone(d20)}">{pct_txt(d20)}</td>'
            f'<td class="num">{s["value"] / 1e8:,.1f}</td></tr>')


TW_HEAD = ('<thead><tr><th>股票</th><th>收盤</th><th>漲跌</th><th>漲跌幅</th><th>近 5 日</th><th>距 20 日線</th>'
           '<th>成交值（億）</th></tr></thead>')


def ai_section(cfg, names):
    """Taiwan AI stocks by group, each group headed by its average change."""
    if not cfg["ai"]:
        return ""
    rows, day = [], ""
    for group, codes in cfg["ai"].items():
        stats = [s for c in codes if (s := stock_stats(c))]
        avg = mean(s["pct"] for s in stats) if stats else None
        day = day or (stats[0]["date"] if stats else "")
        rows.append(f'<tr class="grp"><td colspan="3">{escape(group)}</td>'
                    f'<td class="num {tone(avg)}">平均 {pct_txt(avg)}</td><td colspan="3"></td></tr>')
        rows += [tw_row(c, names) for c in codes]
    return (f'<section id="ai"><div class="sec-head"><h2>台股 AI 概念股</h2><span class="mute">'
            f'{fmt_day(day) if day else ""} 收盤；分組與清單在 config.toml 的 [ai_tw]</span></div>'
            f'<div class="tbl"><table>{TW_HEAD}<tbody>{"".join(rows)}</tbody></table></div></section>')


def load_fund():
    try:
        return json.loads(FUND.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def stock_groups(cfg):
    """The Taiwan stocks as shown on the page: the [ai_tw] groups, then the watch list."""
    return [*cfg["ai"].items(), *([("關注台股", cfg["tw"])] if cfg["tw"] else [])]


def spark(code, color, n=60):
    """Small inline SVG of a stock's close with its 20- and 60-day averages over the last n days,
    the price line coloured like the card's verdict so the two never disagree."""
    c = [r["close"] for r in bars(code)]
    if len(c) < 20:
        return ""
    ma = {w: [mean(c[i - w + 1:i + 1]) if i >= w - 1 else None for i in range(len(c))][-n:] for w in (20, 60)}
    c = c[-n:]
    vals = c + [v for m in ma.values() for v in m if v]
    lo, hi = min(vals), max(vals)
    W, H = 300, 72

    def pts(series):
        return " ".join(f"{i * W / (len(series) - 1):.1f},{H - 3 - (v - lo) / ((hi - lo) or 1) * (H - 6):.1f}"
                        for i, v in enumerate(series) if v)

    return (f'<svg class="spark" viewBox="0 0 {W} {H}" preserveAspectRatio="none" role="img" '
            f'aria-label="近 {len(c)} 日股價與月線、季線">'
            f'<polyline class="m60" points="{pts(ma[60])}"/><polyline class="m20" points="{pts(ma[20])}"/>'
            f'<polyline class="px {color}" points="{pts(c)}"/></svg>')


def stock_card(code, names, fund):
    """One stock: plain-language read and a small chart up front, the scored details folded away."""
    head = f'<b>{code} {escape(names.get(code, ""))}</b>'
    if not (v := stock_view(code, fund.get(code))):
        return f'<div class="stock"><div class="s-head">{head}</div><p class="mute">資料不足</p></div>'
    s = stock_stats(code)
    detail = "".join(f'<li><span class="dot {tone(sc)}">{"—" if sc is None else f"{sc:+d}"}</span>'
                     f'<b>{k}</b>{escape(txt)}</li>'
                     for k, (sc, txt) in zip(("技術面", "籌碼面", "基本面"), (v["tech"], v["chips"], v["fund"])))
    return (f'<div class="stock"><div class="s-head">{head}<span class="num {tone(s["pct"])}">'
            f'{s["close"]:,.2f}　{pct_txt(s["pct"])}</span></div>'
            f'<div class="s-label {tone(v["tone"])}">{escape(v["headline"])}</div>{spark(code, tone(v["tone"]))}'
            f'<p class="s-plain">{escape(v["plain"])}</p>'
            f'<details><summary>看細節</summary><ul>{detail}</ul></details></div>')


def view_section(cfg, names):
    """Each Taiwan stock as a card, read on technicals, chips and fundamentals (code_artifact.md)."""
    fund, parts = load_fund(), []
    for group, codes in stock_groups(cfg):
        parts.append(f'<h3>{escape(group)}</h3><div class="stocks">'
                     + "".join(stock_card(c, names, fund) for c in codes) + "</div>")
    return ('<section id="view"><div class="sec-head"><h2>個股白話分析</h2><span class="mute">'
            '小圖：<i class="key"></i>近 3 個月股價　<i class="key m20"></i>月線　<i class="key m60"></i>季線；'
            '股價在兩條線上面代表走勢強</span></div>' + "".join(parts) + '</section>')


GUIDE = [("怎麼看分數", "最上面兩張卡片是整個台股的判斷：盤前預判猜今天開盤方向，盤後評分看今天收盤強弱。"),
         ("紅漲綠跌", "台股習慣紅色代表上漲、偏強，綠色代表下跌、偏弱，和歐美相反。"),
         ("均線（月線、季線）", "最近一段時間的平均股價：月線約 20 個交易日、季線約 60 個交易日。"
          "股價在均線上面代表最近買的人多半賺錢，走勢強；在下面代表走勢弱。"),
         ("外資、投信", "外資是外國的法人機構，買賣金額最大，最能帶動大盤；投信是國內的基金公司。"
          "連續幾天買進通常代表看好。"),
         ("營收年增", "這個月營收和去年同一個月比較，看公司生意有沒有變好。"),
         ("本益比", "股價是公司一年獲利的幾倍，用來看股價貴不貴；一般 10～20 倍算合理，60 倍以上算偏貴。")]


def guide():
    items = "".join(f"<dt>{escape(t)}</dt><dd>{escape(d)}</dd>" for t, d in GUIDE)
    return f'<details class="guide"><summary>第一次看？點這裡看怎麼讀這一頁</summary><dl>{items}</dl></details>'


def watch_tables(cfg, names):
    tw = [tw_row(code, names) for code in cfg["tw"]]
    us_rows = latest_us()
    us = []
    for sym in cfg["us"]:
        r = us_rows.get(sym)
        if not r:
            us.append(f'<tr><td class="name">{sym}</td><td colspan="2" class="mute">沒有資料</td></tr>')
            continue
        p = fnum(r["change_pct"])
        us.append(f'<tr><td class="name">{sym} <span class="mute">{escape(names.get(sym, ""))}</span></td>'
                  f'<td class="num">{fnum(r["close"]):,.2f}</td><td class="num {tone(p)}">{pct_txt(p)}</td></tr>')
    us_day = next((fmt_day(us_rows[s]["date"]) for s in cfg["us"] if s in us_rows), "")
    return (ai_section(cfg, names) +
            ('<section><div class="sec-head"><h2>關注台股</h2></div>'
             f'<div class="tbl"><table>{TW_HEAD}<tbody>{"".join(tw)}</tbody></table></div></section>' if tw else "") +
            view_section(cfg, names) +
            f'<section><div class="sec-head"><h2>關注美股 AI 股</h2><span class="mute">美東 {us_day} 收盤</span></div>'
            '<div class="tbl narrow"><table><thead><tr><th>股票</th><th>收盤（美元）</th><th>漲跌幅</th></tr></thead>'
            f'<tbody>{"".join(us)}</tbody></table></div></section>')


def write_report(cfg, latest, ts, config_error=None):
    body = []
    taiex = read_history("taiex")
    chips = [f"更新 {ts[5:].replace('-', '/')}"]
    if taiex:
        t = taiex[-1]
        chg = fnum(t["change"])
        chips.append(f'加權指數 {fnum(t["close"]):,.2f}（{chg:+,.2f}）{fmt_day(t["date"])}')
    chips.append("每個交易日 08:00 盤前、15:30 盤後更新")

    if config_error:
        body.append(f'<div class="banner err"><b>設定檔有誤，本次沒有分析</b>{escape(config_error)}</div>')
    errs = [e for m in ("pre", "post") if m in latest for e in latest[m].get("errors", [])]
    if errs:
        body.append('<div class="banner warn"><b>部分資料沒抓到</b>' + "；".join(map(escape, errs)) + "</div>")

    hits = hit_rate()
    cards = []
    for mode, title, sub in (("pre", "盤前預判", "今天開盤方向"), ("post", "盤後評分", "今天盤勢強弱")):
        r = latest.get(mode)
        if not r:
            cards.append(f'<div class="card"><div class="c-title">{title}</div><div class="big flat">—</div>'
                         f'<div class="sub">還沒有資料</div></div>')
            continue
        h, n = hits[mode]
        rate = (f"近 {n} 次方向命中 {h} 次（{h / n:.0%}）" if n else "命中率：累積資料中")
        target = "當天收盤" if mode == "pre" else "隔天收盤"
        t = "up" if "多" in r["label"] else "down" if "空" in r["label"] else "flat"
        cards.append(f'<a class="card {t}" href="#{mode}"><div class="c-title">{title}'
                     f'<span class="mute">{fmt_day(r["date"])}・{sub}</span></div>'
                     f'<div class="big {t}">{r["label"]}<small>{r["score"]:+d} 分</small></div>'
                     f'{gauge(r["score"])}<div class="sub">{rate}，對照{target}</div></a>')
    body.append(guide())
    body.append(f'<div class="cards">{"".join(cards)}</div>')

    for mode, title, sub in (("pre", "盤前指標", "美股收盤與台指期夜盤"), ("post", "盤後指標", "加權指數與三大法人")):
        if mode in latest:
            body.append(f'<section id="{mode}"><div class="sec-head"><h2>{title}</h2><span class="mute">{sub}・'
                        f'{fmt_day(latest[mode]["date"])}</span></div>{signal_table(latest[mode]["signals"])}</section>')

    if len(taiex) >= 20:
        body.append(f'<section><div class="sec-head"><h2>加權指數走勢</h2></div>{chart(taiex)}</section>')
    if cfg:
        body.append(watch_tables(cfg, latest.get("names", {})))

    note = ("每個指標先依漲跌評 −2～+2 分，乘上 config.toml 的權重後加總，換算成 −100～+100 分。"
            "命中率只計算偏多或偏空的判斷，中性不計。"
            "個股白話分析：技術面看收盤站上幾條均線（5、10、20、60 日）；籌碼面看近 5 日外資加投信買賣超占成交量"
            "（3% 以上 ±1、10% 以上 ±2），股價跌而融資增加 2% 以上扣 1 分；基本面看最近一個月營收年增率"
            "（10% 以上 ±1、30% 以上 ±2），本益比 60 倍以上扣 1 分；三面平均 ≥ 1 偏強、≤ −1 偏弱。"
            "股票分割或減資的價格已還原。"
            "資料來源：臺灣證券交易所、證券櫃檯買賣中心、臺灣期貨交易所、Yahoo Finance。"
            "本頁為程式自動產生，僅供參考，不構成投資建議。")
    chip_html = "".join(f"<span>{escape(c)}</span>" for c in chips)
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text(f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>台股每日走勢</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@400;500;700;900&display=swap" rel="stylesheet">
<style>
:root {{
  --bg:#f4f5f7; --card:#ffffff; --ink:#1d1d1f; --mute:#6e6e73; --line:#e4e6eb;
  --brand:#1d4e89; --up:#d93025; --up-bg:#fde8e7; --down:#1a8a4a; --down-bg:#e3f4ea; --flat:#6e6e73;
  --warn-bg:#fff6dd; --warn-edge:#f5a623; --track:linear-gradient(90deg,#1a8a4a,#d9dce1 50%,#d93025);
  --shadow:0 1px 2px rgba(0,0,0,.04),0 4px 16px rgba(0,0,0,.05);
}}
@media (prefers-color-scheme: dark) {{ :root {{
  --bg:#121416; --card:#1c1e22; --ink:#f2f2f2; --mute:#a0a3aa; --line:#2c2f35;
  --brand:#6ea8ff; --up:#ff7b72; --up-bg:#3a1d1c; --down:#5fd38d; --down-bg:#16301f; --flat:#a0a3aa;
  --warn-bg:#33290f; --warn-edge:#f5a623; --track:linear-gradient(90deg,#5fd38d,#3a3d44 50%,#ff7b72);
  --shadow:0 1px 2px rgba(0,0,0,.3),0 4px 16px rgba(0,0,0,.25);
}} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:15px/1.55 "Noto Sans TC","PingFang TC","Microsoft JhengHei",sans-serif; -webkit-font-smoothing:antialiased; }}
.hero {{ background:linear-gradient(135deg,#0f2b5b 0%,#1d4e89 60%,#2a6fb5 100%); color:#fff; }}
.hero-in {{ max-width:1040px; margin:0 auto; padding:28px 16px 64px; }}
.brand {{ font-size:12px; font-weight:700; letter-spacing:.14em; text-transform:uppercase; opacity:.7; }}
h1 {{ font-size:28px; font-weight:900; margin:4px 0 14px; }}
.chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
.chips span {{ background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.2); border-radius:999px;
  padding:3px 12px; font-size:13px; font-weight:500; }}
main {{ max-width:1040px; margin:-44px auto 0; padding:0 16px 40px; }}
.banner {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:12px 16px;
  margin-bottom:16px; box-shadow:var(--shadow); }}
.banner b {{ margin-right:8px; }}
.banner.err {{ color:var(--up); }}
.banner.warn {{ background:var(--warn-bg); border-color:var(--warn-edge); font-size:14px; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:14px; }}
.card {{ display:block; background:var(--card); border:1px solid var(--line); border-radius:16px; padding:18px 20px;
  color:var(--ink); text-decoration:none; box-shadow:var(--shadow); border-top:4px solid var(--flat); }}
.card.up {{ border-top-color:var(--up); }} .card.down {{ border-top-color:var(--down); }}
.c-title {{ font-weight:700; display:flex; justify-content:space-between; gap:8px; flex-wrap:wrap; }}
.c-title .mute {{ font-weight:400; font-size:13px; }}
.big {{ font-size:34px; font-weight:900; margin:6px 0 10px; }}
.big small {{ font-size:15px; font-weight:500; margin-left:10px; color:var(--mute); font-variant-numeric:tabular-nums; }}
.gauge {{ position:relative; height:8px; margin:0 6px; }}
.g-track {{ position:absolute; inset:0; border-radius:999px; background:var(--track); opacity:.85; }}
.g-mark {{ position:absolute; top:-5px; width:18px; height:18px; margin-left:-9px; border-radius:50%;
  background:var(--card); border:4px solid var(--flat); }}
.g-mark.up {{ border-color:var(--up); }} .g-mark.down {{ border-color:var(--down); }}
.g-scale {{ display:flex; justify-content:space-between; font-size:11px; color:var(--mute); margin:6px 0 10px; }}
.sub {{ font-size:13px; color:var(--mute); }}
section {{ margin-top:32px; scroll-margin-top:16px; }}
.sec-head {{ display:flex; align-items:baseline; gap:12px; margin-bottom:10px; flex-wrap:wrap; }}
.sec-head .mute {{ font-size:13px; }}
h2 {{ font-size:20px; font-weight:700; margin:0; padding-left:12px; border-left:4px solid var(--brand); }}
.tbl {{ background:var(--card); border:1px solid var(--line); border-radius:16px; box-shadow:var(--shadow); overflow-x:auto; }}
.tbl.narrow {{ max-width:560px; }}
table {{ border-collapse:separate; border-spacing:0; width:100%; min-width:560px; }}
.narrow table {{ min-width:0; }}
th {{ font-size:12px; color:var(--mute); font-weight:500; text-align:left; padding:11px 14px;
  border-bottom:1px solid var(--line); white-space:nowrap; }}
td {{ padding:11px 14px; border-bottom:1px solid var(--line); vertical-align:middle; }}
tbody tr:last-child td {{ border-bottom:none; }}
tr.grp td {{ background:rgba(127,127,127,.07); font-weight:700; font-size:13px; padding:8px 14px; }}
.name {{ font-weight:500; white-space:nowrap; }}
.num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }}
.note {{ font-size:13px; color:var(--mute); min-width:240px; }}
.mute {{ color:var(--mute); }}
h3 {{ font-size:15px; font-weight:700; margin:18px 0 8px; color:var(--mute); }}
.stocks {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(290px,1fr)); gap:12px; }}
.stock {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:14px 16px;
  box-shadow:var(--shadow); min-width:0; }}
.s-head {{ display:flex; justify-content:space-between; align-items:baseline; gap:8px; flex-wrap:wrap; }}
.s-label {{ font-size:18px; font-weight:900; margin:4px 0 6px; }}
.spark {{ display:block; width:100%; height:64px; margin:4px 0 8px; }}
.spark polyline {{ fill:none; vector-effect:non-scaling-stroke; stroke-linejoin:round; }}
.spark .px {{ stroke-width:2.2; stroke:var(--flat); }} .spark .px.up {{ stroke:var(--up); }}
.spark .px.down {{ stroke:var(--down); }}
.spark .m20 {{ stroke:var(--mute); stroke-width:1.3; stroke-dasharray:4 3; }}
.spark .m60 {{ stroke:var(--brand); stroke-width:1.5; stroke-dasharray:1 3; opacity:.8; }}
.key {{ display:inline-block; width:16px; height:0; vertical-align:middle; margin:0 4px 0 2px;
  border-top:2px solid var(--ink); }}
.key.m20 {{ border-top:1.5px dashed var(--mute); }} .key.m60 {{ border-top:2px dotted var(--brand); }}
.s-plain {{ margin:0; font-size:14px; }}
details summary {{ cursor:pointer; color:var(--brand); font-size:13px; margin-top:8px; }}
.stock ul {{ list-style:none; padding:0; margin:8px 0 0; font-size:13px; color:var(--mute); }}
.stock li {{ margin-top:6px; }} .stock li b {{ color:var(--ink); margin:0 6px; font-weight:500; }}
.guide {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:10px 16px;
  margin-bottom:14px; box-shadow:var(--shadow); }}
.guide summary {{ margin:0; font-size:14px; font-weight:700; }}
.guide dl {{ margin:8px 0 2px; font-size:14px; }} .guide dt {{ font-weight:700; margin-top:10px; }}
.guide dd {{ margin:2px 0 0; color:var(--mute); }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .flat {{ color:var(--flat); }}
.dot {{ display:inline-block; min-width:36px; text-align:center; border-radius:999px; padding:1px 8px;
  font-size:13px; font-weight:700; background:rgba(127,127,127,.12); }}
.dot.up {{ background:var(--up-bg); }} .dot.down {{ background:var(--down-bg); }}
.chart {{ background:var(--card); border:1px solid var(--line); border-radius:16px; box-shadow:var(--shadow); padding:14px 18px; }}
.chart svg {{ display:block; width:100%; height:240px; overflow:visible; }}
.chart polyline {{ fill:none; vector-effect:non-scaling-stroke; stroke-linejoin:round; }}
.chart .px {{ stroke-width:2.5; }} .chart .px.up {{ stroke:var(--up); }} .chart .px.down {{ stroke:var(--down); }}
.chart .ma {{ stroke:var(--mute); stroke-width:1.5; stroke-dasharray:5 4; }}
.c-legend {{ display:flex; gap:16px; font-size:13px; margin-bottom:8px; flex-wrap:wrap; }}
.c-legend .ln {{ display:inline-block; width:18px; height:0; border-top:2.5px solid var(--brand); vertical-align:middle; margin-right:6px; }}
.c-legend .ln.ma {{ border-top:1.5px dashed var(--mute); }}
.c-axis {{ display:flex; justify-content:space-between; font-size:12px; color:var(--mute); margin-top:6px; }}
.foot {{ font-size:12px; color:var(--mute); margin-top:28px; line-height:1.8; }}
@media (max-width:560px) {{ h1 {{ font-size:24px; }} .big {{ font-size:28px; }} }}
</style></head>
<body>
<div class="hero"><div class="hero-in"><div class="brand">Taiwan Stock Daily</div>
<h1>台股每日走勢</h1><div class="chips">{chip_html}</div></div></div>
<main>
{"".join(body)}
<p class="foot">{escape(note)}</p>
</main>
</body></html>
""", encoding="utf-8")


def report_url():
    """GitHub Pages address, known only when running on GitHub Actions."""
    owner, _, repo = os.environ.get("GITHUB_REPOSITORY", "").partition("/")
    return f"https://{owner.lower()}.github.io/{repo}/" if repo else ""


def notify(cfg, latest, mode, test):
    """Email today's result. Credentials come from environment variables set by GitHub Secrets:
    SMTP_USER, SMTP_PASS, MAIL_TO (optional SMTP_HOST, SMTP_PORT; defaults to Gmail).
    Returns True when sent."""
    user, pw, to = (os.environ.get(k, "") for k in ("SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (user and pw and to):
        print("email skipped: SMTP_USER / SMTP_PASS / MAIL_TO are not set")
        return False

    r = latest[mode]
    title = "盤前預判" if mode == "pre" else "盤後評分"
    d = date.fromisoformat(r["date"])
    subject = f'{"[測試] " if test else ""}台股{title} {d.month}/{d.day}：{r["label"]}（{r["score"]:+d} 分）'
    lines = [f'{title}｜{fmt_day(r["date"])} {r["ts"][11:]}',
             f'判斷：{r["label"]}（{r["score"]:+d} 分）',
             f'分數範圍 −100～+100，≥ {cfg["bullish"]:g} 偏多、≤ {cfg["bearish"]:g} 偏空', "", "【各項指標】"]
    for sg in r["signals"]:
        sc = "—" if sg["score"] is None else f'{sg["score"]:+d}'
        lines += [f'{sg["name"]}　{sg["value"]}　評分 {sc} × 權重 {sg["weight"]:g}', f'  {sg["note"]}']

    if mode == "pre":
        us = latest_us()
        lines += ["", "【關注美股 AI 股】"]
        lines += [f'{sym:<6} {fnum(us[sym]["close"]):>10,.2f}  {pct_txt(fnum(us[sym]["change_pct"]))}'
                  for sym in cfg["us"] if sym in us]
    else:
        names = latest["names"]
        if cfg["ai"]:
            lines += ["", "【台股 AI 概念股】"]
            for group, codes in cfg["ai"].items():
                stats = [(c, s) for c in codes if (s := stock_stats(c))]
                if stats:
                    lines.append(f'{group}（平均 {pct_txt(mean(s["pct"] for _, s in stats))}）')
                    lines.append("  " + "、".join(f'{names.get(c, c)} {s["pct"]:+.2f}%' for c, s in stats))
        lines += ["", "【關注台股】"]
        for code in cfg["tw"]:
            if s := stock_stats(code):
                lines.append(f'{code} {names.get(code, "")}  {s["close"]:,.2f}  {s["change"]:+,.2f}'
                             f'（{pct_txt(s["pct"])}）')
        fund = load_fund()
        lines += ["", "【個股白話分析】走勢圖與細節見網頁"]
        for code in cfg["tw_all"]:
            if v := stock_view(code, fund.get(code)):
                lines += [f'{code} {names.get(code, "")}｜{v["headline"]}', f'  {v["plain"]}']

    h, n = hit_rate()[mode]
    lines += ["", f"近 {n} 次方向命中 {h} 次（{h / n:.0%}）" if n else "命中率：累積資料中"]
    if r["errors"]:
        lines += ["", "⚠ 部分資料沒抓到：" + "；".join(r["errors"])]
    if report_url():
        lines += ["", f"完整報表：{report_url()}"]
    lines += ["", "本信為程式自動產生，僅供參考，不構成投資建議。"]

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content("\n".join(lines))
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or 465)
    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(user, pw)
        smtp.send_message(msg)
    print(f"email sent to MAIL_TO: {subject}")
    return True


def main():
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M")
    mode = sys.argv[1] if len(sys.argv) > 1 else ("pre" if now.hour < 12 else "post")
    scheduled = os.environ.get("SCHEDULED") == "true"
    test = os.environ.get("TEST_EMAIL") == "true"
    if mode not in ("pre", "post"):
        sys.exit(f"用法：python analyze.py [pre|post]（目前是「{mode}」）")
    print(f"[{ts}] {'盤前' if mode == 'pre' else '盤後'}")
    latest = load_latest()
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"ERROR {e}")
        # A broken config shows on the page; scheduled runs exit quietly so GitHub doesn't email every day.
        write_report(None, latest, ts, str(e))
        sys.exit(0 if scheduled else 1)

    today = now.date().isoformat()
    if not is_trading_day(now.date()) and os.environ.get("FORCE") != "true":
        return
    # Several triggers (GitHub cron, cron-job.org, a later retry) may fire for the same slot;
    # once a scheduled run got everything, the rest have nothing to do.
    prev = latest.get(mode, {})
    if scheduled and not test and prev.get("date") == today and prev.get("scheduled") and not prev.get("errors"):
        print("今天這個時段已經完成，略過")
        return
    if scheduled:
        tries = latest.setdefault("tries", {})
        tries[mode] = [today, tries[mode][1] + 1 if tries.get(mode, [""])[0] == today else 1]

    errors, names = [], latest.setdefault("names", {})
    try:
        (run_pre if mode == "pre" else run_post)(cfg, now.date(), errors, names)
        sigs = score_pre(cfg, now.date()) if mode == "pre" else score_post(cfg, now.date(), names)
        score, label = summarize(cfg, sigs)
        latest[mode] = {"date": today, "ts": ts, "score": score, "label": label,
                        "signals": sigs, "errors": errors, "scheduled": scheduled}
        upsert("signals", SIGNAL_COLS, [{"date": today, "mode": mode, "score": score, "label": label}],
               keys=("date", "mode"))
        print(f"  => {label}（{score:+d} 分）")
    except NotReady as e:
        errors.append(str(e))
    except Exception:
        traceback.print_exc()
        errors.append("未預期的錯誤，這次沒有更新分數")
    write_report(cfg, latest, ts)

    # Email once per day and mode. With data missing, the first scheduled try waits for the retry
    # (e.g. institutional flows published late); the retry sends whatever it has.
    emailed = latest.setdefault("emailed", {})
    done = latest.get(mode, {}).get("date") == today
    if done and (test or (scheduled and emailed.get(mode) != today and (not errors or tries[mode][1] >= 2))):
        try:
            if notify(cfg, latest, mode, test) and not test:
                emailed[mode] = today
        except Exception:
            print("email failed (report still updated):")
            traceback.print_exc()
    LATEST.write_text(json.dumps(latest, ensure_ascii=False, indent=1), encoding="utf-8")

    for e in errors:
        print(f"  ⚠ {e}")
    print("ok" if not errors else f"{len(errors)} 個問題")
    # Scheduled runs exit quietly so GitHub doesn't email a failure for every late or missing source.
    if errors and not scheduled:
        sys.exit(1)


if __name__ == "__main__":
    main()
