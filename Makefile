.PHONY: setup start

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
