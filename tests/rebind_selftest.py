"""取件柜换绑演练：端到端自测。

覆盖：
  1. 重置演示数据；
  2. 整批交换换绑成功（唯一约束下的两码互换）；
  3. 一行原码不符 → 整批回滚、绑定不变；
  4. 柜号不存在 / 新码与批次外冲突 / 批内新码重复 → 退回；
  5. 旧终端上报：ON CONFLICT 实际更新的柜号、独立连接 pid；
  6. 原码过期：库中无此码 → INSERT 未注册柜并标记 expired；
  7. 柜况留在原柜：换绑不清 last_status，历史按柜号留存；
  8. 上报 × 换绑并发：before/during/after 三场景落库绑定；
  9. HTTP 层（Flask test_client）接口冒烟。

用法：
  docker compose up -d --build 之后：
      docker compose exec web python tests/rebind_selftest.py
  或对着任意可达的 PostgreSQL：
      PGHOST=... PGPORT=... PGDATABASE=... PGUSER=... PGPASSWORD=... python tests/rebind_selftest.py
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import psycopg2  # noqa: E402

import rebind  # noqa: E402

FAILURES = []


def conninfo():
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "dbname": os.environ.get("PGDATABASE", "sandbox"),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "connect_timeout": 10,
    }


CI = conninfo()


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def code_of(state, locker_no):
    return next(r["code"] for r in state["lockers"] if r["locker_no"] == locker_no)


def status_of(state, locker_no):
    return next(r["last_status"] for r in state["lockers"] if r["locker_no"] == locker_no)


def expect_rebind_error(fn, needle):
    try:
        fn()
        check(f"应被退回（{needle}）", False, "未抛 RebindError")
    except rebind.RebindError as exc:
        check(f"应被退回（{needle}）", needle in str(exc), str(exc))


def test_reset_and_swap():
    print("== 重置与整批交换换绑 ==")
    rebind.ensure_schema(CI)
    rebind.reset_demo(CI)
    state = rebind.get_state(CI)
    check("基线 4 个柜", len(state["lockers"]) == 4, str(len(state["lockers"])))
    check("基线 L01=QR-A / L02=QR-B",
          code_of(state, "L01") == "QR-A" and code_of(state, "L02") == "QR-B")

    result = rebind.rebind(CI, [
        {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-B"},
        {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-A"},
    ])
    check("换绑影响 2 行", result["affected"] == 2, str(result["affected"]))
    state = rebind.get_state(CI)
    check("交换后 L01=QR-B / L02=QR-A",
          code_of(state, "L01") == "QR-B" and code_of(state, "L02") == "QR-A")
    check("L03/L04 绑定不变",
          code_of(state, "L03") == "QR-C" and code_of(state, "L04") == "QR-D")


def test_rollback_on_wrong_old_code():
    print("== 一行原码不符，整批退回 ==")
    rebind.reset_demo(CI)
    before = rebind.get_state(CI)
    expect_rebind_error(
        lambda: rebind.rebind(CI, [
            {"locker_no": "L01", "old_code": "QR-WRONG", "new_code": "QR-B"},
            {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-A"},
        ]),
        "原码与库中不符",
    )
    after = rebind.get_state(CI)
    check("回滚后绑定完全不变",
          [(r["locker_no"], r["code"]) for r in before["lockers"]]
          == [(r["locker_no"], r["code"]) for r in after["lockers"]])


def test_other_validation():
    print("== 其他整批校验 ==")
    rebind.reset_demo(CI)
    expect_rebind_error(
        lambda: rebind.rebind(CI, [
            {"locker_no": "L99", "old_code": "QR-A", "new_code": "QR-X"}]),
        "柜号不存在")
    expect_rebind_error(
        lambda: rebind.rebind(CI, [
            {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-C"}]),
        "批次外")
    expect_rebind_error(
        lambda: rebind.rebind(CI, [
            {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-X"},
            {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-X"}]),
        "多个柜号使用")
    expect_rebind_error(
        lambda: rebind.rebind(CI, [
            {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-B"},
            {"locker_no": "L01", "old_code": "QR-B", "new_code": "QR-A"}]),
        "出现了多次")
    expect_rebind_error(lambda: rebind.rebind(CI, []), "批次为空")
    # 全部失败后基线仍在
    state = rebind.get_state(CI)
    check("校验失败后基线不变", code_of(state, "L01") == "QR-A")


def test_report_and_pid():
    print("== 旧终端上报：实际更新柜号与独立连接 ==")
    rebind.reset_demo(CI)
    r = rebind.report(CI, "QR-A", "满格")
    check("QR-A 上报落到 L01", r["locker_no"] == "L01", str(r))
    check("动作是 ON CONFLICT UPDATE", r["action"] == "update", r["action"])
    check("返回独立连接 pid", isinstance(r["pid"], int) and r["pid"] > 0, str(r.get("pid")))
    state = rebind.get_state(CI)
    check("L01 最近柜况已更新为满格", status_of(state, "L01") == "满格")
    check("L02 柜况未被波及", status_of(state, "L02") == "正常")
    latest = state["reports"][0]
    check("历史表按柜号 L01 留存", latest["locker_no"] == "L01" and latest["code"] == "QR-A")

    # 第二次上报确认复用同一行（不会新增柜）
    n_before = len(state["lockers"])
    rebind.report(CI, "QR-A", "正常")
    n_after = len(rebind.get_state(CI)["lockers"])
    check("重复上报不新增柜行", n_before == n_after, f"{n_before} -> {n_after}")


def test_expired_code():
    print("== 原码过期：库中无此码 ==")
    rebind.reset_demo(CI)
    r = rebind.report(CI, "QR-OLD", "离线")
    check("走 INSERT 分支", r["action"] == "insert", r["action"])
    check("标记 unregistered/expired", r["unregistered"] and r["expired"])
    check("落为 UNREG- 未注册柜", r["locker_no"].startswith("UNREG-"), r["locker_no"])
    state = rebind.get_state(CI)
    check("库里多了一条未注册柜",
          any(x["locker_no"] == r["locker_no"] and x["code"] == "QR-OLD"
              for x in state["lockers"]))
    rebind.reset_demo(CI)  # 清掉未注册柜，恢复干净基线


def test_status_stays_with_locker():
    print("== 已有柜况留在原柜 ==")
    rebind.reset_demo(CI)
    rebind.report(CI, "QR-A", "满格")
    rebind.rebind(CI, [
        {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-B"},
        {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-A"},
    ])
    state = rebind.get_state(CI)
    check("换绑后 L01 仍保留“满格”柜况", status_of(state, "L01") == "满格",
          status_of(state, "L01"))
    check("换绑后 L02 仍是“正常”柜况", status_of(state, "L02") == "正常")
    # 旧码 QR-A 现在属于 L02：旧终端再上报，柜况写到 L02，而不是新码持有者之外的柜
    rebind.report(CI, "QR-A", "故障")
    state = rebind.get_state(CI)
    check("换绑后旧码上报写到新归属 L02", status_of(state, "L02") == "故障",
          status_of(state, "L02"))
    check("L01 的历史柜况未被覆盖", status_of(state, "L01") == "满格")


def test_concurrency():
    print("== 上报 × 换绑并发 ==")
    demo = rebind.concurrent_demo(CI, "QR-A", "满格")
    by = {r["scenario"]: r for r in demo["results"]}

    before = by["before"]
    check("before：上报按换绑前绑定落到 L01",
          before["landed_locker"] == "L01", str(before["landed_locker"]))
    check("before：换绑后 QR-A 归属 L02", before["after_rebind_owner"] == "L02")

    during = by["during"]
    check("during：换绑事务成功提交", during.get("rebind") is not None
          and during["rebind"].get("ok"), str(during.get("rebind_error")))
    check("during：并发上报等锁后按换绑后绑定落到 L02",
          during["landed_locker"] == "L02", str(during["landed_locker"]))
    check("during：上报确实经历了等待（持锁窗口 300ms）",
          during["report"]["elapsed_ms"] >= 200,
          f"{during['report']['elapsed_ms']}ms（直写约 1~3ms）")

    after = by["after"]
    check("after：上报按换绑后绑定落到 L02",
          after["landed_locker"] == "L02", str(after["landed_locker"]))

    landed = {r["landed_locker"] for r in demo["results"]}
    check("落库结果只可能是换绑前 L01 或换绑后 L02", landed <= {"L01", "L02"}, str(landed))


def test_concurrent_rollback_keeps_report():
    print("== 并发时换绑整批退回，上报按旧绑定落库 ==")
    rebind.reset_demo(CI)
    barrier = threading.Event()
    release = threading.Event()
    box = {}

    def bad_rebind():
        try:
            rebind.rebind(CI, [
                {"locker_no": "L01", "old_code": "QR-WRONG", "new_code": "QR-B"},
                {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-A"},
            ], lock_event=_Hook(barrier, release), hold_s=0)
        except rebind.RebindError as exc:
            box["error"] = str(exc)

    t = threading.Thread(target=bad_rebind, daemon=True)
    t.start()
    barrier.wait(timeout=10)
    r = rebind.report(CI, "QR-A", "满格")  # 与持锁（即将回滚）的事务并发
    release.set()
    t.join(timeout=10)
    check("错原码换绑被退回", "原码与库中不符" in box.get("error", ""), str(box))
    check("并发上报仍按换绑前绑定落到 L01", r["locker_no"] == "L01", r["locker_no"])
    state = rebind.get_state(CI)
    check("回滚后 QR-A 仍属 L01", code_of(state, "L01") == "QR-A")


class _Hook:
    def __init__(self, barrier, release):
        self._barrier, self._release = barrier, release

    def set(self):
        self._barrier.set()
        self._release.wait(timeout=10)


def test_http_smoke():
    print("== HTTP 接口冒烟（Flask test_client） ==")
    import main as flask_main

    client = flask_main.app.test_client()
    resp = client.post("/api/rebind/reset")
    check("POST /reset 200", resp.status_code == 200, str(resp.status_code))

    resp = client.get("/api/rebind/state")
    check("GET /state 200 且含 4 柜",
          resp.status_code == 200 and len(resp.get_json()["lockers"]) == 4)

    resp = client.post("/api/rebind/execute", json={"items": [
        {"locker_no": "L01", "old_code": "QR-A", "new_code": "QR-B"},
        {"locker_no": "L02", "old_code": "QR-B", "new_code": "QR-A"},
    ]})
    check("POST /execute 交换成功",
          resp.status_code == 200 and resp.get_json()["affected"] == 2,
          str(resp.status_code))

    resp = client.post("/api/rebind/execute", json={"items": [
        {"locker_no": "L01", "old_code": "QR-NOPE", "new_code": "QR-A"},
    ]})
    body = resp.get_json()
    check("错原码 HTTP 422 + rolled_back",
          resp.status_code == 422 and body.get("rolled_back") is True,
          str(resp.status_code))

    resp = client.post("/api/rebind/report", json={"code": "QR-A", "status": "满格"})
    body = resp.get_json()
    # 上一步交换后 QR-A 属 L02
    check("POST /report 返回实际柜号 L02 与 pid",
          resp.status_code == 200 and body["locker_no"] == "L02"
          and isinstance(body.get("pid"), int), str(body))

    resp = client.post("/api/rebind/concurrent", json={"code": "QR-A", "status": "满格"})
    body = resp.get_json()
    check("POST /concurrent 返回 3 个场景",
          resp.status_code == 200 and len(body["results"]) == 3,
          str(resp.status_code))


def test_no_idle_transactions_left():
    print("== 连接收尾：没有遗留未提交事务 ==")
    conn = psycopg2.connect(**CI)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND state <> 'idle' "
                "AND pid <> pg_backend_pid()"
            )
            active = cur.fetchone()[0]
    finally:
        conn.close()
    check("演练后无在飞事务/查询", active == 0, f"active={active}")


def main():
    test_reset_and_swap()
    test_rollback_on_wrong_old_code()
    test_other_validation()
    test_report_and_pid()
    test_expired_code()
    test_status_stays_with_locker()
    test_concurrency()
    test_concurrent_rollback_keeps_report()
    test_http_smoke()
    test_no_idle_transactions_left()
    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项失败：")
        for name in FAILURES:
            print("  -", name)
        return 1
    print("全部换绑演练自测通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
