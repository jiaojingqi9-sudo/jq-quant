"""下单练习台：合成市场 + 撮合 + 计分 + 界面。

模块分工：
    book.py      订单簿与撮合（价格优先、同价先到先得）
    market.py    合成市场引擎（做市商 + 噪音 / 价值 / 动量三类机器人）
    session.py   一局练习：任务、下单接口、收工评分
    panel.py     Streamlit 界面
    calibrate.py / merge_calibration.py   从真实盘口数据量参数（先跑这两个）

参数是拿自己收集的 NVDA 真实盘口标定的，验收口径与实测结果见
docs/execution_trainer_v1_notes.md。
"""
