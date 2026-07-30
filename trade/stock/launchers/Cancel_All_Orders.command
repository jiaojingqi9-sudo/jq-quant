#!/bin/bash
cd "$(cd "$(dirname "$0")/../.." && pwd)"
.venv/bin/python -m taa_futu.cli cancel-orders
echo ""
echo "按任意键关闭..."
read -n 1
