"""提示变更共用业务模块（app/cueflow）回归测试。

共用模块手工确认 / 自动发出 / 撤回 / 编辑 / 重排 / 接受提议共用同一套
锁内业务实现（手工与自动发出共用 cueflow.issue_current）。本测试在既有
各操作专项回归之外，补充"共用路径"维度：

- 手工 / 自动发出同构：快照、issued_seq=新版本、版本 +1、换轮、下一条
  完整间隔重计时；末条完成并关闭自动；
- 共用路径交错：手工确认 vs 自动到期、确认 vs 撤回、接受提议 vs 自动
  发出在同一屏障下多线程竞争——同版本最多一个生效，不重复、不跳条、
  不留半次结果；
- 中途写入失败：向 cues / meta / suggestions UPDATE 注入失败触发器，
  手工确认、自动发出、撤回、编辑、重排、接受提议各自整体回滚——提示
  快照、队列、版本、提议状态、核验轮次与自动计时全部不变；
- 成功编辑 / 接受当前提示即使文本相同也换轮并重计时；编辑其他提示与
  不换队首的重排不重置；暂停中重计时保存完整剩余间隔。

沿用各专项测试的设施：FastAPI TestClient + 独立临时数据库 + 可控时钟；
交错场景除 HTTP 端点外，还在多线程内直接以独立连接调用 cueflow 函数
（共用 main.lock），真正竞争同一把写锁。

运行：python3 tests/cueflow.test.py
"""
import os
import sys
import tempfile
import threading

_tmp = tempfile.mkdtemp(prefix="cue-cueflow-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "cues.db")
# 可控时钟精确驱动；不启动真实后台线程，避免压测时额外发出
os.environ["AUTO_WORKER"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app import cueflow  # noqa: E402

failures = 0


def check(cond, msg):
    global failures
    if cond:
        print("  ✓ " + msg)
    else:
        failures += 1
        print("  ✗ " + msg)


CUES3 = [
    {"number": "A1", "content": "灯光渐亮"},
    {"number": "A2", "content": "音乐起"},
    {"number": "A3", "content": "幕落"},
]
CUES5 = CUES3 + [
    {"number": "A4", "content": "谢幕"}, {"number": "A5", "content": "散场灯"},
]


class FakeClock:
    """可控时钟：只在测试显式推进时流逝。"""

    def __init__(self, t=1_000.0):
        self.t = float(t)
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            return self.t

    def advance(self, seconds):
        with self._lock:
            self.t += seconds


def use_fake_clock(t=1_000.0):
    clock = FakeClock(t)
    main._clock = clock.now
    return clock


_db_seq = 0


def fresh_db():
    global _db_seq
    _db_seq += 1
    main.DB_PATH = os.path.join(_tmp, f"c{_db_seq:02d}.db")


def setup_show(c, cues=None, headers=None):
    r = c.post("/api/setup", json={"cues": cues or CUES3}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def enable_auto(c, v, interval=10, headers=None):
    r = c.post("/api/auto", json={"version": v, "enabled": True,
                                 "interval": interval}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def enable_verify_and_setup(c, c_obj, cues=None):
    """启用本场核验后开场：返回开场状态；第 1 轮就绪可随后回报。"""
    v = c.get("/api/state").json()["version"]
    r = c.post("/api/verify/enable", json={"version": v})
    assert r.status_code == 200, r.text
    return setup_show(c, cues or c_obj)


def report_both(c, state):
    cur, rnd = state["current"], state["verify"]["round"]
    for dept in ("lighting", "sound"):
        r = c.post("/api/verify/report", json={
            "department": dept, "cue_id": cur["id"], "round": rnd, "ready": True})
        assert r.status_code == 200, r.text


def add_trigger(name, table):
    with main.db() as conn:
        conn.execute(
            f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} "
            f"BEGIN SELECT RAISE(ABORT, 'injected {table} failure'); END"
        )


def drop_trigger(name):
    with main.db() as conn:
        conn.execute(f"DROP TRIGGER {name}")


def run_racer(barrier, fn, results, index):
    """屏障后同时执行 fn，捕获结果 / 异常类型名（共用同一把 main.lock）。"""
    barrier.wait()
    try:
        results[index] = ("ok", fn())
    except Exception as exc:  # noqa: BLE001 - 压测只分类异常
        results[index] = ("err", type(exc).__name__)


def race(n, *fns):
    """n 个线程在同一屏障后执行 fns（长度须为 n），返回按序号排列的结果。"""
    barrier = threading.Barrier(n)
    results = [None] * n
    threads = [
        threading.Thread(target=run_racer, args=(barrier, fn, results, i))
        for i, fn in enumerate(fns)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def direct(fn):
    """以独立连接在共用写锁内直接跑 cueflow 业务函数。"""
    def _run():
        with main.lock, main.db() as conn:
            return fn(conn)
    return _run


# ----------------------------------------------------------- 共用发出同构

def test_manual_and_auto_share_issue():
    fresh_db()
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("C1 手工 / 自动共用发出：快照、seq、换轮、重计时、末条关自动")
        s = enable_verify_and_setup(c, CUES3)
        v = enable_auto(c, s["version"], 10)["version"]

        # 手工发出第一条：两工种本轮就绪
        s = c.get("/api/state").json()
        report_both(c, s)
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 200, "手工确认成功")
        s = r.json()
        check(s["version"] == v + 1, "手工发出版本 +1")
        check(s["issued"][0]["content"] == "灯光渐亮", "手工已发为内容快照")
        check(s["issued"][0]["issued_at"], "手工已发记录发出时间")
        # issued_seq 取新版本号（与自动路径同一实现）
        with main.db() as conn:
            seq = conn.execute(
                "SELECT issued_seq FROM cues WHERE status = 'issued'"
            ).fetchone()["issued_seq"]
        check(seq == v + 1, "手工 issued_seq 等于新版本号")
        check(s["verify"]["round"] == 2 and s["verify"]["lighting"] is None,
              "手工发出开启新轮次并清空就绪")
        check(s["auto"]["remaining_seconds"] == 10.0,
              "手工发出后下一条从完整间隔重新计时")

        # 自动发出第二条：规则完全一致（同一 cueflow.issue_current）
        s = c.get("/api/state").json()
        report_both(c, s)  # 手工换轮后需对新一轮重新报就绪
        clock.advance(10)
        check(main.auto_tick() is True, "到期自动发出一条")
        s = c.get("/api/state").json()
        check(s["version"] == v + 2 and len(s["issued"]) == 2,
              "自动发出版本 +1、进度 +1")
        check(s["issued"][1]["number"] == "A2"
              and s["issued"][1]["content"] == "音乐起",
              "自动已发同为内容快照")
        with main.db() as conn:
            seqs = [r["issued_seq"] for r in conn.execute(
                "SELECT issued_seq FROM cues WHERE status = 'issued' "
                "ORDER BY issued_seq")]
        check(seqs == [v + 1, v + 2], "自动 issued_seq 同样等于新版本号且连续")
        check(s["verify"]["round"] == 3, "自动发出同样换轮")
        check(s["auto"]["remaining_seconds"] == 10.0,
              "自动发出后下一条重新计满间隔（每次一条）")

        # 末条由自动发出：完成并关闭自动模式（共用实现的末条分支）
        report_both(c, s)
        clock.advance(10)
        check(main.auto_tick() is True, "自动发出末条")
        s = c.get("/api/state").json()
        check(s["status"] == "done", "末条发出进入已完成")
        check(s["auto"]["enabled"] is False
              and s["auto"]["remaining_seconds"] is None,
              "末条发出在同一事务关闭自动模式并清空计时")
        check(main.auto_tick() is False, "完成后自动任务不再发出")


# ----------------------------------------------------------- 共用路径交错

def test_manual_vs_auto_barrier():
    fresh_db()
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("C2 屏障交错：手工确认 vs 自动到期，恰好一个生效")
        s = setup_show(c)
        v = enable_auto(c, s["version"], 10)["version"]
        clock.advance(10)  # 已到期：两个调用都"看起来可发"

        # 两个竞争者在同一屏障后出发：手工直跑 cueflow.manual_issue，
        # 自动走后台入口 main.auto_tick（内部自取锁 / 连接）。
        barrier = threading.Barrier(2)
        outcome = {}

        def manual():
            barrier.wait()
            try:
                with main.lock, main.db() as conn:
                    cueflow.manual_issue(conn, version=v, clock=clock.now)
                outcome["manual"] = "ok"
            except cueflow.Conflict:
                outcome["manual"] = "conflict"

        def auto_run():
            barrier.wait()
            outcome["auto"] = "issued" if main.auto_tick() else "noop"

        threads = [threading.Thread(target=manual), threading.Thread(target=auto_run)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        check(
            (outcome["manual"] == "ok" and outcome["auto"] == "noop")
            or (outcome["manual"] == "conflict" and outcome["auto"] == "issued"),
            f"恰好一个发出（手工={outcome['manual']}，自动={outcome['auto']}）",
        )

        s = c.get("/api/state").json()
        check(s["version"] == v + 1 and len(s["issued"]) == 1,
              "同版本手工 / 自动竞争：只发出一条、版本只 +1")
        check(s["current"]["number"] == "A2", "不跳条：当前为第二条")
        # 输家随后必为过期 / 未到期：再调自动任务不补发
        check(main.auto_tick() is False, "竞争后不补发、不重复发出")
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 409, "输家手工旧版本确认得到 409")
        check(r.json()["state"]["version"] == v + 1, "409 附带最新状态")


def test_confirm_vs_withdraw_barrier():
    fresh_db()
    use_fake_clock()
    wins = {"confirm": 0, "withdraw": 0, "other": 0}
    with TestClient(main.app) as c:
        print("C3 多轮屏障交错：确认 vs 撤回同版本竞争，状态恒一致")
        for _ in range(30):
            s = setup_show(c, CUES3)
            v = s["version"]
            cue2, cue3 = s["pending"][1]["id"], s["pending"][2]["id"]
            # 先正常发出第一条，制造"末条已发"局面
            r = c.post("/api/confirm", json={"version": v})
            assert r.status_code == 200, r.text
            v = r.json()["version"]
            last_id = r.json()["issued"][0]["id"]

            barrier = threading.Barrier(2)

            def confirm_now():
                barrier.wait()
                with main.lock, main.db() as conn:
                    cueflow.manual_issue(conn, version=v, clock=main._clock)

            def withdraw_now():
                barrier.wait()
                with main.lock, main.db() as conn:
                    cueflow.withdraw_last(
                        conn, version=v, cue_id=last_id, clock=main._clock)

            outcomes = []
            def runner(fn):
                try:
                    fn()
                    outcomes.append("ok")
                except cueflow.Conflict:
                    outcomes.append("conflict")
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(f"other:{type(exc).__name__}")

            ts = [threading.Thread(target=runner, args=(confirm_now,)),
                  threading.Thread(target=runner, args=(withdraw_now,))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            if outcomes != ["ok", "conflict"] and outcomes != ["conflict", "ok"]:
                wins["other"] += 1
                check(False, f"出现非预期结果：{outcomes}")
                continue

            s = c.get("/api/state").json()
            check(s["version"] == v + 1, "胜者推进恰好一个版本")
            issued_ids = [i["id"] for i in s["issued"]]
            pending_ids = [p["id"] for p in s["pending"]]
            if len(issued_ids) == 2:
                # 确认胜：第二条已发，待发只剩第三条
                wins["confirm"] += 1
                check(pending_ids == [3] and s["status"] == "live",
                      "确认胜：顺序不跳条")
            else:
                # 撤回胜：无已发，第一条按快照回到队首
                wins["withdraw"] += 1
                check(issued_ids == []
                      and pending_ids == [last_id, cue2, cue3],
                      "撤回胜：末条按快照回到队首、其余顺序后移")
                check(s["status"] == "live" and s["paused"] is False,
                      "撤回胜后演出中、未暂停")
            # 队列内部一致性：位置紧凑且无重复
            with main.db() as conn:
                positions = [r["position"] for r in conn.execute(
                    "SELECT position FROM cues WHERE status='pending' "
                    "ORDER BY position")]
            check(positions == list(range(1, len(positions) + 1)),
                  "待发位置紧凑 1..n")
        check(wins["other"] == 0, "30 轮无异常类型结果")
        print(f"    （30 轮中确认胜 {wins['confirm']} 次、"
              f"撤回胜 {wins['withdraw']} 次）")
        check(wins["confirm"] + wins["withdraw"] == 30, "30 轮均为二选一")


def test_accept_vs_auto_issue_barrier():
    fresh_db()
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("C4 屏障交错：接受队首提议 vs 自动发出，不留半次结果")
        s = setup_show(c, CUES3)
        v = enable_auto(c, s["version"], 10)["version"]
        # 无控制权页面对队首提交提议（内容不同于原文，便于分辨快照）
        cur = c.get("/api/state").json()["current"]
        r = c.post("/api/suggestions", json={
            "version": v, "cue_id": cur["id"], "content": "灯光慢亮"})
        check(r.status_code == 200 and r.json()["version"] == v,
              "提交提议不改版本")
        clock.advance(10)  # 自动已到期
        sug_id = c.get("/api/state").json()["suggestions"][0]["id"]

        barrier = threading.Barrier(2)

        def accept_now():
            barrier.wait()
            with main.lock, main.db() as conn:
                cueflow.accept_pending_suggestion(
                    conn, suggestion_id=sug_id, version=v, clock=clock.now)

        def auto_now():
            barrier.wait()
            main.auto_tick()

        outcomes = []

        def runner(fn, tag):
            try:
                fn()
                outcomes.append((tag, "ok"))
            except cueflow.Conflict:
                outcomes.append((tag, "conflict"))

        ts = [threading.Thread(target=runner, args=(accept_now, "accept")),
              threading.Thread(target=runner, args=(auto_now, "auto"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        check(len([o for o in outcomes if o[1] == "ok"]) == 1,
              "接受 / 自动发出：恰好一个成功")

        s = c.get("/api/state").json()
        check(s["version"] == v + 1, "只有一个版本推进")
        sug = next(x for x in s["suggestions"] if x["id"] == sug_id)
        if sug["status"] == "accepted":
            # 接受胜：提示仍待发且内容为建议，自动计时被重置（未到期）
            check(s["current"]["content"] == "灯光慢亮" and len(s["issued"]) == 0,
                  "接受胜：队首改为建议内容、未发出")
            check(main.auto_tick() is False,
                  "接受胜重置计时：同一刻自动不发出")
            check(s["auto"]["remaining_seconds"] == 10.0,
                  "接受胜后从完整间隔重新计时")
            check(s["verify"]["round"] == 2, "接受当前提示开启新轮次")
        else:
            # 自动胜：队首已按原文快照发出，提议保持待审（目标失效）
            check(sug["status"] == "pending" and sug["target_valid"] is False,
                  "自动胜：提议保持待审且目标失效")
            check(len(s["issued"]) == 1
                  and s["issued"][0]["content"] == "灯光渐亮",
                  "自动胜：按原文快照发出，未被建议污染")
        # 再跑一次：接受迟到必 409，且不重复处理
        r = c.post(f"/api/suggestions/{sug_id}/accept",
                   json={"version": v + 1})
        check(r.status_code == 409, "迟到接受得到 409")


def test_same_version_many_callers():
    fresh_db()
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("C5 同版本多调用者齐发：只有一个写生效，其余 409 / 无操作")
        s = setup_show(c)
        v = enable_auto(c, s["version"], 10)["version"]
        clock.advance(10)
        # 4 手工确认（同版本）+ 2 自动任务 + 1 撤回（无已发，必失败）
        fns = [
            direct(lambda conn: cueflow.manual_issue(
                conn, version=v, clock=clock.now))
            for _ in range(4)
        ] + [main.auto_tick, main.auto_tick] + [
            direct(lambda conn: cueflow.withdraw_last(
                conn, version=v, cue_id=999, clock=clock.now))
        ]
        results = race(7, *fns)
        # 手工 / 撤回成功返回 None（计为 "ok", None）；自动任务返回 bool；
        # 过期手工 / 撤回抛 Conflict。
        write_oks = [r for r in results if r == ("ok", None)]
        auto_true = [r for r in results if r == ("ok", True)]
        conflicts = [r for r in results if r == ("err", "Conflict")]
        check(len(write_oks) + len(auto_true) == 1,
              "7 个同版本调用恰好 1 个生效")
        check(len(conflicts) >= 1, "过期手工 / 撤回均以 Conflict 失败")
        s = c.get("/api/state").json()
        check(s["version"] == v + 1 and len(s["issued"]) == 1,
              "只发出一条、版本只 +1")


# ----------------------------------------------------------- 中途写入失败

def test_issue_write_failure_rollback():
    fresh_db()
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("C6 共用发出写入失败：手工 / 自动都整体回滚")
        s = enable_verify_and_setup(c, CUES3)
        v = enable_auto(c, s["version"], 10)["version"]
        s = c.get("/api/state").json()
        report_both(c, s)
        round_before = s["verify"]["round"]
        clock.advance(10)  # 自动也到期

        # cues 更新失败：手工确认在写快照处中止
        add_trigger("fail_cues", "cues")
        try:
            raised = False
            try:
                c.post("/api/confirm", json={"version": v})
            except Exception:
                raised = True
            check(raised, "手工确认写入失败抛出异常（不被吞掉）")
            s = c.get("/api/state").json()
            check(s["version"] == v and len(s["issued"]) == 0,
                  "手工失败：版本与进度不变")
            check(s["current"]["number"] == "A1", "手工失败：提示仍待发")
            check(s["verify"]["round"] == round_before
                  and s["verify"]["all_ready"] is True,
                  "手工失败：轮次与本轮就绪不变")
            check(s["auto"]["remaining_seconds"] == 0.0,
                  "手工失败：自动计时未被重置（仍到期）")

            # 同一触发器下自动任务同样整体回滚
            raised = False
            try:
                main.auto_tick()
            except Exception:
                raised = True
            check(raised, "自动发出写入失败抛出异常")
            s = c.get("/api/state").json()
            check(s["version"] == v and len(s["issued"]) == 0,
                  "自动失败：版本与进度不变")
            check(s["verify"]["round"] == round_before,
                  "自动失败：轮次不变")
        finally:
            drop_trigger("fail_cues")

        # 故障解除：到期条件仍在，自动发出成功（手工 / 自动共用路径恢复）
        check(main.auto_tick() is True, "故障解除后到期自动发出成功")
        s = c.get("/api/state").json()
        check(s["version"] == v + 1 and len(s["issued"]) == 1,
              "恢复后正常推进")
        check(s["verify"]["round"] == round_before + 1, "恢复后正常换轮")

        # meta 更新失败：快照 / 轮次 / 计时都不得留下半次结果
        clock.advance(10)
        report_both(c, c.get("/api/state").json())
        add_trigger("fail_meta", "meta")
        try:
            raised = False
            try:
                main.auto_tick()
            except Exception:
                raised = True
            check(raised, "meta 更新失败时自动任务抛出异常")
            s = c.get("/api/state").json()
            check(s["version"] == v + 1 and len(s["issued"]) == 1,
                  "meta 失败：连提示快照也一起回滚（无半次发出）")
            check(s["current"]["number"] == "A2", "meta 失败：第二条仍待发")
        finally:
            drop_trigger("fail_meta")
        check(main.auto_tick() is True, "meta 触发器移除后正常发出")


def test_edit_write_failure_rollback():
    fresh_db()
    use_fake_clock()
    with TestClient(main.app) as c:
        print("C7 编辑写入失败：提示、版本、轮次不变")
        s = enable_verify_and_setup(c, CUES3)
        v = s["version"]
        cur = s["current"]
        add_trigger("fail_cues_edit", "cues")
        try:
            raised = False
            try:
                c.post("/api/edit", json={"version": v, "cue_id": cur["id"],
                                         "content": "改不动"})
            except Exception:
                raised = True
            check(raised, "编辑写入失败抛出异常")
        finally:
            drop_trigger("fail_cues_edit")
        s = c.get("/api/state").json()
        check(s["version"] == v, "编辑失败：版本不变")
        check(s["current"]["content"] == "灯光渐亮", "编辑失败：内容不变")
        check(s["verify"]["round"] == 1 and s["verify"]["cue_id"] == cur["id"],
              "编辑失败：轮次不变")
        r = c.post("/api/edit", json={"version": v, "cue_id": cur["id"],
                                     "content": "改成了"})
        check(r.status_code == 200 and r.json()["version"] == v + 1,
              "故障解除后编辑成功")


def test_withdraw_write_failure_rollback():
    fresh_db()
    use_fake_clock()
    with TestClient(main.app) as c:
        print("C8 撤回写入失败：已发、队列、版本、轮次不变")
        s = enable_verify_and_setup(c, CUES3)
        v = s["version"]
        report_both(c, s)  # 核验开启：确认发出须两工种就绪
        r = c.post("/api/confirm", json={"version": v})
        assert r.status_code == 200, r.text
        v2 = r.json()["version"]
        last_id = r.json()["issued"][0]["id"]
        round_after_issue = r.json()["verify"]["round"]

        add_trigger("fail_cues_wd", "cues")
        try:
            raised = False
            try:
                c.post("/api/withdraw", json={"version": v2, "cue_id": last_id})
            except Exception:
                raised = True
            check(raised, "撤回写入失败抛出异常")
        finally:
            drop_trigger("fail_cues_wd")
        s = c.get("/api/state").json()
        check(s["version"] == v2 and len(s["issued"]) == 1,
              "撤回失败：版本与已发不变")
        check(s["current"]["number"] == "A2", "撤回失败：队首不变")
        check(s["verify"]["round"] == round_after_issue, "撤回失败：轮次不变")
        with main.db() as conn:
            row = conn.execute(
                "SELECT status, issued_content, position FROM cues WHERE id = ?",
                (last_id,)).fetchone()
        check(row["status"] == "issued" and row["issued_content"] == "灯光渐亮",
              "撤回失败：发出快照保留")


def test_reorder_write_failure_rollback():
    fresh_db()
    use_fake_clock()
    with TestClient(main.app) as c:
        print("C9 重排写入失败：位置、版本、轮次不变")
        s = setup_show(c)
        v = s["version"]
        ids = [p["id"] for p in s["pending"]]
        new_order = [ids[2], ids[0], ids[1]]  # 换队首
        add_trigger("fail_cues_ro", "cues")
        try:
            raised = False
            try:
                c.post("/api/reorder", json={"version": v, "order": new_order})
            except Exception:
                raised = True
            check(raised, "重排写入失败抛出异常")
        finally:
            drop_trigger("fail_cues_ro")
        s = c.get("/api/state").json()
        check(s["version"] == v, "重排失败：版本不变")
        check([p["id"] for p in s["pending"]] == ids,
              "重排失败：待发顺序不变")
        check(s["current"]["number"] == "A1", "重排失败：队首不变")
        r = c.post("/api/reorder", json={"version": v, "order": new_order})
        check(r.status_code == 200 and r.json()["current"]["number"] == "A3",
              "故障解除后重排成功")


def test_accept_write_failure_rollback():
    fresh_db()
    use_fake_clock()
    with TestClient(main.app) as c:
        print("C10 接受提议写入失败：提示、提议、版本、轮次不变")
        s = enable_verify_and_setup(c, CUES3)
        v = enable_auto(c, s["version"], 10)["version"]
        cur = c.get("/api/state").json()["current"]
        r = c.post("/api/suggestions", json={
            "version": v, "cue_id": cur["id"], "content": "灯光慢亮"})
        assert r.status_code == 200, r.text
        sug_id = r.json()["suggestions"][0]["id"]

        # 先改 cues 成功、再在 suggestions 更新处失败 -> 同事务整体回滚
        add_trigger("fail_sug", "suggestions")
        try:
            raised = False
            try:
                c.post(f"/api/suggestions/{sug_id}/accept",
                       json={"version": v})
            except Exception:
                raised = True
            check(raised, "接受时标记提议失败抛出异常")
        finally:
            drop_trigger("fail_sug")
        s = c.get("/api/state").json()
        check(s["version"] == v, "接受失败：版本不变")
        check(s["current"]["content"] == "灯光渐亮",
              "接受失败：提示内容回滚（未留下半次改写）")
        sug = next(x for x in s["suggestions"] if x["id"] == sug_id)
        check(sug["status"] == "pending" and sug["target_valid"] is True,
              "接受失败：提议保持待审且仍有效")
        check(s["verify"]["round"] == 1, "接受失败：轮次不变")
        check(s["auto"]["remaining_seconds"] == 10.0,
              "接受失败：自动计时未重置")
        r = c.post(f"/api/suggestions/{sug_id}/accept", json={"version": v})
        check(r.status_code == 200 and r.json()["current"]["content"] == "灯光慢亮",
              "故障解除后接受成功")
        check(r.json()["version"] == v + 1, "接受成功版本 +1")


# ----------------------------------------------- 相同文本换轮 / 暂停完整间隔

def test_same_text_head_still_restarts():
    fresh_db()
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("C11 相同文本：编辑 / 接受当前提示仍换轮重计时；其他位置不重置")
        s = enable_verify_and_setup(c, CUES3)
        v = enable_auto(c, s["version"], 10)["version"]
        clock.advance(4)
        s = c.get("/api/state").json()
        cur = s["current"]

        # 编辑当前提示，文本与原文完全相同：仍成功、仍换轮、仍重计时
        r = c.post("/api/edit", json={"version": s["version"],
                                     "cue_id": cur["id"],
                                     "content": cur["content"]})
        check(r.status_code == 200, "相同文本编辑当前提示仍成功")
        check(r.json()["verify"]["round"] == 2, "相同文本编辑当前提示仍换轮")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "相同文本编辑当前提示仍从完整间隔重计时")
        v = r.json()["version"]

        # 编辑其他提示（同文本）不换轮、不重置
        clock.advance(3)
        s = c.get("/api/state").json()
        other = s["pending"][1]
        r = c.post("/api/edit", json={"version": s["version"],
                                     "cue_id": other["id"],
                                     "content": other["content"]})
        check(r.status_code == 200, "相同文本编辑其他提示成功")
        check(r.json()["verify"]["round"] == 2, "编辑其他提示不换轮")
        check(r.json()["auto"]["remaining_seconds"] == 7.0,
              "编辑其他提示不重置计时（10 - 3 = 7）")

        # 队首不变的重排（完全相同顺序）也不换轮、不重置
        s = c.get("/api/state").json()
        ids = [p["id"] for p in s["pending"]]
        r = c.post("/api/reorder", json={"version": s["version"], "order": ids})
        check(r.status_code == 200, "相同顺序重排成功")
        check(r.json()["verify"]["round"] == 2, "不换队首不换轮")
        check(r.json()["auto"]["remaining_seconds"] == 7.0,
              "不换队首不重置计时")

        # 接受当前提示的提议，建议文本与原文相同：仍换轮、仍重计时
        s = c.get("/api/state").json()
        r = c.post("/api/suggestions", json={
            "version": s["version"], "cue_id": s["current"]["id"],
            "content": s["current"]["content"]})
        assert r.status_code == 200, r.text
        sug_id = r.json()["suggestions"][-1]["id"]
        s = c.get("/api/state").json()
        r = c.post(f"/api/suggestions/{sug_id}/accept",
                   json={"version": s["version"]})
        check(r.status_code == 200, "相同文本接受当前提示仍成功")
        check(r.json()["verify"]["round"] == 3,
              "相同文本接受当前提示仍换轮")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "相同文本接受当前提示仍重计时")
        check(r.json()["version"] == v + 3,
              "编辑其他提示、不换队首重排与接受各推进一个版本")


def test_paused_restart_keeps_full_interval():
    fresh_db()
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("C12 暂停中换队首类操作：重计时保存完整剩余间隔")
        s = enable_verify_and_setup(c, CUES5)
        v = enable_auto(c, s["version"], 10)["version"]
        clock.advance(4)  # 剩余 6 秒时暂停
        r = c.post("/api/pause", json={"version": v, "paused": True})
        check(r.status_code == 200 and r.json()["auto"]["remaining_seconds"] == 6.0,
              "暂停冻结当时剩余 6 秒")
        v = r.json()["version"]

        # 暂停中编辑当前提示：重计时保存**完整**间隔（不是剩余 6 秒）
        s = c.get("/api/state").json()
        cur = s["current"]
        r = c.post("/api/edit", json={"version": s["version"],
                                     "cue_id": cur["id"], "content": "暂停中改词"})
        check(r.status_code == 200, "暂停中可编辑当前提示")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "暂停中编辑当前提示：冻结完整 10 秒间隔")
        check(r.json()["verify"]["round"] == 2, "暂停中编辑当前提示仍换轮")
        v = r.json()["version"]

        # 暂停中接受当前提示的提议：同样完整间隔
        s = c.get("/api/state").json()
        r = c.post("/api/suggestions", json={
            "version": s["version"], "cue_id": s["current"]["id"],
            "content": "暂停中提议文本"})
        assert r.status_code == 200, r.text
        sug_id = r.json()["suggestions"][-1]["id"]
        s = c.get("/api/state").json()
        r = c.post(f"/api/suggestions/{sug_id}/accept",
                   json={"version": s["version"]})
        check(r.status_code == 200, "暂停中可接受当前提示提议")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "暂停中接受当前提示：冻结完整 10 秒间隔")
        v = r.json()["version"]

        # 暂停中重排换头：完整间隔；编辑其他提示不碰冻结值
        s = c.get("/api/state").json()
        ids = [p["id"] for p in s["pending"]]
        r = c.post("/api/reorder", json={"version": s["version"],
                                         "order": [ids[1], ids[0]] + ids[2:]})
        check(r.status_code == 200, "暂停中可重排换头")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "暂停中重排换头：冻结完整 10 秒间隔")
        v = r.json()["version"]

        # 撤回末条（暂停态允许）：同样保存完整间隔
        # 先恢复发出一条，再暂停，再暂停态撤回
        r = c.post("/api/pause", json={"version": v, "paused": False})
        v = r.json()["version"]
        report_both(c, c.get("/api/state").json())
        clock.advance(10)
        check(main.auto_tick() is True, "恢复后自动发出一条")
        s = c.get("/api/state").json()
        v = s["version"]
        clock.advance(4)
        r = c.post("/api/pause", json={"version": v, "paused": True})
        v = r.json()["version"]
        last_id = c.get("/api/state").json()["issued"][-1]["id"]
        r = c.post("/api/withdraw", json={"version": v, "cue_id": last_id})
        check(r.status_code == 200, "暂停中可撤回末条")
        check(r.json()["paused"] is True, "撤回不改变暂停标记")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "暂停中撤回：冻结完整 10 秒间隔")

        # 恢复后续计的是完整间隔，而不是暂停前的剩余
        v = r.json()["version"]
        r = c.post("/api/pause", json={"version": v, "paused": False})
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "恢复后按完整间隔续计")


# ------------------------------------------------- 身份 / 资格检查同锁事务

def test_identity_and_eligibility_in_lock():
    fresh_db()
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("C13 手工查凭据与版本，自动查到期 / 暂停 / 就绪且不要凭据")
        v0 = c.get("/api/state").json()["version"]
        r = c.post("/api/control/enable", json={"version": v0})
        token = r.json()["control_token"]
        v = r.json()["version"]
        h = {"X-Control-Token": token}

        # 启用核验 + 开场 + 开启自动（间隔 10 秒）
        r = c.post("/api/verify/enable", json={"version": v}, headers=h)
        assert r.status_code == 200, r.text
        s = setup_show(c, CUES3, headers=h)
        v = s["version"]
        v = enable_auto(c, v, 10, headers=h)["version"]

        # 手工路径：缺凭据 403 且数据不变（即使版本最新）
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 403, "手工确认缺凭据返回 403")
        s = c.get("/api/state").json()
        check(s["version"] == v and len(s["issued"]) == 0,
              "403 不推进版本、不发出")
        # 手工路径：过期版本带凭据 -> 409（暂停 / 就绪 / 版本由业务模块查）
        r = c.post("/api/confirm", json={"version": v + 1}, headers=h)
        check(r.status_code == 409, "手工确认过期版本 409")
        # 手工路径：版本最新、凭据有效但未就绪 -> 409
        r = c.post("/api/confirm", json={"version": v}, headers=h)
        check(r.status_code == 409, "两工种未就绪时手工确认 409")

        # 自动路径：不需要任何客户端凭据；到期但未就绪继续等
        clock.advance(10)
        check(main.auto_tick() is False, "到期未就绪：自动继续等、不发出")
        s = c.get("/api/state").json()
        check(s["version"] == v and s["auto"]["waiting_ready"] is True,
              "等待就绪期间版本不变")

        # 仅一个工种就绪仍不发
        cur, rnd = s["current"]["id"], s["verify"]["round"]
        r = c.post("/api/verify/report", json={
            "department": "lighting", "cue_id": cur, "round": rnd, "ready": True})
        assert r.status_code == 200, r.text
        check(main.auto_tick() is False, "缺一工种自动仍不发出")
        r = c.post("/api/verify/report", json={
            "department": "sound", "cue_id": cur, "round": rnd, "ready": True})
        assert r.status_code == 200, r.text
        check(main.auto_tick() is True, "两工种就绪后自动发出（无客户端凭据）")
        s = c.get("/api/state").json()
        check(s["version"] == v + 1 and len(s["issued"]) == 1,
              "自动发出推进版本且不要求凭据")

        # 自动路径：暂停中不发出
        v = s["version"]
        clock.advance(4)
        r = c.post("/api/pause", json={"version": v, "paused": True}, headers=h)
        v = r.json()["version"]
        clock.advance(30)
        check(main.auto_tick() is False, "暂停中自动不发出")
        check(c.get("/api/state").json()["version"] == v, "暂停中版本不变")


test_manual_and_auto_share_issue()
test_manual_vs_auto_barrier()
test_confirm_vs_withdraw_barrier()
test_accept_vs_auto_issue_barrier()
test_same_version_many_callers()
test_issue_write_failure_rollback()
test_edit_write_failure_rollback()
test_withdraw_write_failure_rollback()
test_reorder_write_failure_rollback()
test_accept_write_failure_rollback()
test_same_text_head_still_restarts()
test_paused_restart_keeps_full_interval()
test_identity_and_eligibility_in_lock()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
