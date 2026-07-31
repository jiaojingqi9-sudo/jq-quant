# crypto/launchers

加密交易页在**寻宝猫**里，而且 OFIM 实盘由后台任务常驻在 8503 端口
（`com.jiao.taa_futu_crypto_ofim_app` 与配套的 watchdog），不用手动启动。

| 脚本 | 什么时候用 |
| --- | --- |
| Open_Crypto_Data_Downloader.command | 下载币安历史数据。这是独立的桌面工具，app 里没有 |
| Open_Crypto_OFIM_App.command | 兜底用。加密页嵌入渲染失败时，页面上会出现「打开独立 App」按钮，调的就是它 |
