.PHONY: help install update demo verify check test clean

PY ?= python3
export PYTHONPATH := src

help:
	@echo "00929 Buffett Investment Monitor"
	@echo ""
	@echo "  make install   安裝相依套件"
	@echo "  make update    抓取最新資料，重新評分並產生 Dashboard 與報表"
	@echo "  make demo      以合成資料產生 Dashboard（不需網路，驗證版面與計算）"
	@echo "  make verify    檢查各資料來源可否連線，列出實際回傳的科目名稱"
	@echo "  make check     驗證設定檔與指標實作是否一致"
	@echo "  make test      執行測試（不需網路）"
	@echo ""
	@echo "設定 FINMIND_TOKEN 環境變數可取得 5–10 年歷史資料；"
	@echo "未設定時系統仍可執行，但長期指標會標示「資料不足」。"

install:
	$(PY) -m pip install -e ".[dev]"

update:
	$(PY) -m buffett00929.cli update

demo:
	$(PY) -m buffett00929.cli demo

verify:
	$(PY) -m buffett00929.cli verify-sources

check:
	$(PY) -m buffett00929.cli check-config

test:
	$(PY) -m pytest -q

clean:
	rm -rf data/raw/* .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
