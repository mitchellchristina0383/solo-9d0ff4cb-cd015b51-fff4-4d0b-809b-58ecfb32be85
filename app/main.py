"""Flask 入口：页面 + 运行接口 + 取件柜换绑演练。

环境变量：PGHOST / PGPORT / PGDATABASE / PGUSER / PGPASSWORD / PORT
"""
import os
import threading

from flask import Flask, jsonify, render_template, request

import rebind
from engine import run_experiment
from presets import PRESETS
from script_parser import ScriptError, parse_script

app = Flask(__name__)

# 所有请求共用一个沙箱库，同一时刻只允许跑一轮实验，避免互相重置数据
RUN_LOCK = threading.Lock()
# 换绑演练自身的写操作也串行化（上报刻意不加锁，用来与换绑真实并发）
REBIND_LOCK = threading.Lock()

DEFAULT_TIMEOUT = 5.0
MAX_TIMEOUT = 60.0


def conninfo():
    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "dbname": os.environ.get("PGDATABASE", "sandbox"),
        "user": os.environ.get("PGUSER", "postgres"),
        "password": os.environ.get("PGPASSWORD", ""),
        "connect_timeout": 10,
    }


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/presets")
def get_presets():
    return jsonify(PRESETS)


@app.post("/api/run")
def run():
    body = request.get_json(force=True, silent=True) or {}
    init_sql = (body.get("init_sql") or "").strip()
    script = (body.get("script") or "").strip()
    check_sql = (body.get("check_sql") or "").strip()
    try:
        timeout_s = float(body.get("timeout_s") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT
    timeout_s = min(max(timeout_s, 1.0), MAX_TIMEOUT)

    if not init_sql:
        return jsonify({"error": "初始数据 SQL 不能为空"}), 400
    if not check_sql:
        return jsonify({"error": "核对查询不能为空"}), 400
    try:
        steps = parse_script(script)
    except ScriptError as exc:
        return jsonify({"error": f"脚本解析失败：{exc}"}), 400

    if not RUN_LOCK.acquire(blocking=False):
        return jsonify({"error": "已有实验正在运行，请稍候（所有会话共用一个沙箱库）"}), 409
    try:
        rounds = run_experiment(conninfo(), init_sql, steps, check_sql, timeout_s)
    finally:
        RUN_LOCK.release()
    return jsonify({"rounds": rounds, "timeout_s": timeout_s})


# ---------------------------------------------------------- 取件柜换绑演练


@app.post("/api/rebind/reset")
def rebind_reset():
    with REBIND_LOCK:
        rebind.reset_demo(conninfo())
        return jsonify(rebind.get_state(conninfo()))


@app.get("/api/rebind/state")
def rebind_state():
    return jsonify(rebind.get_state(conninfo()))


@app.post("/api/rebind/execute")
def rebind_execute():
    body = request.get_json(force=True, silent=True) or {}
    items = body.get("items") or []
    if not REBIND_LOCK.acquire(blocking=False):
        return jsonify({"error": "已有换绑/演练正在执行，请稍候"}), 409
    try:
        try:
            result = rebind.rebind(conninfo(), items)
        except rebind.RebindError as exc:
            # 业务校验失败：事务已回滚，连同回滚后的当前状态一起返回，
            # 便于页面证明“一行不符，整批退回，绑定不变”。
            # 静态请求校验（空批次/空值等）算 400；库中原码不符等算 422。
            status_code = 400 if "批次为空" in str(exc) or "空值" in str(exc) \
                or "格式不正确" in str(exc) else 422
            return jsonify({"ok": False, "error": str(exc),
                            "rolled_back": True,
                            "state": rebind.get_state(conninfo())}), status_code
        return jsonify({**result, "state": rebind.get_state(conninfo())})
    finally:
        REBIND_LOCK.release()


@app.post("/api/rebind/report")
def rebind_report():
    # 上报不加 REBIND_LOCK：它必须能与换绑事务真实并发（独立连接）。
    body = request.get_json(force=True, silent=True) or {}
    try:
        result = rebind.report(conninfo(), body.get("code"), body.get("status"))
    except rebind.RebindError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({**result, "state": rebind.get_state(conninfo())})


@app.post("/api/rebind/concurrent")
def rebind_concurrent():
    body = request.get_json(force=True, silent=True) or {}
    code = (body.get("code") or "").strip()
    status = (body.get("status") or "正常").strip() or "正常"
    if not code:
        return jsonify({"error": "并发演练需要一个上报码（默认 QR-A）"}), 400
    if not REBIND_LOCK.acquire(blocking=False):
        return jsonify({"error": "已有换绑/演练正在执行，请稍候"}), 409
    try:
        result = rebind.concurrent_demo(conninfo(), code, status)
    finally:
        REBIND_LOCK.release()
    return jsonify(result)


def _init_rebind_schema():
    try:
        rebind.ensure_schema(conninfo())
    except Exception as exc:  # 数据库未就绪时不应阻塞页面启动
        app.logger.warning("换绑演练建表失败（稍后接口会再尝试）：%s", exc)


# 与 gunicorn 多进程不同，本镜像单进程 python main.py 启动，直接初始化即可
_init_rebind_schema()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
