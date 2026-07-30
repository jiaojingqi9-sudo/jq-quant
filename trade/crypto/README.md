# Crypto System

这里放加密货币侧的用户入口、独立 App、文档和辅助工具。

当前加密货币交易系统是独立的 Crypto OFIM Binance 插头。核心代码仍保留在 `src/taa_futu/crypto_ofim.py` 和 `src/taa_futu/crypto_ofim_app.py`，运行数据保留在 `runtime/crypto_ofim/`。这样能保持现有导入路径稳定，同时把你日常会点开的东西整理到 `crypto/` 下。

常用入口：

- `crypto/apps/Crypto OFIM Binance.app`：Crypto OFIM 一体化 App，控制和监控在同一个页面
- `crypto/launchers/Open_Crypto_OFIM_App.command`：打开 Crypto OFIM 独立 App
- `crypto/launchers/Open_Crypto_Data_Downloader.command`：打开加密数据下载器
- `crypto/apps/Crypto Data Downloader.app`：加密数据下载器 App 包
- `crypto/docs/crypto-ofim-binance.md`：Binance 模拟盘 API、权限和使用说明
- `crypto/tools/`：加密数据抓取和下载辅助工具

桌面快捷方式：

- `~/Desktop/Crypto OFIM Binance.app`（本机桌面快捷方式；`.app` 包是本机生成的，不进仓库）

这个桌面 App 已经写死项目根目录，不依赖它自己所在位置，所以项目内文件整理后仍然可以直接双击。
