# 管家观测旁路

`data/keeper_observations/YYYY-MM-DD.jsonl`记录未来复盘需要的价格来源、return窗口及模型响应后观察。日期按writer本地UTC时钟分文件。这是独立、版本化的诊断sidecar，不进入`FeatureSnapshot`、Jev原snapshot、三问、模板指纹、概率、平仓门槛或执行器。历史没有记录的时钟与覆盖不能回填。本旁路不产生任何交易所请求、不等待下一tick，不扩大订阅。

## 三种不同证据

`keeper_observation`每次复查至多一条，含原`review_started_at`、紧邻原f获取且早于历史恢复await的`request_view`，以及最终ask／retry返回后立即同步冻结的`post_response_view`。`response_completed_at`是keeper在ask返回边界读到的真实wall clock，不用原request时间或最后一次`latency_ms`冒充；重试用最终`request_id`及`attempt_count`关联原决策记录。没调用模型的无行情／guard路径不伪造响应时间。观测源、wall或sink异常不修改verdict，错误详情只保留类型。

即时缓存观察只证明capture时应用可见。价格、book、mark分别保留自己的来源事件时间及在feature.update中的本地观察时间；后者不是WebSocket接收时间。`feature_ts`仍是trade/book/mark等混合推进时钟，不能充当最后成交时钟。来源事件早于模型响应时，`source_event_after_response=false`，即使capture发生在响应后，也不能叫新的响应后报价。request缓存与原f不一致时仅标记，不重新取f或改模型输入。

`keeper_first_newer_event`才是有限的后续事件采集：最终响应注册pending，原market事件分发在既有features.update后同步通知；trade、book、mark各只选择首个`response_completed_at < source_event_ts <= observed_at`且`observed_at > response_completed_at`的应用观察。来源跑到本地未来、naive或不可用时钟不占有效首点槽。`observed_at`是应用处理该事件后的本地观察时间，不冒称网络receive或交易所成交确认。价格、双向bid/ask及数量、mark分别标注来源；它们都不是本仓可执行填价或净收益。

`visibility_receipt`由后台独立writer在样本行write+flush之后写出。`visibility_confirmed_at`是该样本已交给操作系统、可被读取的保守上界，不是首次可见的精确瞬间，也不代表fsync持久化。receipt自身之后才写入；分析按单writer文件内每条record与紧随的receipt配对，并核对request_id、source_kind及source。无模型分支request_id可为空，不许用null request_id做全局join；缺receipt或相邻记录不匹配均为unknown。writer时钟不可用或回退则此字段为null。不能用capture时间、write_started_at或ledger.closed_at替代精确落盘／成交时间。

## 返回窗口与未知

`returns.ret1m`／`ret5m`旁路直接复刻原`_price_at`选择，保留请求跨度、以`st.last_ts`为基准的target、实际选中baseline桶及价格、actual_lookback、anchor_gap、history_span、max_bucket_gap、sample_count、chronological与first-available fallback。原price桶精度为一秒，实际numerator价格的事件时钟另列。暖机不足、无历史或不完整／乱序时保持unknown／相应标志；即使旧ret为0或buy为0.5，也不能据此宣称完整覆盖或中性市场。原`_values`、`_price_at`与snapshot数值不改。

## 有界、丢弃与关闭

前台仅冻结纯值和`put_nowait`，后台单writer拥有独立文件及队列，不复用同步DecisionLog锁。默认queue上限256、pending上限64、每pending最多trade/book/mark三类首点，TTL为60秒。并非全市场逐tick落盘。pending不因仓位forget／退出就立即删除，以免漏掉退出后的首点。

TTL用monotonic计算，任何事件通知或新登记时惰性清理；全断流时不声称到点立即写超时。`keeper_observation_end`列missing_sources、dropped_sources及结束原因；容量满淘汰、队列满或writer故障均使相关证据未知。`all_sources_processed`只表示三个首点槽已处理，不表示已保存；`enqueued_receipts_required`仍需相应receipt，`unknown_due_to_queue_drop`明确不可作完整证据。后续样本／receipt附诊断drop、write_errors等计数，不修改交易角色错误统计。

关闭时只标记missing、停止接收并通知daemon writer，不等待排队或磁盘。进程立即退出可能丢失队尾与receipt；没有receipt的样本不能证明可见时间或完整保存。诊断文件失败、NaN、非JSON对象、队列满及异常回调都不能改变模型请求、重试、sleep、history、原verdict或交易调度。

这些字段只改善今后观察的可识别性。是否取得可用的双时钟响应后价格，仍取决于原有流是否到达、时钟质量、TTL和诊断交付状态；它们本身不证明策略有效，也不触发任何交易判断修改。
