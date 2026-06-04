.PHONY: bootstrap setup start start-bg stop restart logs enable disable status backup

SERVICE := taiwan-company.service
LOG_FILE := $(CURDIR)/logs/app.log

# ── 一鍵安裝完整環境（系統套件 + Python + playwright + claude + systemd）────────
# 新裝置首次部署用這個；它是冪等的，可重複執行。
bootstrap:
	bash scripts/bootstrap.sh

# ── 只建 Python 環境（已具備系統依賴時用）──────────────────────────────────────
setup:
	@echo "── 建立 Python 虛擬環境 ──"
	uv venv
	@echo "── 安裝套件 ──"
	uv pip install -r requirements.txt
	@echo "── 下載 playwright Chromium ──"
	.venv/bin/python -m playwright install chromium
	@mkdir -p data logs
	@echo ""
	@echo "✅ 環境建立完成，執行 make start-bg 啟動（或 make start 跑 hot-reload）"
	@echo "ℹ 若 findbiz 報 libnspr4.so 缺失，補系統庫："
	@echo "   sudo .venv/bin/python -m playwright install-deps chromium"

# ── 前景 dev hot-reload ────────────────────────────────────────────────────────
# 自動暫停 systemd 避免搶 port 8003。Ctrl+C 結束後 systemd 會在 5s 內自動重啟回 production code。
start:
	@if systemctl --user is-active --quiet $(SERVICE); then \
		echo "⏸  暫停 systemd service（避免搶 port 8003，Ctrl+C 結束後它會自動回來）"; \
		systemctl --user stop $(SERVICE); \
	fi
	@echo "── 啟動台灣產業商情平台（http://localhost:8003，hot reload）──"
	.venv/bin/uvicorn main:app --reload --host 0.0.0.0 --port 8003

# ── systemd 背景模式（生產建議用法）────────────────────────────────────────────
start-bg:
	@systemctl --user start $(SERVICE)
	@sleep 2
	@if systemctl --user is-active --quiet $(SERVICE); then \
		PID=$$(systemctl --user show -p MainPID --value $(SERVICE)); \
		echo "✅ 已啟動 (PID $$PID)，瀏覽 http://localhost:8003"; \
		echo "   make logs 看日誌 / make stop 停止 / make restart 套新 code"; \
	else \
		echo "❌ 啟動失敗，systemctl --user status $(SERVICE) 看詳情"; \
		exit 1; \
	fi

stop:
	@if systemctl --user is-active --quiet $(SERVICE); then \
		systemctl --user stop $(SERVICE) && echo "✅ 已停止"; \
	else \
		echo "ℹ service 未在執行"; \
	fi

# ── 套用新 code（commit 後執行）────────────────────────────────────────────────
restart:
	@systemctl --user restart $(SERVICE)
	@sleep 2
	@if systemctl --user is-active --quiet $(SERVICE); then \
		PID=$$(systemctl --user show -p MainPID --value $(SERVICE)); \
		echo "✅ 已重啟 (PID $$PID)"; \
	else \
		echo "❌ 重啟失敗，systemctl --user status $(SERVICE) 看詳情"; \
		exit 1; \
	fi

logs:
	@tail -n 80 -F $(LOG_FILE)

# ── systemd user service 管理 ──────────────────────────────────────────────────
enable:
	@systemctl --user enable $(SERVICE) && echo "✅ 已設為開機自啟"

disable:
	@systemctl --user disable $(SERVICE) && echo "⚠ 已取消開機自啟"

status:
	@systemctl --user status $(SERVICE) --no-pager

# ── 資料備份 ───────────────────────────────────────────────────────────────────
# 立刻備份一次使用者資料（companies.json 等）。內容沒變會自動跳過。
# 平時由 taiwan-company-backup.timer 每日自動跑，這個給你手動補一份。
backup:
	@bash scripts/backup_data.sh
	@echo "── 備份清單（~/taiwan-company-backups）──"
	@ls -1t $(HOME)/taiwan-company-backups/taiwan-company-data_*.tar.gz 2>/dev/null | head -5 || echo "（尚無備份）"
