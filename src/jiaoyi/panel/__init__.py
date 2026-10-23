"""面板（设计文档 §十）：局域网网页，独立进程 ``jiaoyi panel``。

- 只读主循环落盘的文件（``data/``）和 hummingbot-api 的查询接口，不导入执行层、不连币安、
  不读币安密钥。主循环没在跑时面板照样能打开，只是灯全灭。
- 分层：``files``（读 data 目录）、``hb``（只读 hummingbot-api）、``overview``（拼总览页的数据）、
  ``server``（HTTP 路由和局域网限制）、``static/``（页面；换皮只动 ``theme.css``）。
"""
