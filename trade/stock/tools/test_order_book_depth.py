"""
测试 FutuOpenD 实际能返回多少档盘口数据
在项目根目录运行: .venv/bin/python stock/tools/test_order_book_depth.py
"""
import sys

try:
    import futu
except ImportError:
    print("找不到 futu 包，请在项目 venv 里运行：")
    print("  source .venv/bin/activate && python stock/tools/test_order_book_depth.py")
    sys.exit(1)

HOST = "127.0.0.1"
PORT = 11111
TEST_CODE = "US.NVDA"

print(f"连接 FutuOpenD {HOST}:{PORT} ...")
ctx = futu.OpenQuoteContext(host=HOST, port=PORT)

# 必须先订阅 OrderBook 才能调用 get_order_book
ret, err = ctx.subscribe([TEST_CODE], [futu.SubType.ORDER_BOOK])
if ret != futu.RET_OK:
    print(f"订阅失败: {err}")
    ctx.close()
    sys.exit(1)
print(f"订阅成功，开始测试档位深度...\n")

import time
time.sleep(1)  # 等一秒让数据推过来

for depth in [10, 20, 40, 60, 80, 100]:
    ret, data = ctx.get_order_book(TEST_CODE, num=depth)
    if ret != futu.RET_OK:
        print(f"depth={depth:>3}  ERROR: {data}")
        break

    bid_levels = len(data.get("Bid", []))
    ask_levels = len(data.get("Ask", []))
    actual = max(bid_levels, ask_levels)
    print(f"depth={depth:>3}  请求: {depth}  实际返回: {actual} 档  {'✓ 满' if actual >= depth else f'⚠ 只给了 {actual}'}")

    if actual < depth:
        print(f"\n→ 你的权限上限大约是 {actual} 档")
        break
else:
    print(f"\n→ 100 档全部返回，权限不止 100 档，可以继续往上测")

ctx.close()
