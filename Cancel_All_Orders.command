#!/bin/bash
cd "/Users/jiao/All here/trade"
.venv/bin/python -m taa_futu.cli cancel-orders
echo ""
echo "按任意键关闭..."
read -n 1
