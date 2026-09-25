# 企业合规档案与“无事不扰”资格服务

在企业合规档案基础服务之上，建立“无事不扰”免访资格服务：把每日自查完成质量、隐患闭环、
历史检查、企业主动求助和严重/紧急事件汇入**按日结算的资格快照**；规则经审批后带生效日期
发布，旧快照不随新规则重算；合格时生成**有期限的免访窗口**，企业可申请复核，监管人员只能
依据明确例外暂停或终止窗口。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。写操作均在 `BEGIN IMMEDIATE`
事务内完成，同一请求编号携带相同内容返回原结果，内容变化返回业务冲突；所有决定写入
哈希串联审计链。

## 核心规则与不变量

- **规则版本化**：草稿（draft）须经审批（approved）才能发布（published），发布必须带不早于
  当日的生效日期；结算日只选用当日已生效的最新版本。规则以系列（series）修订，历史版本保留。
- **资格不止看连续打卡**：连续按时高质量自查天数、结算窗口内按时合格率、隐患闭环与反复整改、
  历史检查结论与问题项、严重/紧急事件一票否决共同决定；主动求助仅作正向参考，从不扣分。
- **按日快照不可变**：每个场所每个自然日至多一份快照（数据库唯一约束），重复结算只返回原快照，
  绝不使用新规则重算。
- **封账与更正**：某日一旦结算即对该日及之前“封账”。迟到的历史事实（迟报自查、补录隐患、
  补录事件等）进入更正表并标记待消费，在下一次结算时计入，已出快照不变。
- **有期限免访窗口**：快照合格且无在途窗口时开窗，窗口带到期日与下一次复核日；到期后在下次
  结算时置为 expired，合格则另开新窗。窗口在途唯一（部分唯一索引）。
- **紧急即时突破**：紧急事件登记后立即把生效中窗口置为 `breached`，但**不抹去原资格快照**；
  事件解除并通过复核后可恢复。重复事件按业务键去重，不产生第二份决定。
- **受控例外**：监管人员暂停/终止窗口必须引用明确原因码并携带非空证据：
  `EMERGENCY_DISPOSAL`、`MAJOR_HAZARD_VERIFIED`、`PUBLIC_TIP_VERIFIED`、
  `SPECIAL_CAMPAIGN`、`REPEATED_RECTIFICATION_VERIFIED`。
- **复核**：企业可对窗口状态/资格结论申请复核；复核决定为 `upheld`（维持）、`resumed`
  （恢复）、`adjusted`（调整到期日/复核日）。紧急事件未解除时不得恢复窗口。
- **自然语言解释**：`GET /qualification` 返回当前资格、窗口状态、下一次复核日，以及逐条业务
  原因和每次例外的证据、来源、决定人与解除说明。
- **并发安全**：进程内写事务串行化，跨进程由 SQLite 写锁与唯一约束共同保证并发结算、重复事件
  不产生两份决定。

## 目录

- `src/relief_core/domain.py`：基础领域资料类别；
- `src/relief_core/eligibility.py`：资格规则口径（默认值、校验）与纯函数按日评估；
- `src/relief_core/qualification_service.py`：规则审批、事实登记与封账更正、按日结算、
  免访窗口、例外与复核、资格解释视图；
- `src/relief_core/service.py`、`storage.py`、`audit.py`、`clock.py`：基础主体/场所/资料、
  SQLite 事务与建表、哈希审计链、可替换时钟；
- `src/relief_core/api.py`：HTTP/JSON 路由；`acceptance.py`：离线端到端验收；
- `tests/`：规则引擎、资格结算、封账更正、窗口例外、复核、HTTP 路由与验收测试。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m relief_core.acceptance
```

命令在临时 SQLite 数据库中走完整链路：建档 → 规则起草/审批/发布 → 自查与求助 → 按日结算
开窗 → 紧急事件即时突破（资格保留）→ 解除并复核恢复 → 封账后迟到事实进入更正流程 →
自然语言解释，核对幂等回执与审计链。成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 0
结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m relief_core.api --database relief_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查 `GET /health`；写接口通过 `X-Actor-Id` 标识操作者并要求 `request_id` 幂等键。
主要资格接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/qualification-rules` `/revise` `/approve` `/publish` | 规则起草、修订、审批、带生效日期发布 |
| GET | `/qualification-rules` | 规则版本列表（可按 `series_id` 过滤） |
| POST | `/daily-reports` | 每日自查（含提交时间、截止时间、质量；迟报自动识别） |
| POST | `/hazards` `/hazards/close` | 隐患登记与闭环（含整改次数、重大隐患） |
| POST | `/inspections` | 历史检查结论与问题项 |
| POST | `/assistances` | 企业主动求助（正向参考） |
| POST | `/serious-events` `/serious-events/resolve` | 严重/紧急事件登记与解除（紧急即时突破窗口） |
| POST | `/qualification-settlements` | 按日结算（可指定 `settlement_date`，须按日期顺序） |
| GET | `/qualification?site_id=` | 当前资格与自然语言解释 |
| GET | `/qualification-snapshots` | 快照列表或 `&date=` 指定日期（不可变） |
| GET | `/exemption-window` `/window-exceptions` `/corrections` | 窗口、例外证据、封账更正查询 |
| POST | `/window-suspensions` `/window-terminations` | 凭例外码+证据暂停/终止窗口 |
| POST | `/reviews` `/reviews/decide` | 企业申请复核与复核决定 |

服务重启后，SQLite 中的规则、事实、快照、更正、窗口、例外、复核与审计链继续保留。
