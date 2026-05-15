# 台灣產業商情平台

前後端統一由 systemd user service 管理，**開機自動啟動、crash 自動重啟**。

## 服務管理

```bash
# 查狀態
make status                             # 或 systemctl --user status taiwan-company

# 重啟（改完 code 後）
systemctl --user restart taiwan-company

# 查 log
tail -f /home/jacktsai/taiwan-company/logs/app.log
journalctl --user -u taiwan-company -f
```

開啟瀏覽器：http://localhost:8003

## 驗證有沒有跑起來

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8003/health
# 回 200 就 OK
```

## 首次安裝（換裝置）

```bash
make setup                              # 建立虛擬環境
systemctl --user daemon-reload
make enable                             # 設為開機自啟
systemctl --user start taiwan-company
```

## 前景開發模式（含 hot reload）

```bash
systemctl --user stop taiwan-company   # 先停掉 service
make start                             # 前景啟動
```
