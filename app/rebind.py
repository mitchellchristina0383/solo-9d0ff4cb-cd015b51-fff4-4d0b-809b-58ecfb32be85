"""取件柜二维码换绑演练：整批原子换绑 + 旧终端上报。

场景
----
门店给取件柜调换二维码时，旧终端仍按“原码”上报柜况。本模块在沙箱库里维护
两张表：

* ``locker_cabinet``        柜号与当前绑定二维码（code 唯一），保留最近一次柜况；
* ``cabinet_status_report`` 每次终端上报追加一行历史，柜况随柜号走。

换绑（``rebind``）
------------------
按柜号提交一批 (柜号, 原码, 新码)，在**单个事务**里完成：

1. 校验整批请求：柜号存在、没有重复柜号；
2. 锁存全部涉及柜号的行（``SELECT ... FOR UPDATE``）；
3. 逐行核对原码——只要一行原码与库中不符，``ROLLBACK``，整批退回，
   任何绑定都不改变；
4. 校验新码：非空、本批内不重复、且不与批次外柜号已绑定的码冲突；
5. 两段式 UPDATE 完成整批换绑（先把本批码改成事务内临时占位码释放原码，
   再写入目标新码；避开普通唯一索引的逐行即时检查，交换/轮换都支持），
   ``COMMIT``。全程同一事务，失败整体回滚。

上报（``report``）
------------------
模拟旧终端，用**每次调用新建的独立连接**（autocommit）执行：

    INSERT INTO locker_cabinet (code, ...)
    VALUES (...)
    ON CONFLICT (code) DO UPDATE SET 柜号=EXCLUDED.柜号之外的柜况字段 ...
    RETURNING locker_no

网页据此显示“这次上报实际更新的柜号”。原码已不在库中时走 INSERT 分支，
会以未注册柜号落一条新行（返回 ``unregistered`` 标记），页面明确提示
“原码已过期（库中无此码）”。

并发（``concurrent_demo``）
--------------------------
上报与换绑并发时，READ COMMITTED 下结果只有两种，按上报落库瞬间绑定关系
是换绑前还是换绑后而定：

* 上报在换绑事务提交**之前**落库 → 按换绑前绑定写入；
* 上报在换绑事务提交**之后**落库 → 按换绑后绑定写入。

换绑持有的行锁不会让上报“写丢”：冲突时上报等待换绑提交，再在新绑定上
继续（ON CONFLICT DO UPDATE 读最新已提交版本），天然落在“换绑后”。

已有柜况（``last_status`` / 历史表）只跟柜号走，换绑不触碰柜况列。
"""
from __future__ import annotations

import threading
import time

import psycopg2
from psycopg2 import errors

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS locker_cabinet (
    id          int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    locker_no   text NOT NULL UNIQUE,
    -- 普通唯一索引：既是最终唯一性保证，也是 INSERT ... ON CONFLICT (code)
    -- 的仲裁器（PG 不允许可延迟约束充当 ON CONFLICT 仲裁器）。
    -- 交换/轮换通过“临时占位码”两段式 UPDATE 实现，逐行也不撞唯一约束。
    code        text NOT NULL UNIQUE,
    last_status text,
    last_report_at timestamptz
);

CREATE TABLE IF NOT EXISTS cabinet_status_report (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    locker_no   text NOT NULL,
    code        text NOT NULL,
    status      text NOT NULL,
    reported_at timestamptz NOT NULL DEFAULT now()
);
"""

# 旧版表可能被改成过可延迟约束，恢复为普通唯一约束（配合占位码两段式换绑）
_MIGRATE_SQL = """
DO $$
DECLARE
    con_name text;
BEGIN
    SELECT conname INTO con_name
    FROM pg_constraint
    WHERE conrelid = 'locker_cabinet'::regclass
      AND contype = 'u'
      AND conkey = (
          SELECT array_agg(attnum ORDER BY attnum)
          FROM pg_attribute
          WHERE attrelid = 'locker_cabinet'::regclass AND attname = 'code'
      )
      AND condeferrable;
    IF con_name IS NOT NULL THEN
        EXECUTE 'ALTER TABLE locker_cabinet DROP CONSTRAINT ' || quote_ident(con_name);
        ALTER TABLE locker_cabinet ADD CONSTRAINT locker_cabinet_code_key UNIQUE (code);
    END IF;
END $$;
"""

# 演示基线：4 个柜、4 个码；初始柜况证明“柜况留在原柜”。
DEFAULT_LOCKERS = [
    ("L01", "QR-A", "正常"),
    ("L02", "QR-B", "正常"),
    ("L03", "QR-C", "柜门将开"),
    ("L04", "QR-D", "正常"),
]


class RebindError(Exception):
    """换绑业务校验失败（整批退回）。"""


# ---------------------------------------------------------------- 基础工具


def _connect(conninfo):
    """每次调用独立连接：上报路径刻意不与 Web 工作连接复用。"""
    conn = psycopg2.connect(**conninfo)
    conn.autocommit = True
    return conn


def ensure_schema(conninfo):
    conn = _connect(conninfo)
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(_MIGRATE_SQL)
    finally:
        conn.close()


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def reset_demo(conninfo):
    """重置为演示基线数据。"""
    conn = _connect(conninfo)
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE cabinet_status_report, locker_cabinet RESTART IDENTITY")
            for locker_no, code, status in DEFAULT_LOCKERS:
                cur.execute(
                    "INSERT INTO locker_cabinet (locker_no, code, last_status) "
                    "VALUES (%s, %s, %s)",
                    (locker_no, code, status),
                )
    finally:
        conn.close()


def get_state(conninfo):
    """返回当前绑定、最近柜况与上报历史。"""
    conn = _connect(conninfo)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT locker_no, code, last_status, last_report_at "
                "FROM locker_cabinet ORDER BY locker_no"
            )
            lockers = [
                {"locker_no": r[0], "code": r[1], "last_status": r[2],
                 "last_report_at": _iso(r[3])}
                for r in cur.fetchall()
            ]
            cur.execute(
                "SELECT locker_no, code, status, reported_at "
                "FROM cabinet_status_report ORDER BY id DESC LIMIT 30"
            )
            reports = [
                {"locker_no": r[0], "code": r[1], "status": r[2],
                 "reported_at": _iso(r[3])}
                for r in cur.fetchall()
            ]
        return {"lockers": lockers, "reports": reports}
    finally:
        conn.close()


# ---------------------------------------------------------------- 整批换绑


def _validate_items(items):
    """请求体 → 规整后的列表，做不依赖数据库的静态校验。"""
    if not isinstance(items, list) or not items:
        raise RebindError("换绑批次为空：至少提交一行 (柜号, 原码, 新码)")
    clean = []
    seen_lockers: set[str] = set()
    seen_new_codes: set[str] = set()
    for i, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            raise RebindError(f"第 {i} 行格式不正确")
        locker_no = (str(raw.get("locker_no") or "")).strip()
        old_code = (str(raw.get("old_code") or "")).strip()
        new_code = (str(raw.get("new_code") or "")).strip()
        if not locker_no or not old_code or not new_code:
            raise RebindError(f"第 {i} 行存在空值：柜号、原码、新码都必须填写")
        if locker_no in seen_lockers:
            raise RebindError(f"柜号 {locker_no} 在本批中出现了多次")
        if new_code in seen_new_codes:
            raise RebindError(f"新码 {new_code} 在本批中被多个柜号使用，无法唯一绑定")
        seen_lockers.add(locker_no)
        seen_new_codes.add(new_code)
        clean.append((locker_no, old_code, new_code))
    return clean


def rebind(conninfo, items, lock_event=None, hold_s=0.0):
    """整批原子换绑。

    成功返回映射明细；任一原码不符或其他校验失败则抛 :class:`RebindError`，
    调用方保证事务已回滚、绑定不变。

    ``lock_event`` / ``hold_s`` 仅并发演练使用：拿到行锁后 set 事件，
    并把事务多持锁 ``hold_s`` 秒，制造与上报并发的窗口。
    """
    clean = _validate_items(items)
    locker_nos = [c[0] for c in clean]

    conn = _connect(conninfo)
    conn.autocommit = False  # 整批一个事务，失败整体回滚
    try:
        with conn.cursor() as cur:
            cur.execute("BEGIN")

            # 1) 锁存涉及行（按柜号排序加锁，避免与其他换批死锁）
            cur.execute(
                "SELECT locker_no, code FROM locker_cabinet "
                "WHERE locker_no = ANY(%s) ORDER BY locker_no FOR UPDATE",
                (locker_nos,),
            )
            rows = cur.fetchall()
            found = {r[0]: r[1] for r in rows}
            missing = [n for n in locker_nos if n not in found]
            if missing:
                raise RebindError("柜号不存在：" + ", ".join(missing))

            if lock_event is not None:
                lock_event.set()
            if hold_s:
                time.sleep(hold_s)

            # 2) 逐行核对原码：一行不符，整批退回
            for locker_no, old_code, _ in clean:
                if found[locker_no] != old_code:
                    raise RebindError(
                        f"柜号 {locker_no} 的原码与库中不符"
                        f"（提交 {old_code}，库中 {found[locker_no]}），整批换绑退回"
                    )

            # 3) 新码不得与“批次外”柜号已绑定的码冲突
            new_codes = [c[2] for c in clean]
            cur.execute(
                "SELECT code, locker_no FROM locker_cabinet "
                "WHERE code = ANY(%s) AND NOT (locker_no = ANY(%s))",
                (new_codes, locker_nos),
            )
            clash = cur.fetchall()
            if clash:
                detail = "；".join(f"码 {c} 已绑柜 {n}" for c, n in clash)
                raise RebindError("新码与批次外的已有绑定冲突：" + detail)

            # 4) 两段式换绑：code 是普通唯一索引（ON CONFLICT 仲裁器要求），
            #    直接交换会在逐行更新时撞唯一约束。先把本批码全部改成事务内
            #    唯一的临时占位码释放原码，再写入目标新码。全部在同一事务内，
            #    提交前对外不可见，失败整体回滚。
            placeholder = f"__rebind_{int(time.time() * 1000)}_{id(conn) % 100000}__"
            for idx, (locker_no, _old, _new) in enumerate(clean):
                cur.execute(
                    "UPDATE locker_cabinet SET code = %s WHERE locker_no = %s",
                    (f"{placeholder}{idx}", locker_no),
                )
            affected = 0
            for locker_no, _old, new_code in clean:
                cur.execute(
                    "UPDATE locker_cabinet SET code = %s WHERE locker_no = %s",
                    (new_code, locker_no),
                )
                affected += cur.rowcount

            cur.execute("COMMIT")
    except RebindError:
        conn.rollback()
        raise
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise RebindError(f"换绑后出现重复码，整批退回（{exc.diag.message_primary}）")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "ok": True,
        "affected": affected,
        "mapping": [
            {"locker_no": n, "old_code": o, "new_code": w} for n, o, w in clean
        ],
    }


# ---------------------------------------------------------------- 旧终端上报


def report(conninfo, code, status):
    """旧终端按码上报柜况。独立连接 + INSERT ... ON CONFLICT (code) DO UPDATE。

    返回上报实际落到的柜号；库中无此码时 INSERT 一条未注册柜号，
    并显式标记 ``unregistered``（即“原码已过期”）。
    """
    code = (str(code or "")).strip()
    status = (str(status or "")).strip()
    if not code:
        raise RebindError("上报码不能为空")
    if not status:
        status = "正常"

    conn = _connect(conninfo)  # 刻意独立连接，不复用换绑/页面连接
    pid = conn.get_backend_pid()
    t0 = time.monotonic()
    try:
        with conn.cursor() as cur:
            # 标准 upsert：INSERT ... ON CONFLICT (code) DO UPDATE。
            # 库中无此码（原码已过期）→ 走 INSERT，落 UNREG- 未注册柜；
            # 码已存在 → 走 DO UPDATE，只改柜况、不动 locker_no。
            # RETURNING 取回这次上报实际更新的柜号，xmax=0 区分插入/更新。
            cur.execute(
                """
                INSERT INTO locker_cabinet (locker_no, code, last_status, last_report_at)
                VALUES ('UNREG-' || substr(md5(random()::text), 1, 8), %s, %s, now())
                ON CONFLICT (code) DO UPDATE
                SET last_status    = EXCLUDED.last_status,
                    last_report_at = EXCLUDED.last_report_at
                RETURNING locker_no, (xmax = 0) AS inserted
                """,
                (code, status),
            )
            locker_no, inserted = cur.fetchone()
            action = "insert" if inserted else "update"
            unregistered = locker_no.startswith("UNREG-")
            cur.execute(
                "INSERT INTO cabinet_status_report (locker_no, code, status) "
                "VALUES (%s, %s, %s)",
                (locker_no, code, status),
            )
    finally:
        conn.close()

    return {
        "ok": True,
        "code": code,
        "status": status,
        "locker_no": locker_no,
        "action": action,
        "unregistered": unregistered,
        "expired": unregistered,  # 语义别名：库中无此码 = 原码已过期
        "pid": pid,
        "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
    }


# ---------------------------------------------------------------- 并发演练


def _state_map(conninfo):
    return {row["locker_no"]: row for row in get_state(conninfo)["lockers"]}


def concurrent_demo(conninfo, code, status):
    """上报与整批换绑并发，跑 before / during / after 三个对照。

    每个子场景都先重置基线，换绑内容固定为 L01/L02 交换二维码。
    返回上报实际落到的柜号，以及落库瞬间相对换绑事务的时序。
    """
    results = []

    # ---- 场景 1：上报在换绑开始前完成（按换绑前绑定） ----
    reset_demo(conninfo)
    before = report(conninfo, code, status)
    rebind(conninfo, [
        {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-B"},
        {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-A"},
    ])
    before_state = _state_map(conninfo)
    results.append({
        "scenario": "before",
        "label": "先上报，后换绑",
        "report": before,
        "landed_locker": before["locker_no"],
        "bound_at_land": "换绑前",
        "after_rebind_owner": next(
            (n for n, r in before_state.items() if r["code"] == code), None),
    })

    # ---- 场景 2：上报与换绑并发（换绑持锁期间发起） ----
    reset_demo(conninfo)
    during = {}
    locked = threading.Event()   # 换绑事务已拿到行锁
    go_on = threading.Event()    # 放行换绑事务继续提交
    HOLD_LOCK_S = 0.3            # 稳定持锁窗口，保证上报确实经历等锁

    def on_locked():
        locked.set()
        go_on.wait(timeout=10)
        time.sleep(HOLD_LOCK_S)  # 放行后仍持锁 300ms，制造确定性并发窗口

    t = threading.Thread(
        target=lambda: during.update(
            _safe(lambda: _rebind_with_hook(conninfo, on_locked), "rebind")),
        daemon=True,
    )
    t.start()
    locked.wait(timeout=10)   # 等换绑事务拿到行锁、尚未提交
    go_on.set()               # 通知钩子；钩子再持锁 HOLD_LOCK_S 后才返回
    during_report = report(conninfo, code, status)  # 持锁窗口内发起 → 等锁，提交后落库
    t.join(timeout=10)
    during_state = _state_map(conninfo)
    results.append({
        "scenario": "during",
        "label": "上报与换绑并发（换绑持锁时发起）",
        "report": during_report,
        "landed_locker": during_report["locker_no"],
        # 并发上报等行锁释放后在换绑提交的新绑定上继续，必然落在换绑后
        "bound_at_land": "换绑后（等行锁释放后继续）",
        "waited_for_lock": during_report["elapsed_ms"],
        "after_rebind_owner": next(
            (n for n, r in during_state.items() if r["code"] == code), None),
        "rebind": during.get("rebind"),
        "rebind_error": during.get("error"),
    })

    # ---- 场景 3：换绑完成后再上报（按换绑后绑定） ----
    reset_demo(conninfo)
    rebind(conninfo, [
        {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-B"},
        {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-A"},
    ])
    after = report(conninfo, code, status)
    after_state = _state_map(conninfo)
    results.append({
        "scenario": "after",
        "label": "先换绑，后上报",
        "report": after,
        "landed_locker": after["locker_no"],
        "bound_at_land": "换绑后",
        "after_rebind_owner": next(
            (n for n, r in after_state.items() if r["code"] == code), None),
    })

    return {"ok": True, "code": code, "status": status, "results": results,
            "final_state": {"lockers": list(after_state.values())}}


def _rebind_with_hook(conninfo, after_lock):
    """带“拿到行锁后钩子”的换绑，仅供并发演练控制时序。"""
    return rebind(conninfo, [
        {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-B"},
        {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-A"},
    ], lock_event=_EventHook(after_lock), hold_s=0)


class _EventHook:
    """把 rebind 的 lock_event 接口（threading.Event）适配成回调。"""

    def __init__(self, fn):
        self._fn = fn

    def set(self):
        self._fn()


def _safe(fn, key):
    try:
        return {key: fn()}
    except Exception as exc:  # 演练要把失败也带回页面
        return {"error": str(exc)}
