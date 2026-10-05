"""自动发出后端回归测试（FastAPI TestClient + 独立临时数据库 + 可控时钟）。

覆盖：默认关闭旧调用不变；设置规则（仅演出中、携带版本号、受控制席保护，
成功版本 +1；非演出中/版本过期 409 附最新状态；间隔非 1～60 整数 400 且
先于版本检查）；开启或改间隔后从完整间隔计时；后台任务在当前提示等满
间隔、未暂停且核验门槛满足时发出一条（可控时钟直接驱动 auto_tick，无需
页面保持打开）；暂停冻结剩余时间、恢复续计（含到期后暂停、暂停中开启）；
到期未就绪继续等；手工确认 / 撤回 / 改写当前提示 / 重排换头重新计时，
队首不变的重排与编辑其他提示不重置；与手工确认竞争不重复发出、不跳条；
每次只发一条、不补发积压；接管 / 归档 / 重置 / 完成 / 服务重启关闭自动
模式；写入失败整体回滚（不推进、不重置计时、不改版本）；发出沿用快照与
换轮规则；真实后台线程冒烟（1 秒间隔自动发出、停止后不再发出）。

运行：python3 tests/auto.test.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-auto-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "cues.db")
# 可控时钟测试直接驱动 main.auto_tick()，不需要真实后台线程
os.environ["AUTO_WORKER"] = "0"
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402

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
CUES2 = CUES3[:2]
CUES5 = CUES3 + [{"number": "A4", "content": "谢幕"}, {"number": "A5", "content": "散场灯"}]

REAL_CLOCK = main._clock  # 模块原始时钟（真实时间），真实线程冒烟测试用


class FakeClock:
    """可控时钟：只在测试显式推进时流逝。"""

    def __init__(self, t=1_000.0):
        self.t = float(t)

    def now(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def use_fake_clock(t=1_000.0):
    clock = FakeClock(t)
    main._clock = clock.now
    return clock


def fresh_db(name):
    main.DB_PATH = os.path.join(_tmp, name)


def setup_show(c, cues=None, headers=None):
    r = c.post("/api/setup", json={"cues": cues or CUES3}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def set_auto(c, v, enabled, interval=None, headers=None):
    body = {"version": v, "enabled": enabled}
    if interval is not None:
        body["interval"] = interval
    return c.post("/api/auto", json=body, headers=headers)


def enable_auto(c, v, interval=10, headers=None):
    r = set_auto(c, v, True, interval, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def confirm(c, v, headers=None):
    return c.post("/api/confirm", json={"version": v}, headers=headers)


def report_both(c, state):
    cur = state["current"]
    rnd = state["verify"]["round"]
    for dept in ("lighting", "sound"):
        r = c.post("/api/verify/report", json={
            "department": dept, "cue_id": cur["id"], "round": rnd, "ready": True,
        })
        assert r.status_code == 200, r.text


def test_default_off():
    fresh_db("a01.db")
    use_fake_clock()
    with TestClient(main.app) as c:
        print("A1 默认关闭：状态含关闭视图，后台任务不动作，旧调用不变")
        s = c.get("/api/state").json()
        check(s["auto"] == {
            "enabled": False, "interval": 10,
            "remaining_seconds": None, "waiting_ready": False,
        }, "初始 auto 为固定关闭视图（缺省间隔 10 秒）")
        s = setup_show(c)
        check(s["auto"]["enabled"] is False, "开场后自动模式仍关闭")
        v = s["version"]
        check(main.auto_tick() is False, "关闭时后台任务不发出")
        s = c.get("/api/state").json()
        check(s["version"] == v and s["issued"] == [], "关闭时后台任务不改状态")
        r = confirm(c, v)
        check(r.status_code == 200, "关闭时手工确认不变（旧行为）")


def test_settings_rules():
    fresh_db("a02.db")
    use_fake_clock()
    with TestClient(main.app) as c:
        print("A2 设置规则：仅演出中、版本 +1、间隔 1～60 整数、400/409")
        v = c.get("/api/state").json()["version"]
        r = set_auto(c, v, True, 10)
        check(r.status_code == 409, "开场前设置返回 409")
        check(r.json()["state"]["auto"]["enabled"] is False, "409 附最新状态")

        s = setup_show(c)
        v = s["version"]
        for bad in (0, 61, -5, 1.5, "10", True):
            body = {"version": v, "enabled": True, "interval": bad}
            r = c.post("/api/auto", json=body)
            check(r.status_code == 400, f"非法间隔 {bad!r} 返回 400")
        check(c.get("/api/state").json()["version"] == v, "非法间隔不改版本号")
        r = c.post("/api/auto", json={"version": v + 99, "enabled": True,
                                      "interval": 1.5})
        check(r.status_code == 400, "非法间隔先于版本检查返回 400")
        # 显式 null 与缺省相同：沿用已存间隔
        r = c.post("/api/auto", json={"version": v, "enabled": True,
                                      "interval": None})
        check(r.status_code == 200, "显式 null 间隔按缺省处理")
        check(r.json()["auto"]["interval"] == 10, "null 间隔沿用已存间隔")
        check(r.json()["auto"]["enabled"] is True, "null 间隔开启成功")
        r = set_auto(c, r.json()["version"], False)
        v = r.json()["version"]

        r = set_auto(c, v, True)  # 缺省间隔：沿用已存（初始缺省 10）
        check(r.status_code == 200, "开启不带间隔：沿用已存间隔")
        check(r.json()["version"] == v + 1, "成功版本 +1")
        a = r.json()["auto"]
        check(a["enabled"] is True and a["interval"] == 10, "缺省间隔 10 秒")
        check(a["remaining_seconds"] == 10.0, "开启后从完整间隔计时")
        v = r.json()["version"]

        r = set_auto(c, v + 99, False)
        check(r.status_code == 409, "过期版本设置返回 409")
        check(c.get("/api/state").json()["auto"]["enabled"] is True,
              "过期设置不生效")

        r = set_auto(c, v, False, 30)  # 关闭时一并保存新间隔
        check(r.status_code == 200 and r.json()["auto"]["enabled"] is False,
              "关闭成功")
        check(r.json()["auto"]["interval"] == 30, "关闭后保留新设间隔")
        v = r.json()["version"]
        r = set_auto(c, v, True)
        check(r.json()["auto"]["interval"] == 30, "再次开启沿用保留的间隔")
        v = r.json()["version"]

        for edge in (1, 60):
            r = set_auto(c, v, True, edge)
            check(r.status_code == 200 and r.json()["auto"]["interval"] == edge,
                  f"边界间隔 {edge} 秒合法")
            v = r.json()["version"]
        r = set_auto(c, v, True, 10.0)  # 整数值浮点视为整数
        check(r.status_code == 200 and r.json()["auto"]["interval"] == 10,
              "整数值浮点（10.0）按整数接受")

        # 已完成：设置返回 409
        while True:
            s = c.get("/api/state").json()
            if s["status"] == "done":
                break
            r = confirm(c, s["version"])
            assert r.status_code == 200, r.text
        v = s["version"]
        r = set_auto(c, v, True, 10)
        check(r.status_code == 409, "已完成设置返回 409")


def test_settings_control_seat():
    fresh_db("a03.db")
    use_fake_clock()
    with TestClient(main.app) as c:
        print("A3 控制席保护：无凭据 403 且数据不变，凭据可设置")
        v = c.get("/api/state").json()["version"]
        r = c.post("/api/control/enable", json={"version": v})
        token = r.json()["control_token"]
        v = r.json()["version"]
        r = c.post("/api/setup", json={"cues": CUES2},
                   headers={"X-Control-Token": token})
        check(r.status_code == 200, "控制席凭据可开场")
        s = r.json()
        v = s["version"]
        r = set_auto(c, v, True, 5)
        check(r.status_code == 403, "启用控制席后无凭据设置返回 403")
        s = c.get("/api/state").json()
        check(s["auto"]["enabled"] is False and s["version"] == v,
              "403 不改数据与版本")
        r = set_auto(c, v, True, 5, headers={"X-Control-Token": token})
        check(r.status_code == 200 and r.json()["auto"]["enabled"] is True,
              "携带当前凭据设置成功")


def test_timing_and_issue():
    fresh_db("a04.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A4 计时与发出：等满间隔才发出，每次一条，下一条重新计时")
        s = setup_show(c)
        v = s["version"]
        s = enable_auto(c, v, 10)
        v = s["version"]
        check(s["auto"]["remaining_seconds"] == 10.0, "开启后剩余为完整间隔")

        clock.advance(9.9)
        check(main.auto_tick() is False, "未满间隔不发出")
        s = c.get("/api/state").json()
        check(s["version"] == v and s["issued"] == [], "未发出时版本与进度不变")
        check(s["auto"]["remaining_seconds"] == 0.1, "剩余秒数随时间递减")

        # 开关与间隔都未变的重复设置：推进版本但不重置计时
        r = set_auto(c, v, True, 10)
        check(r.status_code == 200, "同间隔重复开启成功")
        check(r.json()["auto"]["remaining_seconds"] == 0.1,
              "同间隔重复开启不重置计时")
        v = r.json()["version"]

        clock.advance(0.2)  # 到期
        check(main.auto_tick() is True, "到期后台任务发出一条")
        s = c.get("/api/state").json()
        check(s["version"] == v + 1, "自动发出推进版本 +1")
        check(len(s["issued"]) == 1 and s["issued"][0]["number"] == "A1",
              "发出当前提示")
        check(s["issued"][0]["content"] == "灯光渐亮", "已发为发出时内容快照")
        check(s["current"]["number"] == "A2", "推进到下一条")
        check(s["auto"]["enabled"] is True
              and s["auto"]["remaining_seconds"] == 10.0,
              "下一条从完整间隔重新计时")

        check(main.auto_tick() is False, "同刻不再发第二条（每次只发一条）")
        s2 = c.get("/api/state").json()
        check(len(s2["issued"]) == 1 and s2["version"] == s["version"],
              "不补发积压")


def test_pause_freeze_resume():
    fresh_db("a05.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A5 暂停冻结剩余时间，恢复续计；到期后暂停、暂停中开启")
        s = setup_show(c)
        v = enable_auto(c, s["version"], 10)["version"]
        clock.advance(4)
        r = c.post("/api/pause", json={"version": v, "paused": True})
        check(r.status_code == 200, "暂停成功")
        check(r.json()["auto"]["remaining_seconds"] == 6.0,
              "暂停冻结剩余时间（10 - 4 = 6 秒）")
        v = r.json()["version"]
        clock.advance(100)
        check(main.auto_tick() is False, "暂停期间到期也不发出")
        r = c.post("/api/pause", json={"version": v, "paused": False})
        check(r.status_code == 200, "恢复成功")
        check(r.json()["auto"]["remaining_seconds"] == 6.0, "恢复按冻结剩余续计")
        v = r.json()["version"]
        clock.advance(5.9)
        check(main.auto_tick() is False, "续计未满不发出")
        clock.advance(0.2)
        check(main.auto_tick() is True, "续计满后发出")

        # 到期后暂停：剩余按 0 冻结，恢复后立即可发
        clock.advance(20)  # 远超间隔
        s = c.get("/api/state").json()
        r = c.post("/api/pause", json={"version": s["version"], "paused": True})
        check(r.json()["auto"]["remaining_seconds"] == 0.0,
              "到期后暂停：剩余按 0 冻结")
        v = r.json()["version"]
        clock.advance(50)
        check(main.auto_tick() is False, "暂停中仍不发出")
        r = c.post("/api/pause", json={"version": v, "paused": False})
        v = r.json()["version"]
        check(main.auto_tick() is True, "恢复后立即发出（剩余为 0）")

        # 暂停中开启自动：冻结完整间隔，恢复后续计
        s = c.get("/api/state").json()
        r = c.post("/api/pause", json={"version": s["version"], "paused": True})
        v = r.json()["version"]
        r = set_auto(c, v, False)
        v = r.json()["version"]
        r = set_auto(c, v, True, 8)
        check(r.status_code == 200, "暂停中可开启自动")
        check(r.json()["auto"]["remaining_seconds"] == 8.0,
              "暂停中开启：冻结完整间隔")
        v = r.json()["version"]
        clock.advance(30)
        check(main.auto_tick() is False, "暂停中不计时")
        r = c.post("/api/pause", json={"version": v, "paused": False})
        clock.advance(7.9)
        check(main.auto_tick() is False, "恢复后续计未满不发出")
        clock.advance(0.2)
        check(main.auto_tick() is True, "恢复后续计满发出")


def test_verify_gate_waits():
    fresh_db("a06.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A6 核验门槛：到期未就绪继续等，就绪后发出；不补发积压")
        v = c.get("/api/state").json()["version"]
        r = c.post("/api/verify/enable", json={"version": v})
        v = r.json()["version"]
        s = setup_show(c)
        v = enable_auto(c, s["version"], 10)["version"]

        clock.advance(15)  # 已到期但未报就绪
        check(main.auto_tick() is False, "到期未就绪不发出")
        s = c.get("/api/state").json()
        check(s["version"] == v and s["issued"] == [], "等待就绪不改版本与进度")
        check(s["auto"]["waiting_ready"] is True, "状态标记等待就绪")
        check(s["auto"]["remaining_seconds"] == 0.0, "到期剩余为 0 继续等")

        cur, rnd = s["current"]["id"], s["verify"]["round"]
        r = c.post("/api/verify/report", json={
            "department": "lighting", "cue_id": cur, "round": rnd, "ready": True})
        check(r.status_code == 200, "灯光先报就绪")
        check(main.auto_tick() is False, "缺一工种仍不发出")
        r = c.post("/api/verify/report", json={
            "department": "sound", "cue_id": cur, "round": rnd, "ready": True})
        check(r.status_code == 200, "音响报就绪")
        check(main.auto_tick() is True, "两工种就绪后发出")
        s = c.get("/api/state").json()
        check(s["issued"][0]["number"] == "A1", "发出当前提示")
        check(s["verify"]["round"] == rnd + 1, "自动发出同样推进轮次")
        check(s["verify"]["lighting"] is None, "新轮次就绪已清空")
        check(s["auto"]["waiting_ready"] is False, "发出后不再等待就绪")

        # 不补发积压：间隔 10 秒，一次性推进 55 秒也只发一条
        report_both(c, s)
        clock.advance(55)
        check(main.auto_tick() is True, "积压多时到期发出一条")
        s = c.get("/api/state").json()
        check(len(s["issued"]) == 2, "一次只发一条，不补发积压")
        check(s["auto"]["remaining_seconds"] == 10.0, "下一条重新计满间隔")
        check(main.auto_tick() is False, "同刻不连发")


def test_manual_ops_restart_timer():
    fresh_db("a07.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A7 手工确认 / 撤回 / 改写当前 / 重排换头重新计时")
        s = setup_show(c, CUES5)
        v = enable_auto(c, s["version"], 10)["version"]

        # 手工确认重新计时
        clock.advance(8)
        r = confirm(c, v)
        check(r.status_code == 200, "到期前手工确认成功")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "手工确认后重新计满间隔")
        v = r.json()["version"]
        clock.advance(9.9)
        check(main.auto_tick() is False, "手工确认后未满新间隔不发出")
        clock.advance(0.2)
        check(main.auto_tick() is True, "手工确认后满新间隔发出")

        # 撤回末条重新计时
        clock.advance(5)
        s = c.get("/api/state").json()
        last = s["issued"][-1]
        r = c.post("/api/withdraw", json={"version": s["version"],
                                          "cue_id": last["id"]})
        check(r.status_code == 200, "撤回末条成功")
        check(r.json()["auto"]["remaining_seconds"] == 10.0, "撤回后重新计满间隔")
        v = r.json()["version"]
        clock.advance(9.9)
        check(main.auto_tick() is False, "撤回后未满新间隔不发出")
        clock.advance(0.2)
        check(main.auto_tick() is True, "撤回后满新间隔发出")

        # 改写当前提示重新计时
        clock.advance(5)
        s = c.get("/api/state").json()
        cur = s["current"]
        r = c.post("/api/edit", json={"version": s["version"],
                                      "cue_id": cur["id"], "content": "改过的内容"})
        check(r.status_code == 200, "改写当前提示成功")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "改写当前提示后重新计满间隔")
        clock.advance(9.9)
        check(main.auto_tick() is False, "改写后未满新间隔不发出")
        clock.advance(0.2)
        check(main.auto_tick() is True, "改写后满新间隔发出")

        # 重排换头重新计时
        clock.advance(5)
        s = c.get("/api/state").json()
        ids = [p["id"] for p in s["pending"]]
        r = c.post("/api/reorder", json={"version": s["version"],
                                         "order": [ids[1], ids[0]]})
        check(r.status_code == 200, "重排换头成功")
        check(r.json()["auto"]["remaining_seconds"] == 10.0,
              "重排换头后重新计满间隔")
        clock.advance(9.9)
        check(main.auto_tick() is False, "换头后未满新间隔不发出")
        clock.advance(0.2)
        check(main.auto_tick() is True, "换头后满新间隔发出")


def test_non_restarting_ops():
    fresh_db("a08.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A8 队首不变的重排 / 编辑其他提示 / 记问题不重置计时；"
              "演出中录入新场重新计时")
        s = setup_show(c)
        v = enable_auto(c, s["version"], 10)["version"]

        # 编辑其他（非队首）提示不重置
        clock.advance(4)
        s = c.get("/api/state").json()
        other = s["pending"][1]
        r = c.post("/api/edit", json={"version": s["version"],
                                      "cue_id": other["id"], "content": "别的内容"})
        check(r.status_code == 200, "编辑其他提示成功")
        check(r.json()["auto"]["remaining_seconds"] == 6.0,
              "编辑其他提示不重置计时")
        # 队首不变的重排不重置
        s = c.get("/api/state").json()
        ids = [p["id"] for p in s["pending"]]
        r = c.post("/api/reorder", json={"version": s["version"],
                                         "order": [ids[0], ids[2], ids[1]]})
        check(r.status_code == 200, "队首不变的重排成功")
        check(r.json()["auto"]["remaining_seconds"] == 6.0,
              "队首不变的重排不重置计时")
        # 记现场问题不重置
        s = c.get("/api/state").json()
        r = c.post("/api/issues", json={"version": s["version"],
                                        "cue_id": s["current"]["id"],
                                        "text": "现场记录一条"})
        check(r.status_code == 200, "记现场问题成功")
        check(r.json()["auto"]["remaining_seconds"] == 6.0, "记问题不重置计时")
        clock.advance(6.1)  # 累计 10.1 秒：仍按原计时到期
        check(main.auto_tick() is True, "未被重置的计时照常到期发出")

        # 演出中直接录入新场：自动保持开启并重新计满间隔
        clock.advance(3)
        s = setup_show(c, CUES2)
        check(s["auto"]["enabled"] is True, "演出中录入新场自动保持开启")
        check(s["auto"]["remaining_seconds"] == 10.0, "新场首条重新计满间隔")
        clock.advance(9.9)
        check(main.auto_tick() is False, "新场未满间隔不发出")
        clock.advance(0.2)
        check(main.auto_tick() is True, "新场满间隔发出")


def test_competition_with_manual():
    fresh_db("a09.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A9 与手工确认竞争：不重复发出、不跳条")
        s = setup_show(c)
        v = enable_auto(c, s["version"], 10)["version"]

        # 手工先确认：自动到期检查落空，不重复发出
        clock.advance(10)
        r = confirm(c, v)
        check(r.status_code == 200, "到期瞬间手工确认先成功")
        v = r.json()["version"]
        check(main.auto_tick() is False, "手工已确认：自动不再重复发出")
        s = c.get("/api/state").json()
        check(len(s["issued"]) == 1 and s["version"] == v,
              "只发出一条（无重复）")

        # 自动先发出：手工旧版本确认被 409，不跳条
        clock.advance(10)
        check(main.auto_tick() is True, "再次到期自动发出")
        s = c.get("/api/state").json()
        check(len(s["issued"]) == 2 and s["version"] == v + 1,
              "自动发出第二条并推进版本")
        r = confirm(c, v)  # 旧版本
        check(r.status_code == 409, "自动先发出：手工旧版本确认 409")
        s = c.get("/api/state").json()
        check(len(s["issued"]) == 2 and s["current"]["number"] == "A3",
              "不跳条：当前仍为第三条")


def test_disable_conditions():
    fresh_db("a10.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A10 接管 / 归档 / 重置 / 完成关闭自动模式")
        # 接管关闭
        v = c.get("/api/state").json()["version"]
        r = c.post("/api/control/enable", json={"version": v})
        token = r.json()["control_token"]
        v = r.json()["version"]
        h = {"X-Control-Token": token}
        s = setup_show(c, CUES2, headers=h)
        v = enable_auto(c, s["version"], 10, headers=h)["version"]
        r = c.post("/api/control/takeover", json={"version": v})
        check(r.status_code == 200, "接管成功")
        check(r.json()["auto"]["enabled"] is False, "接管关闭自动模式")
        token = r.json()["control_token"]
        h = {"X-Control-Token": token}
        clock.advance(30)
        check(main.auto_tick() is False, "接管后后台不再发出")

        # 归档关闭
        v = r.json()["version"]
        v = enable_auto(c, v, 10, headers=h)["version"]
        r = c.post("/api/archive", json={"version": v, "name": "第一场"},
                   headers=h)
        check(r.status_code == 200, "归档成功")
        check(r.json()["auto"]["enabled"] is False, "归档关闭自动模式")
        clock.advance(30)
        check(main.auto_tick() is False, "归档后后台不再发出")

        # 重置关闭
        s = setup_show(c, CUES2, headers=h)
        v = enable_auto(c, s["version"], 10, headers=h)["version"]
        r = c.post("/api/reset", headers=h)
        check(r.status_code == 200, "重置成功")
        check(r.json()["auto"]["enabled"] is False, "重置关闭自动模式")

        # 完成关闭（自动发出最后一条）
        s = setup_show(c, CUES2, headers=h)
        v = enable_auto(c, s["version"], 10, headers=h)["version"]
        clock.advance(10)
        check(main.auto_tick() is True, "自动发出第一条")
        clock.advance(10)
        check(main.auto_tick() is True, "自动发出最后一条")
        s = c.get("/api/state").json()
        check(s["status"] == "done", "全部发出进入已完成")
        check(s["auto"]["enabled"] is False, "完成关闭自动模式")
        check(main.auto_tick() is False, "完成后后台不再发出")

        # 完成关闭（手工确认最后一条）
        s = setup_show(c, CUES2, headers=h)
        v = enable_auto(c, s["version"], 10, headers=h)["version"]
        r = confirm(c, v, headers=h)
        r = confirm(c, r.json()["version"], headers=h)
        check(r.json()["status"] == "done", "手工确认全部发出")
        check(r.json()["auto"]["enabled"] is False, "手工完成同样关闭自动模式")


def test_restart_disables():
    fresh_db("a11.db")
    clock = use_fake_clock()
    print("A11 服务重启关闭自动模式（间隔保留）")
    with TestClient(main.app) as c:
        s = setup_show(c)
        v = enable_auto(c, s["version"], 15)["version"]
        check(c.get("/api/state").json()["auto"]["enabled"] is True, "重启前已开启")
    # 模拟服务重启：同一数据库重新进入应用（lifespan 启动）
    with TestClient(main.app) as c:
        s = c.get("/api/state").json()
        check(s["auto"]["enabled"] is False, "重启后自动模式关闭")
        check(s["auto"]["interval"] == 15, "重启后已设间隔保留")
        check(s["auto"]["remaining_seconds"] is None, "重启后计时清空")
        clock.advance(30)
        check(main.auto_tick() is False, "重启后后台不再发出")


def test_write_failure_rollback():
    fresh_db("a12.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A12 写入失败整体回滚：不推进、不重置计时、不改版本")
        s = setup_show(c)
        v = enable_auto(c, s["version"], 10)["version"]

        # 设置接口写入失败：开关与版本都不变
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_meta_update BEFORE UPDATE ON meta "
                "BEGIN SELECT RAISE(ABORT, 'injected meta failure'); END"
            )
        try:
            raised = False
            try:
                set_auto(c, v, True, 20)
            except Exception:
                raised = True
            check(raised, "注入写入失败：设置请求抛出异常（不被吞掉）")
            s = c.get("/api/state").json()
            check(s["version"] == v and s["auto"]["interval"] == 10,
                  "设置失败：版本与间隔不变")

            # 后台任务写入失败：不推进、不重置计时、不改版本
            clock.advance(10)  # 到期
            raised = False
            try:
                main.auto_tick()
            except Exception:
                raised = True
            check(raised, "注入写入失败：后台任务抛出异常（不被吞掉）")
            s = c.get("/api/state").json()
            check(s["version"] == v and s["issued"] == [],
                  "后台写入失败：版本与进度不变")
            check(s["auto"]["remaining_seconds"] == 0.0,
                  "后台写入失败：计时未被重置（仍到期）")
        finally:
            with main.db() as conn:
                conn.execute("DROP TRIGGER fail_meta_update")

        check(main.auto_tick() is True, "故障解除后到期正常发出")
        s = c.get("/api/state").json()
        check(s["version"] == v + 1 and len(s["issued"]) == 1,
              "故障解除后正常推进")


def test_snapshot_and_round_rules():
    fresh_db("a13.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("A13 沿用发出快照与换轮规则")
        v = c.get("/api/state").json()["version"]
        r = c.post("/api/verify/enable", json={"version": v})
        v = r.json()["version"]
        s = setup_show(c, CUES2)
        v = enable_auto(c, s["version"], 10)["version"]
        clock.advance(2)
        s = c.get("/api/state").json()
        r = c.post("/api/edit", json={"version": s["version"],
                                      "cue_id": s["current"]["id"],
                                      "content": "灯光慢亮（改过）"})
        v = r.json()["version"]
        report_both(c, c.get("/api/state").json())
        rnd = c.get("/api/state").json()["verify"]["round"]
        clock.advance(10)  # 改写当前提示已重新计时，此处满新间隔
        check(main.auto_tick() is True, "到期且就绪：自动发出")
        s = c.get("/api/state").json()
        check(s["issued"][0]["content"] == "灯光慢亮（改过）",
              "已发为发出那一刻的内容快照")
        check(s["issued"][0]["issued_at"], "记录发出时间")
        check(s["verify"]["round"] == rnd + 1, "自动发出开启新轮次")
        check(s["verify"]["lighting"] is None and s["verify"]["sound"] is None,
              "新轮次就绪清空")
        check(main.auto_tick() is False, "新轮次未就绪不发出")


def test_real_worker_background():
    fresh_db("a14.db")
    main._clock = REAL_CLOCK  # 真实后台线程配真实时钟
    import time as _time

    with TestClient(main.app) as c:
        print("A14 真实后台线程：无需页面保持打开，到期自动发出")
        s = setup_show(c, CUES2)
        v = enable_auto(c, s["version"], 1)["version"]
        main.start_auto_worker()
        try:
            deadline = _time.time() + 3.0
            issued = 0
            while _time.time() < deadline:
                issued = len(c.get("/api/state").json()["issued"])
                if issued >= 1:
                    break
                _time.sleep(0.1)
            check(issued == 1, "后台线程在到期后自动发出一条（无任何页面操作）")
            s = c.get("/api/state").json()
            check(s["version"] == v + 1, "自动发出推进版本")
            check(s["auto"]["enabled"] is True, "发出后自动模式仍开启")
        finally:
            main.stop_auto_worker()
        s = c.get("/api/state").json()
        v2, n2 = s["version"], len(s["issued"])
        _time.sleep(1.3)  # 线程已停：即使间隔已过也不再发出
        s = c.get("/api/state").json()
        check(s["version"] == v2 and len(s["issued"]) == n2,
              "后台线程停止后不再自动发出")


test_default_off()
test_settings_rules()
test_settings_control_seat()
test_timing_and_issue()
test_pause_freeze_resume()
test_verify_gate_waits()
test_manual_ops_restart_timer()
test_non_restarting_ops()
test_competition_with_manual()
test_disable_conditions()
test_restart_disables()
test_write_failure_rollback()
test_snapshot_and_round_rules()
test_real_worker_background()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
