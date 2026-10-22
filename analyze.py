"""Taiwan stock market daily analysis (GitHub Actions edition).

Two runs per trading day, picked by the first argument (or by the clock if omitted):
  pre   08:00  US close (indices + AI watchlist from Yahoo) and TAIEX futures night session (TAIFEX)
  post  15:30  TAIEX close, volume, institutional net buy/sell and the Taiwan watchlist (TWSE)
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
REPORT = HERE / "docs" / "index.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

HOLIDAYS_API = "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"
TWSE = "https://www.twse.com.tw/rwd/zh/{path}?response=json&{query}"
TPEX = "https://www.tpex.org.tw/www/zh-tw/afterTrading/{path}?response=json&{query}"
TAIFEX_QUOTES = "https://mis.taifex.com.tw/futures/api/getQuoteList"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"
US_INDEX = {"^SOX": "費城半導體", "^IXIC": "那斯達克", "^GSPC": "S&P 500", "^DJI": "道瓊",
            "^VIX": "VIX 恐慌指數", "TWD=X": "美元兌台幣"}
W_PRE = ("night_futures", "sox", "tsm_adr", "nasdaq", "ai_stocks", "vix", "usdtwd")
W_POST = ("taiex_change", "ai_tw", "ma", "volume", "foreign", "trust", "foreign_streak")

TAIEX_COLS = ["date", "open", "high", "low", "close", "change", "volume", "value", "transactions",
              "foreign_net", "trust_net", "dealer_net"]
STOCK_COLS = ["date", "open", "high", "low", "close", "change", "volume", "value"]
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
        for r in tpex("dailyQuotes", date=d.strftime("%Y/%m/%d"), type="EW")["tables"][0]["data"]:
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
    for r in tpex("tradingStock", code=code, date=d.strftime("%Y/%m/01"))["tables"][0]["data"]:
        rows[roc(r[0])] = {"date": roc(r[0]), "volume": num(r[1]) * 1000, "value": num(r[2]) * 1000,
                           "open": num(r[3]), "high": num(r[4]), "low": num(r[5]), "close": num(r[6]),
                           "change": num(r[7])}
    return rows


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
    for code in cfg["tw_all"]:
        try:
            if code not in quotes:
                raise NotReady("今天的收盤資料裡沒有這個代號（代號錯誤或今天沒有成交）")
            names[code], market, t = quotes[code]
            rows = {today: t}
            # A stock new to the list backfills three months, one call per month.
            if len(read_history(code)) < 25:
                for m in months_back(today, 2):
                    rows = (stock_month if market == "tse" else otc_month)(code, m) | rows
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


def stock_stats(code):
    """Latest close of a Taiwan stock with its change, 5-day change and moving averages, or None."""
    h = read_history(code)
    if len(h) < 2:
        return None
    c = [fnum(r["close"]) for r in h]
    chg = fnum(h[-1]["change"]) or 0
    return {"date": h[-1]["date"], "close": c[-1], "change": chg, "pct": chg / (c[-1] - chg) * 100,
            "pct5": (c[-1] / c[-6] - 1) * 100 if len(c) > 5 else None,
            "ma5": mean(c[-5:]), "ma20": mean(c[-20:]),
            "volume": fnum(h[-1]["volume"]) or 0, "value": fnum(h[-1]["value"]) or 0}


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
            "命中率只計算偏多或偏空的判斷，中性不計。資料來源：臺灣證券交易所、臺灣期貨交易所、Yahoo Finance。"
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
