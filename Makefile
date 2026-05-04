.PHONY: setup start start-bg stop logs

PID_FILE := /tmp/taiwan-company.pid
LOG_FILE := /tmp/taiwan-company.log

setup:
	@echo "── 建立 Python 虛擬環境 ──"
	uv venv
	@echo "── 安裝套件 ──"
	uv pip install -r requirements.txt
	@mkdir -p data
	@echo ""
	@echo "✅ 環境建立完成，執行 make start 啟動"

start:
	@echo "── 啟動台灣產業商情平台（http://localhost:8000）──"
	.venv/bin/uvicorn main:app --reload --host 127.0.0.1 --port 8000

start-bg:
	@if [ -f "$(PID_FILE)" ] && kill -0 "$$(cat $(PID_FILE))" 2>/dev/null; then \
		echo "⚠ 已在背景執行 (PID $$(cat $(PID_FILE)))，先 make stop 再啟動"; \
		exit 1; \
	fi
	@echo "── 背景啟動（log: $(LOG_FILE)）──"
	@nohup .venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 \
		> $(LOG_FILE) 2>&1 & echo $$! > $(PID_FILE)
	@sleep 1
	@if kill -0 "$$(cat $(PID_FILE))" 2>/dev/null; then \
		echo "✅ 已啟動 (PID $$(cat $(PID_FILE)))，瀏覽 http://localhost:8000"; \
		echo "   make logs 看日誌 / make stop 停止"; \
	else \
		echo "❌ 啟動失敗，請查看 $(LOG_FILE)"; \
		rm -f $(PID_FILE); \
		exit 1; \
	fi

stop:
	@if [ -f "$(PID_FILE)" ]; then \
		PID=$$(cat $(PID_FILE)); \
		if kill -0 "$$PID" 2>/dev/null; then \
			kill "$$PID" && echo "✅ 已停止 (PID $$PID)"; \
		else \
			echo "ℹ PID $$PID 已不存在"; \
		fi; \
		rm -f $(PID_FILE); \
	else \
		pkill -f "uvicorn main:app" && echo "✅ 已停止（透過 pkill）" || echo "ℹ 沒有執行中的 server"; \
	fi

logs:
	@tail -n 80 -f $(LOG_FILE)
