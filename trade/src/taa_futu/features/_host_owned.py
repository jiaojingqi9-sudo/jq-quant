"""宿主自有功能的说明（此文件不登记任何功能，仅作记录）。

股票交易与历史模拟的实现就在 ``dashboard_app.py`` 里，而 dashboard_app 是
Streamlit 直接执行的脚本。如果在这里写一个功能模块去 ``from taa_futu.dashboard_app
import ...``，会把那个 5700 行的模块**再完整导入一次**（一份作为被执行的脚本、
一份作为普通模块），模块级代码跑两遍，实测导致端到端测试 30 秒超时。

所以这两个功能由 dashboard_app 在自己的 main() 里用本地函数引用直接登记，
不经过发现机制。参见 dashboard_app.main() 里的 register_host_features()。
"""
