"""API 可行性測試：逐一呼叫 analyze.py 會用到的資料來源，印出是否成功與資料樣本。

本機：python -u probe.py
GitHub Actions：在 Actions 頁面手動執行 probe workflow
"""
import json
import time
import urllib.request
from datetime import date, timedelta

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

# 最近一個平日（測試用，不處理國定假日）
d = date.today()
while d.weekday() >= 5:
    d -= timedelta(days=1)
YMD = d.strftime("%Y%m%d")

def yahoo(sym):
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=1mo&interval=1d"

PROBES = [
    # 證交所 OpenAPI
    ("證交所 休市日", "https://openapi.twse.com.tw/v1/holidaySchedule/holidaySchedule"),
    ("證交所 大盤統計", "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"),
    ("證交所 個股日成交(全)", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"),
    # 證交所 網站 JSON
    ("證交所 加權指數歷史", f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?response=json&date={YMD}"),
    ("證交所 市場成交量", f"https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date={YMD}"),
    ("證交所 三大法人", f"https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&type=day&dayDate={YMD}"),
    ("證交所 2330 月成交", f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?response=json&date={YMD}&stockNo=2330"),
    ("證交所 0050 月成交", f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?response=json&date={YMD}&stockNo=0050"),
    ("證交所 即時報價", "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw|tse_2330.tw|tse_0050.tw"),
    # 期交所
    ("期交所 期貨每日行情", "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"),
    ("期交所 三大法人期貨", "https://openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate"),
    # Yahoo Finance
    ("Yahoo 費城半導體", yahoo("%5ESOX")),
    ("Yahoo 那斯達克", yahoo("%5EIXIC")),
    ("Yahoo VIX", yahoo("%5EVIX")),
    ("Yahoo 美元台幣", yahoo("TWD%3DX")),
    ("Yahoo NVDA", yahoo("NVDA")),
    ("Yahoo TSM", yahoo("TSM")),
    ("Yahoo 台股加權", yahoo("%5ETWII")),
]

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read()

def sample(body):
    try:
        data = json.loads(body)
    except ValueError:
        return "非 JSON：" + body[:120].decode("utf-8", "replace")
    if isinstance(data, list):
        return f"list[{len(data)}] 第一筆：" + json.dumps(data[0], ensure_ascii=False)[:200] if data else "空 list"
    if "chart" in data:
        res = (data["chart"].get("result") or [{}])[0]
        meta = res.get("meta", {})
        closes = res.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        return f"{meta.get('symbol')} 價格 {meta.get('regularMarketPrice')}，{len(closes)} 根日K"
    if "msgArray" in data:
        return " / ".join(f"{m.get('c')} {m.get('n')} z={m.get('z')} y={m.get('y')}" for m in data["msgArray"])
    stat = data.get("stat")
    rows = data.get("data") or data.get("tables", [{}])[0].get("data") or []
    return f"stat={stat}，{len(rows)} 列，最後一列：" + json.dumps(rows[-1] if rows else None, ensure_ascii=False)[:200]

def main():
    ok = 0
    for name, url in PROBES:
        try:
            status, body = fetch(url)
            print(f"✅ {name}  HTTP {status}  {sample(body)}")
            ok += 1
        except Exception as e:
            print(f"❌ {name}  {type(e).__name__}: {e}")
        time.sleep(1)  # 證交所有頻率限制
    print(f"\n成功 {ok}/{len(PROBES)}（測試日期 {YMD}）")

if __name__ == "__main__":
    main()
