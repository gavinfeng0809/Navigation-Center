把要归档的邮件直接丢在这个目录里就行 —— **现在是自动的**。
========================================================================

丢进来之后会发生什么（不用敲任何命令）
--------------------------------------
systemd 在监听这个目录（`notice-hub-inbox.path`），文件一落地就触发处理；
另有定时器每 15 分钟兜底扫一次（防止目录事件丢失）。处理流程：

  1. `.msg` 自动转成 `.eml`（结果进 processed/，原 .msg 挪进 msg_done/）
  2. 生成静态原文页 → public/originals/<日期>-<hash>.html（含附件实体，可下载）
  3. 自动入库：正文 + 【附件】 + 【查看原文】链接，并打上「待汇总」标签
  4. 记账：data/inbox_state.json 记下已入库的 ext_id

**不会自动做的：写摘要**（关键字 / 一句话 / 要点）。那需要模型，
由 Hermes 按「待汇总」标签来补；补完把标签改掉即可。

企微群里的通知（wecom-bot）走的是**同一个标签、同一套流程** ——
所以在通知中心里，邮件和企微进来的内容看起来是一样的。

支持格式
--------
.eml / .txt / .md  直接丢
.msg               直接丢（自动转换，不用你操心）

目录说明
--------
processed/   已处理的邮件归档（.eml 留在这里，原文页靠它重建）
msg_done/    已转换的原始 .msg 归档
converted/   早期手工流程留下的 .eml（历史遗留，流水线不扫它）
README*.txt  说明文档，会被自动跳过，不会当成通知导入

手动操作（一般用不到）
----------------------
  systemctl start notice-hub-inbox.service      # 立刻跑一次
  systemctl status notice-hub-inbox.service     # 看上次执行结果
  tail -f /data/notice-hub/data/inbox-pipeline.log   # 处理日志
  python3 tools/inbox_pipeline.py --dry-run     # 只看看会做什么，不写库

两个注意
--------
1. **不要在本目录放非 README 开头的 .txt/.md 说明文件** —— 它们会被当成通知导入。
   给同事看的说明请用 `README-xxx.txt` 命名。
2. 同一封邮件重复丢不会重复入库（按 Message-ID 去重），所以可以放心多丢几次。
   这条同时也在保护已经写好的摘要不被原始正文覆盖。
