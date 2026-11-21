# 并发写入观测台（隔离级别沙箱）

给数据库工程师的一个小工具：在改隔离级别之前，先看看**同一组并发写入**在不同隔离级别下会留下什么数据。

页面分两个页签：

1. **隔离级别沙箱**：两个会话（A、B）按可编辑的脚本交错执行 SQL，同一组脚本、同一份初始数据，
   分别在 **READ COMMITTED** 和 **SERIALIZABLE** 下各跑一轮，并排对比事务结局与最终数据；
2. **取件柜换绑演练**：门店调换取件柜二维码的业务场景，演练整批换绑、旧终端按码上报，
   以及两者并发时数据按“换绑前 / 换绑后”哪种绑定落库。

---

## 一、隔离级别沙箱

两个会话（A、B）按可编辑的脚本交错执行 SQL。同一组脚本、同一份初始数据，分别在
**READ COMMITTED** 和 **SERIALIZABLE** 下各跑一轮，页面上并排展示：

- 每一步的执行时间线（完成 / 出错 / 被阻塞 / 解除阻塞）；
- 每个事务的结局（已提交 / 已回滚 / 40001 中止）；
- 两轮事务都结束后，核对查询查到的最终数据。

每步有两种推进方式：

| 模式 | 含义 |
| --- | --- |
| `complete`（默认） | 等这条语句执行完成再推进下一步 |
| `blocked` | 语句在后台执行，等数据库确认它**被另一会话阻塞**（`pg_blocking_pids()`）就推进下一步；语句仍在飞，持锁会话可以继续走完自己的 `COMMIT` |

每轮有超时（默认 5 秒）：超时立即结束本轮，取消卡住的语句并**回滚未提交的事务**。
重跑总是从“初始数据 SQL”重新开始，两轮之间、两次运行之间互不影响。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8080
```

`web` 依赖 `db` 的健康检查，数据库就绪后页面即可用。端口默认 `127.0.0.1:8080`
（本工具不做任何鉴权，会原样执行你贴的 SQL，请勿暴露到公共网络）。

## 页面与脚本语法

页面上三段可编辑内容：

1. **初始数据 SQL**：每轮开始前在控制连接上执行（可多条语句），负责把库重置到相同起点；
2. **交错脚本**：见下；
3. **核对查询**：两轮事务都结束后，在新连接上执行并展示结果。

交错脚本由步骤组成，每个步骤一行头部 + 一段 SQL：

```sql
@A complete
UPDATE accounts SET balance = balance - 100 WHERE id = 1;

@B blocked
UPDATE accounts SET balance = balance - 200 WHERE id = 1;

@A complete
COMMIT;
```

- 头部：`@A` 或 `@B`，模式可写 `complete` / `blocked`（也接受 `完成` / `阻塞`），省略时是 `complete`；
- 事务由引擎按本轮隔离级别自动 `BEGIN ISOLATION LEVEL ...`，**脚本里不要写 `BEGIN`**，
  用 `COMMIT` / `ROLLBACK` 结束事务；脚本结束仍未提交的事务会被引擎回滚并记在时间线里；
- 一个步骤里可以写多条语句（结果只展示最后一条的），但建议一步一条，时间线更清晰。

## 内置样例

页面顶部可一键载入：

1. **两人同时撤班（写偏斜）**：`alice`、`bob` 两人在岗，规则是“至少留一人在岗”。
   两人同时撤班：各自确认在岗人数 ≥ 2 才撤。READ COMMITTED 下两笔都成功，
   核对查询显示 `on_duty_count = 0`——**无人值守**；SERIALIZABLE 下后提交者收到
   `40001 could not serialize access`，事务中止，仍有一人在岗。
2. **行锁等待（blocked 模式）**：两个会话更新同一行。B 用 `blocked` 模式确认被 A
   阻塞即推进，让 A 先提交。READ COMMITTED 下 B 在 A 的结果上继续（余额 700）；
   SERIALIZABLE 下 B 解除阻塞时收到 40001（余额 900）。
3. **超时回滚（演示）**：B 用 `complete` 模式等 A 的行锁，而 A 的 `COMMIT` 排在 B 之后
   永远轮不到 → 本轮超时，引擎取消卡住的语句并回滚两个事务，余额保持 1000。
   超时后立刻重跑，仍从初始数据开始。

---

## 二、取件柜换绑演练

门店给取件柜调换二维码时，旧终端还在按旧码上报柜况。这个页签在沙箱库里维护两张表：

- `locker_cabinet`：柜号（`locker_no` 唯一）与当前二维码（`code` 唯一），带最近柜况；
- `cabinet_status_report`：每次上报追加一行历史，**柜况随柜号走，换绑不清柜况**。

页面顶部可随时“重置演示数据”（4 个柜 L01~L04、4 个码 QR-A~QR-D）。

### 整批换绑（单事务，原码不符整批退回）

按柜号逐行填写**原码**与**新码**，支持：

- **交换**：如 L01 `QR-A→QR-B`、L02 `QR-B→QR-A`；
- **轮换**：如 L01→L02、L02→L03、L03→L01 的码循环；
- 新码也可以是批次外未占用的码。

整批在一个事务里完成：`SELECT ... FOR UPDATE` 锁存涉及行 → 逐行核对原码 →
校验新码不与批次外冲突 → 两段式 UPDATE（先把本批码改成事务内临时占位码释放原码，
再写目标新码，避开普通唯一索引的逐行即时检查）→ `COMMIT`。

**只要一行原码与库中不符，立即 `ROLLBACK`，整批退回、绑定一行不变**，页面红条提示并带回回滚后的状态。
静态校验（空批次、空值、批内重复柜号/新码）返回 400；库中校验失败返回 422。
同一时刻只允许一个换绑/演练请求，其余收到 409。

### 旧终端按码上报（独立连接 · ON CONFLICT）

每次上报都**新建一条独立数据库连接**（autocommit），执行：

```sql
INSERT INTO locker_cabinet (locker_no, code, last_status, last_report_at)
VALUES ('UNREG-' || ..., $code, $status, now())
ON CONFLICT (code) DO UPDATE
SET last_status    = EXCLUDED.last_status,
    last_report_at = EXCLUDED.last_report_at
RETURNING locker_no, (xmax = 0) AS inserted;
```

页面显示这次上报**实际更新的柜号**、是 INSERT 还是 UPDATE、后端进程 pid 和耗时：

- 码在库中 → `DO UPDATE`，柜况写到该码当前归属的柜号；
- **原码过期**（库中已无此码）→ 走 INSERT，落一条 `UNREG-xxxxxxxx` 未注册柜，
  页面黄色提示“原码已过期”，不会静默写到别的柜。

### 上报 × 换绑并发

固定演练 L01/L02 交换二维码，用一个上报码跑三个对照（每个子场景先重置基线）：

| 场景 | 时序 | 上报落库 |
| --- | --- | --- |
| before | 先上报，后换绑 | 按**换绑前**绑定（QR-A → L01） |
| during | 换绑事务持行锁期间发起上报 | 上报等行锁释放，按**换绑后**绑定落库（QR-A → L02） |
| after | 先换绑，后上报 | 按**换绑后**绑定（QR-A → L02） |

READ COMMITTED 下结果只有“整批换绑前”或“整批换绑后”两种绑定，不存在写丢：
换绑持有的行锁会让并发上报等待，提交后 ON CONFLICT 在最新已提交版本上继续。
若并发中的换绑因原码不符回滚，上报则按换绑前的旧绑定正常落库（自测覆盖）。

### 相关接口

| 方法/路径 | 作用 |
| --- | --- |
| `POST /api/rebind/reset` | 重置演示基线 |
| `GET  /api/rebind/state` | 当前绑定、最近柜况、上报历史 |
| `POST /api/rebind/execute` | 整批换绑（body: `{"items":[{locker_no,old_code,new_code},...]}`） |
| `POST /api/rebind/report` | 旧终端按码上报（body: `{"code","status"}`） |
| `POST /api/rebind/concurrent` | before/during/after 并发演练 |

## 自测

栈起来之后，在容器里跑端到端自测。

隔离级别沙箱自测（覆盖：写偏斜两种结局、锁等待的阻塞确认与解除、
超时回滚、超时后重跑、blocked 模式未阻塞分支、脚本解析器）：

```bash
docker compose up -d --build
docker compose exec web python tests/selftest.py
```

换绑演练自测（覆盖：整批交换/轮换成功、一行原码不符整批回滚、各类静态校验、
上报实际柜号与独立连接 pid、原码过期 INSERT 未注册柜、柜况留在原柜、
上报×换绑 before/during/after 并发、换绑回滚时并发上报按旧绑定落库、HTTP 接口冒烟）：

```bash
docker compose exec web python tests/rebind_selftest.py
```

也可以对着任意可达的 PostgreSQL 跑：

```bash
pip install -r app/requirements.txt
PGHOST=... PGDATABASE=... PGUSER=... PGPASSWORD=... python tests/selftest.py
PGHOST=... PGDATABASE=... PGUSER=... PGPASSWORD=... python tests/rebind_selftest.py
```

## 实现要点

- `app/engine.py`：执行引擎。每个会话一条连接（`autocommit=True`，事务边界完全由
  SQL 语句控制），语句在工作线程里执行；`blocked` 模式通过控制连接轮询
  `pg_blocking_pids()` 确认阻塞；超时用 `pg_cancel` 取消在飞语句；收尾先回滚
  “没有在飞语句”的事务（可能借此解除另一会话的阻塞），再等在飞语句结束。
- 事务结局以服务器命令标签为准：`COMMIT` 一个已失败的事务时 PostgreSQL 实际返回
  `ROLLBACK`，页面会如实显示为回滚；`40001` / `40P01` 记为“已中止”。
- `app/script_parser.py`：`@A complete` / `@B blocked` 头部解析，带行号报错。
- `app/main.py`：Flask 页面与 `/api/run`。所有请求共用一个沙箱库，同一时刻只允许
  一轮实验在跑（其余请求收到 409）；换绑演练有独立的 `REBIND_LOCK` 串行化写操作，
  上报路径刻意不加锁，以便与换绑事务真实并发。
- `app/rebind.py`：换绑演练。整批换绑在单事务内 `FOR UPDATE` 锁行、核对原码、
  占位码两段式 UPDATE；上报每次新建独立连接执行 `INSERT ... ON CONFLICT (code) DO UPDATE`
  并 `RETURNING` 实际柜号；并发演练用事件钩子稳定控制“持锁窗口”。
- `tests/selftest.py` / `tests/rebind_selftest.py`：两部分的端到端自测。

## 限制与说明

- 只支持两个会话；`blocked` 确认是 50ms 轮询，时间线精度有限；
- 引擎**不会**自动重试 40001（重试策略属于应用层，可在脚本里自行表达）；
- 单步可含多条语句，但只展示最后一条的结果；
- 初始数据 SQL 在 `postgres` 超级用户下执行，能力不设限——这是本地沙箱，不是多租户服务；
- 换绑演练与沙箱共用同一个库但用不同的表（`locker_cabinet` / `cabinet_status_report`），
  互不影响；未注册柜号用 `UNREG-` 前缀标识，重置演示数据即清空；
- 数据不持久化：每轮重置，容器删除即清空。
