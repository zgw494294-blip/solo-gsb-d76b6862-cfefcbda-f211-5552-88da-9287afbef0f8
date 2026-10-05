"""本场核验后端回归测试（FastAPI TestClient + 独立临时数据库）。

覆盖：默认关闭旧调用不变；开场前凭版本号启用（受控制席保护、版本 +1、
已启用/非开场前/版本过期 409）；工种就绪回报接受可选 X-Control-Token——
无需凭据、不改全局版本号，成功与 409 的 control_granted 按本次凭据回报
（当前席 / 无凭据页 / 被接管旧页，不泄露凭据、不 403），仅接受演出中
（含暂停）当前提示与当前轮次，过期 409 及最新状态，同工种同轮次覆盖写
（含取消就绪）；启用核验后确认缺任一就绪 409，两工种就绪才可发出；确认
推进 / 撤回 / 改写当前提示 / 重排换头开启新轮次并清空就绪（轮次不可复用），
编辑其他提示 / 暂停恢复 / 接管 / 记问题不清空；撤回与确认同版本竞争最多一个
成功；写入失败整体回滚；轮次与回报重启保留；归档 / 重置关闭核验并清空、
下一场不继承、档案结构不变。

运行：python3 tests/verify.test.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-verify-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "cues.db")
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


def fresh_db(name):
    main.DB_PATH = os.path.join(_tmp, name)


def setup_show(c, cues=None):
    r = c.post("/api/setup", json={"cues": cues or CUES3})
    assert r.status_code == 200, r.text
    return r.json()


def enable_verify(c, v, headers=None):
    return c.post("/api/verify/enable", json={"version": v}, headers=headers)


def report(c, dept, cue_id, rnd, ready, headers=None):
    return c.post(
        "/api/verify/report",
        json={"department": dept, "cue_id": cue_id, "round": rnd, "ready": ready},
        headers=headers,
    )


def report_both(c, state):
    """两工种都对当前提示、当前轮次报就绪，返回最后一次响应状态。"""
    cur = state["current"]
    rnd = state["verify"]["round"]
    r = report(c, "lighting", cur["id"], rnd, True)
    assert r.status_code == 200, r.text
    r = report(c, "sound", cur["id"], rnd, True)
    assert r.status_code == 200, r.text
    return r.json()


def confirm(c, v, headers=None):
    return c.post("/api/confirm", json={"version": v}, headers=headers)


def test_default_off_old_calls_unchanged():
    fresh_db("v1.db")
    with TestClient(main.app) as c:
        print("V1 默认关闭：旧调用不变，状态含关闭视图")
        s = c.get("/api/state").json()
        check(s["verify"] == {
            "enabled": False, "round": 0, "cue_id": None,
            "lighting": None, "sound": None, "all_ready": False,
        }, "初始 verify 为固定关闭视图")

        s = setup_show(c)
        v = s["version"]
        check(s["verify"]["enabled"] is False, "开场后核验仍关闭（默认关闭）")
        # 关闭时确认不需要任何就绪回报：旧行为不变
        r = confirm(c, v)
        check(r.status_code == 200, "关闭核验时确认无需就绪回报（旧调用不变）")
        v = r.json()["version"]
        check(r.json()["verify"]["enabled"] is False, "确认后核验仍关闭")
        # 关闭时回报被拒绝（409 附最新状态），不改变版本
        cur = r.json()["current"]
        r = report(c, "lighting", cur["id"], 1, True)
        check(r.status_code == 409, "核验未启用时回报返回 409")
        check(r.json()["state"]["verify"]["enabled"] is False, "409 附最新状态")
        check(c.get("/api/state").json()["version"] == v, "失败回报不改版本号")


def test_enable_rules():
    fresh_db("v2.db")
    with TestClient(main.app) as c:
        print("V2 启用：仅开场前、凭版本号、版本 +1；重复/过期/演出中 409")
        s = c.get("/api/state").json()
        v = s["version"]
        r = enable_verify(c, v + 99)
        check(r.status_code == 409, "启用携带过期版本返回 409")
        check(c.get("/api/state").json()["verify"]["enabled"] is False,
              "过期启用不生效")

        r = enable_verify(c, v)
        check(r.status_code == 200, "开场前凭当前版本号启用成功")
        check(r.json()["verify"]["enabled"] is True, "启用后 verify.enabled 为 true")
        check(r.json()["verify"]["round"] == 0, "启用时轮次为 0（尚未开场）")
        check(r.json()["version"] == v + 1, "启用与版本 +1 一起落库")
        v = r.json()["version"]

        r = enable_verify(c, v)
        check(r.status_code == 409, "已启用时重复启用返回 409")
        check(c.get("/api/state").json()["version"] == v, "重复启用不改版本")

        # 开场后不能再启用（仅开场前）
        s = setup_show(c)
        v = s["version"]
        check(s["verify"]["enabled"] is True, "开场后核验保持启用")
        check(s["verify"]["round"] == 1, "开场后从第 1 轮开始")
        check(s["verify"]["cue_id"] == s["current"]["id"], "轮次对应当前提示")
        r = enable_verify(c, v)
        check(r.status_code == 409, "演出中启用返回 409（仅开场前可启用）")

        # 全部发出进入完成态：同样不能再启用（已启用也是 409）
        for _ in range(3):
            s = report_both(c, c.get("/api/state").json())
            r = confirm(c, s["version"])
            check(r.status_code == 200, "两工种就绪后确认成功")
        check(r.json()["status"] == "done", "全部发出进入已完成")
        r = enable_verify(c, r.json()["version"])
        check(r.status_code == 409, "已完成时启用返回 409")


def test_enable_control_seat_protection():
    fresh_db("v3.db")
    with TestClient(main.app) as c:
        print("V3 启用受控制席保护；回报无需凭据；确认等旧接口保护规则不变")
        s = setup_show(c)
        v = s["version"]
        r = c.post("/api/control/enable", json={"version": v})
        tok = r.json()["control_token"]
        v = r.json()["version"]
        # 回到开场前再启用核验
        r = c.post("/api/reset", headers={"X-Control-Token": tok})
        v = r.json()["version"]

        r = enable_verify(c, v)
        check(r.status_code == 403, "启用控制席后无凭据启用核验返回 403")
        r = enable_verify(c, v, headers={"X-Control-Token": "wrong"})
        check(r.status_code == 403, "错误凭据启用核验返回 403")
        check(c.get("/api/state").json()["verify"]["enabled"] is False,
              "403 后核验仍未启用")
        r = enable_verify(c, v, headers={"X-Control-Token": tok})
        check(r.status_code == 200, "正确凭据启用核验成功")
        v = r.json()["version"]

        r = c.post("/api/setup", json={"cues": CUES3},
                   headers={"X-Control-Token": tok})
        check(r.status_code == 200, "凭据有效：开场成功")
        s = r.json()
        v = s["version"]
        cur = s["current"]
        rnd = s["verify"]["round"]

        # 回报无需控制凭据：无凭据、错凭据都可用（工种回报不对控制席负责）
        r = report(c, "lighting", cur["id"], rnd, True)
        check(r.status_code == 200, "启用控制席后回报无需凭据")
        r = report(c, "sound", cur["id"], rnd, True,
                   headers={"X-Control-Token": "wrong"})
        check(r.status_code == 200, "回报携带无关凭据头也不受影响")
        # 确认等旧写接口仍受控制席保护：无凭据 403
        r = confirm(c, v)
        check(r.status_code == 403, "确认仍受控制席保护（无凭据 403）")
        r = confirm(c, v, headers={"X-Control-Token": tok})
        check(r.status_code == 200, "凭据有效且两工种就绪：确认成功")


def test_report_control_granted_echo():
    fresh_db("v3b.db")
    with TestClient(main.app) as c:
        print("V3b 回报接受可选 X-Control-Token：成功/409 的 control_granted "
              "按本次凭据回报，不泄露凭据、不改版本、不 403")

        def token_header(tok):
            return {"X-Control-Token": tok} if tok else None

        # 同时启用单控制席与本场核验：启用核验 -> 启用控制席 -> 开场
        v = c.get("/api/state").json()["version"]
        r = enable_verify(c, v)
        check(r.status_code == 200, "开场前启用核验")
        v = r.json()["version"]
        r = c.post("/api/control/enable", json={"version": v})
        check(r.status_code == 200, "启用单控制席")
        tok = r.json()["control_token"]
        v = r.json()["version"]
        r = c.post("/api/setup", json={"cues": CUES3}, headers=token_header(tok))
        check(r.status_code == 200, "凭据有效：开场成功")
        s = r.json()
        v = s["version"]
        cur = s["current"]
        rnd = s["verify"]["round"]

        # 1) 当前控制席回报（成功）：control_granted 为 true，版本不变，
        #    响应不含凭据；两工种就绪且未暂停时可直接确认（门槛与 403 不变）
        r = report(c, "lighting", cur["id"], rnd, True, headers=token_header(tok))
        check(r.status_code == 200, "当前控制席：报就绪成功")
        check(r.json()["control_enabled"] is True, "响应含控制席启用标记")
        check(r.json()["control_granted"] is True, "当前席成功响应回报 control_granted=true")
        check("control_token" not in r.json(), "回报响应不泄露凭据")
        check(r.json()["version"] == v, "回报不改全局版本号")
        r = report(c, "sound", cur["id"], rnd, True, headers=token_header(tok))
        check(r.json()["control_granted"] is True, "当前席第二次回报仍具资格")
        r = confirm(c, v, headers=token_header(tok))
        check(r.status_code == 200, "两工种就绪且未暂停：当前席可直接确认")
        v2 = r.json()["version"]
        check(v2 == v + 1, "确认照常推进版本（+1）")

        # 当前席对新轮次取消就绪：仍 200、仍具资格
        cur2 = r.json()["current"]
        rnd2 = r.json()["verify"]["round"]
        report(c, "lighting", cur2["id"], rnd2, True, headers=token_header(tok))
        report(c, "sound", cur2["id"], rnd2, True, headers=token_header(tok))
        r = report(c, "lighting", cur2["id"], rnd2, False, headers=token_header(tok))
        check(r.status_code == 200 and r.json()["verify"]["lighting"] is False,
              "当前席：取消就绪成功（覆盖写为未就绪）")
        check(r.json()["control_granted"] is True, "取消就绪后当前席资格保留")
        r = confirm(c, v2, headers=token_header(tok))
        check(r.status_code == 409, "取消后缺就绪：确认门槛不变（409）")

        # 2) 当前席遇过期（旧轮次）：仍 409 且 control_granted=true，回报不写入
        r = report(c, "lighting", cur2["id"], rnd, True, headers=token_header(tok))
        check(r.status_code == 409, "当前席旧轮次回报返回 409")
        st = r.json()["state"]
        check(st["control_granted"] is True, "当前席 409 仍回报 control_granted=true")
        check("control_token" not in st, "409 状态不泄露凭据")
        check(st["version"] == v2, "409 不改版本")
        check(st["verify"]["round"] == rnd2 and st["verify"]["lighting"] is False,
              "409 附最新状态且过期回报未写入")

        # 3) 无凭据页回报（成功）：不 403，但 control_granted=false
        r = report(c, "lighting", cur2["id"], rnd2, True)
        check(r.status_code == 200, "无凭据页：同样可回报（不返回 403）")
        check(r.json()["control_granted"] is False, "无凭据成功响应回报 control_granted=false")
        # 无凭据页写操作仍 403（控制席写保护不变）
        r = confirm(c, v2)
        check(r.status_code == 403, "无凭据页确认仍 403（写保护不变）")
        # 无凭据页遇过期：409 且 control_granted=false
        r = report(c, "lighting", cur2["id"], rnd, True)
        check(r.status_code == 409, "无凭据页旧轮次回报 409")
        check(r.json()["state"]["control_granted"] is False,
              "无凭据页 409 回报 control_granted=false")

        # 4) 被接管的旧凭据：可回报、不 403、不获得控制权（成功与 409 均 false）
        r = c.post("/api/control/takeover", json={"version": v2})
        check(r.status_code == 200, "另一页凭最新版本号接管")
        new_tok = r.json()["control_token"]
        v3 = r.json()["version"]
        r = report(c, "sound", cur2["id"], rnd2, True, headers=token_header(tok))
        check(r.status_code == 200, "旧凭据回报成功（可回报、不 403、不获得控制权）")
        check(r.json()["control_granted"] is False, "旧凭据成功响应 control_granted=false")
        check("control_token" not in r.json(), "旧凭据响应不泄露新凭据")
        check(r.json()["version"] == v3, "接管后的回报不改版本")
        # 旧凭据遇过期：409 且 control_granted=false
        r = report(c, "lighting", cur2["id"], rnd, True, headers=token_header(tok))
        check(r.status_code == 409, "旧凭据旧轮次回报 409")
        check(r.json()["state"]["control_granted"] is False,
              "旧凭据 409 回报 control_granted=false（旧页据此清缓存）")
        # 新控制席回报：true
        r = report(c, "lighting", cur2["id"], rnd2, True, headers=token_header(new_tok))
        check(r.status_code == 200 and r.json()["control_granted"] is True,
              "新控制席回报 control_granted=true")
        # 旧凭据写操作仍 403（控制席写保护不变）
        r = confirm(c, v3, headers=token_header(tok))
        check(r.status_code == 403, "旧凭据确认仍 403（写保护不变）")
        r = confirm(c, v3, headers=token_header(new_tok))
        check(r.status_code == 200, "新凭据 + 两工种就绪：确认成功")


def test_report_rules():
    fresh_db("v4.db")
    with TestClient(main.app) as c:
        print("V4 回报：工种校验、仅演出中当前提示与轮次、覆盖写、不改版本")
        r = report(c, "dj", 1, 1, True)
        check(r.status_code == 400, "非法工种返回 400")

        v = c.get("/api/state").json()["version"]
        r = enable_verify(c, v)
        v = r.json()["version"]
        # 开场前（已启用未开场）：回报 409
        r = report(c, "lighting", 1, 0, True)
        check(r.status_code == 409, "开场前回报返回 409（仅演出中可回报）")

        s = setup_show(c)
        v = s["version"]
        cur = s["current"]
        rnd = s["verify"]["round"]
        check(s["verify"]["lighting"] is None and s["verify"]["sound"] is None,
              "开场时两工种均未回报")

        r = report(c, "lighting", cur["id"] + 999, rnd, True)
        check(r.status_code == 409, "cue_id 不是当前提示返回 409")
        check(r.json()["state"]["verify"]["round"] == rnd, "409 附最新状态（含轮次）")
        r = report(c, "lighting", cur["id"], rnd + 1, True)
        check(r.status_code == 409, "轮次不符返回 409")
        r = report(c, "lighting", cur["id"], rnd - 1, True)
        check(r.status_code == 409, "旧轮次回报返回 409（轮次不可复用）")

        before = c.get("/api/state").json()
        r = report(c, "lighting", cur["id"], rnd, True)
        check(r.status_code == 200, "当前提示 + 当前轮次回报成功")
        check(r.json()["verify"]["lighting"] is True, "灯光状态为就绪")
        check(r.json()["verify"]["sound"] is None, "音响仍未回报")
        check(r.json()["verify"]["all_ready"] is False, "未全部就绪")
        check(r.json()["version"] == before["version"], "回报不改全局版本号")
        check(r.json()["pending"] == before["pending"]
              and r.json()["issued"] == before["issued"], "回报不改提示与进度")

        # 同工种同轮次重复回报：覆盖写，以最后一次为准
        r = report(c, "lighting", cur["id"], rnd, False)
        check(r.status_code == 200 and r.json()["verify"]["lighting"] is False,
              "同工种重复回报覆盖为未就绪")
        r = report(c, "lighting", cur["id"], rnd, True)
        check(r.json()["verify"]["lighting"] is True, "再次回报覆盖回就绪")

        # 暂停可回报但不能发出
        r = c.post("/api/pause", json={"version": v, "paused": True})
        check(r.status_code == 200 and r.json()["paused"] is True, "暂停成功")
        v = r.json()["version"]
        r = report(c, "sound", cur["id"], rnd, True)
        check(r.status_code == 200 and r.json()["verify"]["all_ready"] is True,
              "暂停期间仍可回报（两工种就绪）")
        r = confirm(c, v)
        check(r.status_code == 409, "暂停期间确认仍被拒绝（可回报但不能发出）")
        r = c.post("/api/pause", json={"version": v, "paused": False})
        v = r.json()["version"]
        check(r.json()["verify"]["all_ready"] is True, "暂停恢复不清空本轮就绪")


def test_confirm_gate_and_round_advance():
    fresh_db("v5.db")
    with TestClient(main.app) as c:
        print("V5 确认门槛：缺任一就绪 409 及最新状态；确认推进开新轮次并清空")
        v = c.get("/api/state").json()["version"]
        r = enable_verify(c, v)
        v = r.json()["version"]
        s = setup_show(c)
        v = s["version"]
        cur = s["current"]
        rnd = s["verify"]["round"]

        r = confirm(c, v)
        check(r.status_code == 409, "两工种都未回报：确认 409")
        check("就绪" in r.json()["message"], "409 提示尚未全部就绪")
        check(r.json()["state"]["verify"]["enabled"] is True, "409 附最新状态")
        check(c.get("/api/state").json()["version"] == v, "确认被拒不改版本")

        r = report(c, "lighting", cur["id"], rnd, True)
        check(r.status_code == 200, "灯光报就绪")
        r = confirm(c, v)
        check(r.status_code == 409, "仅灯光就绪：确认仍 409（缺任一就绪不可发）")

        r = report(c, "sound", cur["id"], rnd, True)
        check(r.status_code == 200, "音响报就绪")
        r = confirm(c, v)
        check(r.status_code == 200, "两工种就绪：确认成功")
        s = r.json()
        check(s["verify"]["round"] == rnd + 1, "确认推进开启新轮次（+1）")
        check(s["verify"]["cue_id"] == s["current"]["id"], "新轮次对应新当前提示")
        check(s["verify"]["lighting"] is None and s["verify"]["sound"] is None,
              "新轮次就绪已清空")
        v = s["version"]

        # 旧轮次回报被拒；新一轮需重新报就绪
        r = report(c, "lighting", cur["id"], rnd, True)
        check(r.status_code == 409, "旧轮次回报被 409 拒绝（轮次不可复用）")
        r = confirm(c, v)
        check(r.status_code == 409, "新一轮未报就绪：确认仍 409")


def test_round_lifecycle():
    fresh_db("v6.db")
    with TestClient(main.app) as c:
        print("V6 轮次生命周期：换当前提示开新轮次；其他操作不清空")
        v = c.get("/api/state").json()["version"]
        v = enable_verify(c, v).json()["version"]
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]

        def ready_round():
            st = c.get("/api/state").json()
            return report_both(c, st)["verify"]["round"]

        # 编辑其他提示：不清空
        rnd = ready_round()
        r = c.post("/api/edit", json={"version": v, "cue_id": id3, "content": "幕落（改）"})
        check(r.status_code == 200, "编辑其他提示成功")
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd, "编辑其他提示不开新轮次")
        check(r.json()["verify"]["all_ready"] is True, "编辑其他提示不清空就绪")

        # 改写当前提示：开新轮次并清空
        r = c.post("/api/edit", json={"version": v, "cue_id": id1, "content": "灯光慢亮"})
        check(r.status_code == 200, "改写当前提示成功")
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd + 1, "改写当前提示开启新轮次")
        check(r.json()["verify"]["lighting"] is None, "改写当前提示清空就绪")

        # 重排不动队首：不清空；重排换队首：开新轮次
        rnd = ready_round()
        r = c.post("/api/reorder", json={"version": v, "order": [id1, id3, id2]})
        check(r.status_code == 200, "重排（队首不变）成功")
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd, "重排未换当前提示：轮次不变")
        check(r.json()["verify"]["all_ready"] is True, "重排未换当前提示：就绪保留")
        r = c.post("/api/reorder", json={"version": v, "order": [id3, id1, id2]})
        check(r.status_code == 200, "重排（换队首）成功")
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd + 1, "重排更换当前提示：开新轮次")
        check(r.json()["verify"]["lighting"] is None, "重排更换当前提示：清空就绪")

        # 记问题：不清空
        rnd = ready_round()
        r = c.post("/api/issues", json={"version": v, "cue_id": id1, "text": "待核对"})
        check(r.status_code == 200, "记问题成功")
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd, "记问题不开新轮次")
        check(r.json()["verify"]["all_ready"] is True, "记问题不清空就绪")

        # 暂停 / 恢复：不清空
        r = c.post("/api/pause", json={"version": v, "paused": True})
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd
              and r.json()["verify"]["all_ready"] is True, "暂停不清空就绪")
        r = c.post("/api/pause", json={"version": v, "paused": False})
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd
              and r.json()["verify"]["all_ready"] is True, "恢复不清空就绪")

        # 确认推进（当前为 id3）：开新轮次
        r = confirm(c, v)
        check(r.status_code == 200, "确认成功")
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd + 1, "确认推进开新轮次")
        check(r.json()["current"]["id"] == id1, "推进后当前为下一条")

        # 撤回：开新轮次并清空
        rnd = ready_round()
        r = c.post("/api/withdraw", json={"version": v, "cue_id": id3})
        check(r.status_code == 200, "撤回末条成功")
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd + 1, "撤回开启新轮次")
        check(r.json()["verify"]["lighting"] is None, "撤回清空就绪")
        check(r.json()["current"]["id"] == id3, "撤回提示回到当前")

        # 接管：不清空（需先启用控制席）
        rnd = ready_round()
        r = c.post("/api/control/enable", json={"version": v})
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd, "启用控制席不开新轮次")
        r = c.post("/api/control/takeover", json={"version": v})
        check(r.status_code == 200, "接管成功")
        tok = r.json()["control_token"]  # 接管换发新凭据，旧凭据立即失效
        v = r.json()["version"]
        check(r.json()["verify"]["round"] == rnd
              and r.json()["verify"]["all_ready"] is True, "接管不清空就绪")
        # 控制席启用后确认仍受就绪门槛约束
        r = confirm(c, v, headers={"X-Control-Token": tok})
        check(r.status_code == 200, "就绪齐全 + 凭据有效：确认成功")


def test_withdraw_confirm_race():
    fresh_db("v7.db")
    with TestClient(main.app) as c:
        print("V7 撤回与确认同版本竞争：最多一个成功，败者 409，轮次只进一次")
        v = c.get("/api/state").json()["version"]
        v = enable_verify(c, v).json()["version"]
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        s = report_both(c, c.get("/api/state").json())
        r = confirm(c, v)
        check(r.status_code == 200, "先确认一条")
        v = r.json()["version"]
        rnd = r.json()["verify"]["round"]
        report_both(c, c.get("/api/state").json())

        # 同版本：撤回（针对刚发出的 id1）与确认（针对当前 id2）竞争
        r1 = c.post("/api/withdraw", json={"version": v, "cue_id": id1})
        r2 = confirm(c, v)
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本撤回与确认：一个 200 一个 409")
        after = c.get("/api/state").json()
        check(after["version"] == v + 1, "竞争后版本只 +1")
        check(after["verify"]["round"] == rnd + 1, "轮次只推进一次")
        check(after["verify"]["lighting"] is None
              and after["verify"]["sound"] is None, "胜出方开启的新轮次就绪已清空")
        if r1.status_code == 200:
            check(after["current"]["id"] == id1 and len(after["issued"]) == 0,
                  "撤回先完成：确认失败，末条退回当前")
        else:
            check(len(after["issued"]) == 2,
                  "确认先完成：旧轮次撤回被拒，已发两条")


def test_confirm_write_failure_rollback():
    fresh_db("v8.db")
    with TestClient(main.app) as c:
        print("V8 确认写入失败整体回滚：轮次、就绪、提示、版本一律不变")
        v = c.get("/api/state").json()["version"]
        v = enable_verify(c, v).json()["version"]
        s = setup_show(c)
        v = s["version"]
        report_both(c, c.get("/api/state").json())
        before = c.get("/api/state").json()

        # 注入失败：meta 更新失败（在就绪清空、轮次推进、提示发出之后）
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_meta_update BEFORE UPDATE ON meta "
                "BEGIN SELECT RAISE(ABORT, 'injected meta failure'); END"
            )
        try:
            with TestClient(main.app, raise_server_exceptions=False) as fc:
                r = confirm(fc, v)
            check(r.status_code == 500, "注入写入失败：确认返回 500（异常不被吞掉）")
        finally:
            with main.db() as conn:
                conn.execute("DROP TRIGGER fail_meta_update")
        after = c.get("/api/state").json()
        check(after["version"] == before["version"], "失败后版本号不变")
        check(after["issued"] == before["issued"]
              and after["pending"] == before["pending"], "失败后提示与进度不变")
        check(after["verify"]["round"] == before["verify"]["round"],
              "失败后轮次不变（轮次推进随事务回滚）")
        check(after["verify"]["all_ready"] is True,
              "失败后就绪保留（清空随事务回滚，不留部分结果）")

        # 故障解除后可正常确认
        r = confirm(c, v)
        check(r.status_code == 200, "故障解除后确认正常")
        check(r.json()["verify"]["round"] == before["verify"]["round"] + 1,
              "确认成功后轮次正常推进")


def test_persistence_and_close():
    print("V9 轮次与回报重启保留；归档/重置关闭并清空；下一场不继承；档案结构不变")
    fresh_db("v9.db")
    with TestClient(main.app) as c:
        v = c.get("/api/state").json()["version"]
        v = enable_verify(c, v).json()["version"]
        s = setup_show(c)
        report_both(c, s)
        snap = c.get("/api/state").json()

    # 重启（重新建应用连接同一数据库）：轮次与回报保留
    with TestClient(main.app) as c:
        s = c.get("/api/state").json()
        check(s["verify"]["enabled"] is True, "重启后核验仍启用")
        check(s["verify"]["round"] == snap["verify"]["round"], "重启后轮次保留")
        check(s["verify"]["lighting"] is True and s["verify"]["sound"] is True,
              "重启后两工种就绪回报保留")

        # 归档：关闭核验并清空；档案结构不变
        v = s["version"]
        r = c.post("/api/archive", json={"version": v, "name": "核验场"})
        check(r.status_code == 200, "归档成功")
        s = r.json()
        check(s["verify"]["enabled"] is False, "归档关闭本场核验")
        check(s["verify"]["round"] == 0, "归档后轮次归零")
        aid = c.get("/api/archives").json()["archives"][0]["id"]
        detail = c.get(f"/api/archives/{aid}").json()
        check("verify" not in detail and "verify_round" not in detail,
              "档案结构不变（不含核验字段）")
        with main.db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM verify_reports").fetchone()["c"]
        check(n == 0, "归档清空就绪记录")

        # 下一场不继承：直接开场，核验为关闭
        s = setup_show(c)
        check(s["verify"]["enabled"] is False, "下一场不继承核验（默认关闭）")
        v = s["version"]
        cur = s["current"]
        r = report(c, "lighting", cur["id"], 1, True)
        check(r.status_code == 409, "未启用的下一场回报被 409 拒绝")
        r = confirm(c, v)
        check(r.status_code == 200, "未启用的下一场确认无需就绪（旧行为）")

        # 重置：关闭核验并清空
        r = c.post("/api/reset")
        v = r.json()["version"]
        r = enable_verify(c, v)
        check(r.status_code == 200, "重置后可再次于开场前启用")
        v = r.json()["version"]
        s = setup_show(c)
        report_both(c, s)
        check(c.get("/api/state").json()["verify"]["all_ready"] is True,
              "新一场核验正常工作")
        r = c.post("/api/reset")
        check(r.status_code == 200, "重置成功")
        check(r.json()["verify"]["enabled"] is False, "重置关闭本场核验")
        check(r.json()["verify"]["round"] == 0, "重置后轮次归零")
        with main.db() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM verify_reports").fetchone()["c"]
        check(n == 0, "重置清空就绪记录")


def test_start_from_archive_with_verify():
    fresh_db("v10.db")
    with TestClient(main.app) as c:
        print("V10 以档案开新场：开场前启用核验同样生效，从第 1 轮开始")
        s = setup_show(c, CUES2)
        v = s["version"]
        r = c.post("/api/archive", json={"version": v, "name": "底稿场"})
        v = r.json()["version"]
        aid = c.get("/api/archives").json()["archives"][0]["id"]

        r = enable_verify(c, v)
        check(r.status_code == 200, "开场前启用核验")
        v = r.json()["version"]
        r = c.post(f"/api/archives/{aid}/start", json={"version": v})
        check(r.status_code == 200, "以档案开新场成功")
        s = r.json()
        check(s["verify"]["enabled"] is True, "开新场后核验启用")
        check(s["verify"]["round"] == 1, "新场从第 1 轮开始")
        check(s["verify"]["cue_id"] == s["current"]["id"], "轮次对应当前提示")
        r = confirm(c, s["version"])
        check(r.status_code == 409, "未报就绪确认 409")
        report_both(c, c.get("/api/state").json())
        r = confirm(c, s["version"])
        check(r.status_code == 200, "报就绪后确认成功")


test_default_off_old_calls_unchanged()
test_enable_rules()
test_enable_control_seat_protection()
test_report_control_granted_echo()
test_report_rules()
test_confirm_gate_and_round_advance()
test_round_lifecycle()
test_withdraw_confirm_race()
test_confirm_write_failure_rollback()
test_persistence_and_close()
test_start_from_archive_with_verify()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
