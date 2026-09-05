@echo off
cd /d %~dp0
echo 1688 SKU 库存快照 MVP
echo 首次使用请先安装依赖：python -m pip install -r requirements.txt
echo 并把店铺清单填入 config\shops.csv
python run.py
if errorlevel 1 pause
