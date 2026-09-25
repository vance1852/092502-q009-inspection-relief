# 企业合规与“无事不扰”资格服务

在企业合规档案基础服务之上，建立“无事不扰”免访资格服务：把每日完成质量、隐患闭环、
历史检查、企业主动求助与严重事件汇入按日结算的资格快照，规则经审批后带生效日期发布，
符合条件时开出有期限的免访窗口，并支持紧急突破、企业复核与自然语言的资格解释。

系统采用 Python 标准库和 SQLite，可在单个 Linux 进程中运行。

## 能力概览

### 合规档案基础服务

操作者与角色登记、场所台账、领域资料记录（compliance_fact、district_profile、
enterprise_contact、assistance_channel）、请求幂等校验与哈希串联审计。所有写操作都在
事务中完成，同一请求编号携带相同内容时返回原结果，内容变化时返回业务冲突。

### “无事不扰”资格服务

- **五类事实汇入**：`daily_completion`（每日自查完成质量与迟报）、`hazard`（隐患闭环与
  反复整改）、`inspection`（历史检查结论）、`assistance`（企业主动求助，正向信号）、
  `incident`（严重事件，硬性阻断）。
- **规则版本化**：规则必须完整给出参数，经起草、他人审批、发布三步，并带生效日期。
  新版本生效日期必须晚于当前已发布版本；只有生效日期之后新结算的快照才使用新规则，
  **旧快照不随新规则重算**。
- **按日结算封账**：每个场所每个业务日结算出一份不可变快照，记录各项闸门结论、连续合格
  天数、业务原因、输入指纹和当日吸收的更正。已封账日期重复结算直接返回原快照。
- **有期限免访窗口**：连续合格天数达到规则门槛时开出窗口，含起止日期与下次复核日；
  同一时刻同一场所至多一张在途窗口。
- **明确例外处置**：监管人员只能依据白名单原因码、并提供非空证据，才能暂停或终止窗口。
- **企业复核**：企业可对暂停/终止申请复核，监管复核后恢复（并重排下次复核日）或维持。
- **封账与迟到数据**：业务日封账后到达的数据进入更正流程（待审/通过/驳回），通过后只对
  此后的结算可见，绝不改写旧快照；快照会留痕当日吸收了哪些更正。
- **紧急事件即时突破**：严重事件在窗口生效期间实时到达时即时突破窗口以安排紧急检查，但
  窗口状态与既有资格快照都保留；处置闭环后恢复。同一事件键只产生一份突破决定。
- **并发与幂等**：进程内写事务串行化，配合唯一约束，并发结算或重复事件不会产生两份快照、
  两张窗口或两份决定；所有写接口都以 `request_id` 幂等。
- **业务原因解释**：`/qualification/explain` 用自然语言说明当前资格、免访状态、下次复核日
  及其原因、每条例外的证据，并列出待处理更正。

## 目录

- `src/relief_core/`
  - `service.py` / `storage.py` / `audit.py` / `domain.py` / `models.py` / `clock.py`：
    合规档案基础服务；
  - `qualification.py`：资格规则参数与按日结算的纯函数引擎（不接触数据库与时钟）；
  - `relief_service.py`：规则版本、事实与更正、按日结算、窗口、例外、复核与解释；
  - `api.py`：HTTP/JSON 边界；`acceptance.py`：离线端到端验收。
- `tests/`：核心规则、事务边界、接口路由、并发幂等和端到端验收测试。

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

成功时输出一行 `status` 为 `ok` 的 JSON（包含窗口开出、紧急突破后恢复、审计链有效等），
并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m relief_core.api --database relief_core.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写接口通过 `X-Actor-Id` 标识操作者，请求体携带 `request_id`。

合规档案接口：`/organizations`、`/actors`、`/sites`、`/domain-records`、`/audit-events`。

资格服务接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/rule-sets/propose` `/approve` `/publish` | 规则起草、审批（起草人不可自审）、发布 |
| GET | `/rule-sets` | 列出全部规则版本 |
| POST | `/facts` | 登记五类事实之一；封账后到达自动进入更正流程 |
| GET | `/corrections` | 列出待处理更正（可按 `site_id` 过滤） |
| POST | `/corrections/apply` `/dismiss` | 通过/驳回迟到数据更正 |
| POST | `/snapshots/settle` | 结算并封账一个业务日（已封账直接返回原快照） |
| GET | `/snapshots` `/snapshots/latest` | 查询指定日或最近一日快照 |
| GET | `/windows` | 列出场所的免访窗口 |
| POST | `/windows/suspend` `/terminate` | 凭白名单原因码与证据暂停/终止 |
| POST | `/exceptions/resolve` | 闭环一次紧急突破 |
| GET | `/exceptions` | 列出窗口的全部例外及证据 |
| POST | `/reviews/request` `/decide` | 企业申请复核、监管作出恢复/维持决定 |
| GET | `/reviews` | 查询复核申请 |
| GET | `/qualification/explain?site_id=` | 自然语言解释当前资格、下次复核日与例外证据 |

服务重启后，SQLite 中的业务状态、封账快照、窗口、例外决定与审计链继续保留。
