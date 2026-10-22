# 台股每日走勢

每個交易日自動分析台股，結果更新到 GitHub Pages 網頁並寄 email：

- **08:00 盤前預判**：依美股收盤（費半、那斯達克、台積電 ADR、AI 股）、台指期夜盤、VIX、匯率，判斷今天開盤偏多或偏空。
- **15:30 盤後評分**：依加權指數漲跌、均線、量能、三大法人買賣超，評估今天盤勢強弱，並追蹤 2330、0050。

每個指標評 −2～+2 分，乘上權重後換算成 −100～+100 分。網頁會統計近 20 次的方向命中率。

> 僅供參考，不構成投資建議。

## 設定

只需要修改 `config.toml`：關注的股票、各指標權重、偏多／偏空的門檻。在 GitHub 網頁上修改並 Commit 後會自動重新執行一次（不寄信）。

## 第一次在 GitHub 上設定

1. **Secrets**：Settings → Secrets and variables → Actions → New repository secret
   - `SMTP_USER`：寄件的 Gmail 地址
   - `SMTP_PASS`：Gmail 的「應用程式密碼」（不是登入密碼）
   - `MAIL_TO`：收件地址，多個用逗號分隔
2. **GitHub Pages**：Settings → Pages → Source 選 `Deploy from a branch`，Branch 選 `main`、資料夾選 `/docs`。
3. **測試**：Actions → Taiwan stock daily analysis → Run workflow，勾選「寄一封測試信」。
4. （建議）GitHub 的排程常延遲 10～30 分鐘，可以用 [cron-job.org](https://cron-job.org) 在 08:00、15:30 呼叫 workflow_dispatch API，並帶 `scheduled=true`。

## 本機執行

```sh
python -u analyze.py pre     # 盤前
python -u analyze.py post    # 盤後
```

只需要 Python 3.11 以上，不用安裝其他套件。
