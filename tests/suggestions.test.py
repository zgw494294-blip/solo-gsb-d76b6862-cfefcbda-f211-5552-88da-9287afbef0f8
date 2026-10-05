"""改写提议后端回归测试（FastAPI TestClient + 独立临时数据库）。

覆盖：
- 提交（POST /api/suggestions）：仅演出中（含暂停）的待发提示可提交；
  保存原文快照与建议、不改提示、不改版本号；无需控制凭据（永不 403），
  成功响应的 control_granted 按本次凭据回报；建议空白 400、提示不存在
  404、开场前 / 已发出 / 版本过期 409 且不写入；
- 接受（POST /api/suggestions/{id}/accept）：目标须仍待发且内容等于原文
  快照；其他提示的编辑、暂停、重排（含换位置）不使提议失效；目标自身被
  改写或被手工 / 自动发出则 409 且提议不变；"写入建议 + 标记已接受 +
  版本 +1"同事务；接受当前提示重开核验轮次、重新自动计时，其他提示不重置；
  重复接受 / 拒绝后再接受 / 版本过期均 409；受控制席保护；
- 拒绝（POST /api/suggestions/{id}/reject）：仅改状态、不改版本；目标
  已失效仍可拒绝；重复拒绝 409；受控制席保护；
- 与确认、编辑、重排、撤回、自动发出同版本竞争最多一个成功；写入失败
  整体回滚（不留半次结果）；刷新 / 重启保留；归档、重置、手工开新场、
  以档案开新场均清空且不写入档案。

运行：python3 tests/suggestions.test.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-suggestion-test-")
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


def setup_show(c, cues=None):
    r = c.post("/api/setup", json={"cues": cues or CUES3})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(c, v):
    r = c.post("/api/confirm", json={"version": v})
    assert r.status_code == 200, r.text
    return r.json()


def suggest(c, v, cue_id, content):
    return c.post("/api/suggestions",
                  json={"version": v, "cue_id": cue_id, "content": content})


def accept(c, v, sid, token=None):
    headers = {"X-Control-Token": token} if token else {}
    return c.post(f"/api/suggestions/{sid}/accept",
                  json={"version": v}, headers=headers)


def reject(c, v, sid, token=None):
    headers = {"X-Control-Token": token} if token else {}
    return c.post(f"/api/suggestions/{sid}/reject",
                  json={"version": v}, headers=headers)


def pending_sugs(s):
    return [x for x in s["suggestions"] if x["status"] == "pending"]


def test_submit_basic():
    with TestClient(main.app) as c:
        print("S1 提交：保存原文快照与建议，不改提示、不改版本；只读页可提交")
        s = setup_show(c)
        v = s["version"]
        id2 = s["pending"][1]["id"]

        r = suggest(c, v, id2, "  音乐提前两拍起  ")
        check(r.status_code == 200, "提交成功")
        data = r.json()
        check(data["version"] == v, "提交不改版本号")
        check(data["control_granted"] is True, "未启用控制席时 control_granted 恒 true")
        cue2 = [p for p in data["pending"] if p["id"] == id2][0]
        check(cue2["content"] == "音乐起", "提交不改提示内容")
        sug = data["suggestions"][0]
        check(sug["cue_id"] == id2 and sug["cue_number"] == "A2", "保存目标 id 与编号快照")
        check(sug["original_content"] == "音乐起", "保存原文快照")
        check(sug["content"] == "音乐提前两拍起", "建议内容去除首尾空白")
        check(sug["status"] == "pending" and sug["target_valid"] is True,
              "初始为待审核且目标有效")
        check(bool(sug["created_at"]) and sug["decided_at"] is None, "有提交时间、无处理时间")

        # 刷新后仍在
        fresh = c.get("/api/state").json()
        check(len(fresh["suggestions"]) == 1 and fresh["version"] == v,
              "刷新后提议保留、版本仍不变")


def test_submit_paused_allowed():
    with TestClient(main.app) as c:
        print("S2 暂停期间可提交；目标提示仍为待发即可")
        s = setup_show(c)
        v = s["version"]
        r = c.post("/api/pause", json={"version": v, "paused": True})
        check(r.status_code == 200, "暂停成功")
        v = r.json()["version"]
        r = suggest(c, v, s["pending"][0]["id"], "暂停时的建议")
        check(r.status_code == 200, "暂停期间提交成功")
        check(r.json()["version"] == v, "暂停期间提交仍不改版本")


def test_submit_errors():
    with TestClient(main.app) as c:
        print("S3 提交失败：空白 400、不存在 404、开场前/已发出/过期 409 且不写入")
        s = c.get("/api/state").json()
        if s["status"] != "setup":
            r = c.post("/api/reset", json={})
            assert r.status_code == 200
            s = r.json()
        r = suggest(c, s["version"], 1, "开场前建议")
        check(r.status_code == 409, "开场前提交 409")
        check(r.json()["state"]["suggestions"] == [], "409 附最新状态且无提议")

        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]

        r = suggest(c, v, id1, "   ")
        check(r.status_code == 400, "建议全空白返回 400")
        check(c.get("/api/state").json()["version"] == v, "400 不改版本")

        r = suggest(c, v, 99999, "不存在的提示")
        check(r.status_code == 404, "提示不存在返回 404")

        # 版本过期（先做一次编辑推进版本）
        r = c.post("/api/edit", json={"version": v, "cue_id": id1, "content": "灯光改"})
        assert r.status_code == 200
        v2 = r.json()["version"]
        r = suggest(c, v, s["pending"][1]["id"], "过期页面的建议")
        check(r.status_code == 409, "版本过期 409")
        check(r.json()["state"]["version"] == v2, "409 附最新状态")
        check(c.get("/api/state").json()["suggestions"] == [], "过期提交不写入")

        # 目标已发出
        s = confirm(c, v2)
        v = s["version"]
        r = suggest(c, v, id1, "已发出提示的建议")
        check(r.status_code == 409, "对已发出提示提交 409")
        check(c.get("/api/state").json()["suggestions"] == [], "已发出提交不写入")


def test_accept_basic():
    with TestClient(main.app) as c:
        print("S4 接受：写入建议、标记已接受、版本 +1 一起完成")
        s = setup_show(c)
        v = s["version"]
        id2 = s["pending"][1]["id"]
        r = suggest(c, v, id2, "音乐提前两拍起")
        sid = r.json()["suggestions"][0]["id"]

        r = accept(c, v, sid)
        check(r.status_code == 200, "接受成功")
        s2 = r.json()
        check(s2["version"] == v + 1, "接受使版本 +1")
        cue2 = [p for p in s2["pending"] if p["id"] == id2][0]
        check(cue2["content"] == "音乐提前两拍起", "提示内容已改为建议")
        sug = [x for x in s2["suggestions"] if x["id"] == sid][0]
        check(sug["status"] == "accepted" and bool(sug["decided_at"]),
              "提议标记为已接受并记录处理时间")
        check(sug["target_valid"] is False, "已处理提议 target_valid 恒为 false")


def test_unrelated_ops_keep_valid():
    with TestClient(main.app) as c:
        print("S5 其他提示编辑、暂停、重排（换位置）不使提议失效")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        r = suggest(c, v, id2, "音乐建议")
        sid = r.json()["suggestions"][0]["id"]

        # 编辑其他提示
        r = c.post("/api/edit", json={"version": v, "cue_id": id3, "content": "幕落改"})
        assert r.status_code == 200
        v = r.json()["version"]
        check(pending_sugs(r.json())[0]["target_valid"] is True, "编辑其他提示后仍有效")

        # 暂停
        r = c.post("/api/pause", json={"version": v, "paused": True})
        assert r.status_code == 200
        v = r.json()["version"]
        check(pending_sugs(r.json())[0]["target_valid"] is True, "暂停后仍有效")
        r = c.post("/api/pause", json={"version": v, "paused": False})
        assert r.status_code == 200
        v = r.json()["version"]

        # 重排：把 id2 移到队首（目标位置变化，内容不变）
        r = c.post("/api/reorder", json={"version": v, "order": [id2, id1, id3]})
        assert r.status_code == 200
        v = r.json()["version"]
        sug = pending_sugs(r.json())[0]
        check(sug["target_valid"] is True, "重排换位置后仍有效")
        check(r.json()["current"]["id"] == id2, "目标现在是当前提示")

        # 接受仍成功，且作用在正确提示上
        r = accept(c, v, sid)
        check(r.status_code == 200, "重排后接受成功")
        check(r.json()["current"]["content"] == "音乐建议", "当前提示内容被改写为建议")


def test_target_changed_or_issued():
    with TestClient(main.app) as c:
        print("S6 目标被改写或发出：接受 409，提议保持待审核、内容不被覆盖")
        s = setup_show(c)
        v = s["version"]
        id1, id2, _ = [p["id"] for p in s["pending"]]

        # 场景一：目标被手工改写
        r = suggest(c, v, id2, "音乐建议")
        sid = r.json()["suggestions"][0]["id"]
        r = c.post("/api/edit", json={"version": v, "cue_id": id2, "content": "音乐被导演改了"})
        assert r.status_code == 200
        v2 = r.json()["version"]
        check(pending_sugs(r.json())[0]["target_valid"] is False, "目标内容变化后标记失效")
        r = accept(c, v2, sid)
        check(r.status_code == 409, "目标内容已变：接受 409")
        s2 = r.json()["state"]
        cue2 = [p for p in s2["pending"] if p["id"] == id2][0]
        check(cue2["content"] == "音乐被导演改了", "409 不覆盖已变化的内容")
        check([x for x in s2["suggestions"] if x["id"] == sid][0]["status"] == "pending",
              "409 后提议仍为待审核（可改走拒绝）")
        check(s2["version"] == v2, "失效接受不推进版本")

        # 仍可拒绝
        r = reject(c, v2, sid)
        check(r.status_code == 200 and r.json()["version"] == v2,
              "失效目标仍可拒绝，拒绝不改版本")

        # 场景二：目标被确认发出
        r = suggest(c, v2, id1, "灯光建议")
        sid2 = r.json()["suggestions"][-1]["id"]
        s = confirm(c, v2)
        v3 = s["version"]
        r = accept(c, v3, sid2)
        check(r.status_code == 409, "目标已发出：接受 409")
        check(r.json()["state"]["version"] == v3, "发出后接受不推进版本")
        check([x for x in r.json()["state"]["suggestions"]
               if x["id"] == sid2][0]["status"] == "pending", "发出后提议仍待审")


def test_reject():
    with TestClient(main.app) as c:
        print("S7 拒绝：仅改状态、不改版本、不动提示；失效目标也可拒绝")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = suggest(c, v, id1, "灯光建议 A")
        sid = r.json()["suggestions"][0]["id"]

        r = reject(c, v, sid)
        check(r.status_code == 200, "拒绝成功")
        check(r.json()["version"] == v, "拒绝不改版本号")
        check(r.json()["current"]["content"] == "灯光渐亮", "拒绝不改提示内容")
        check([x for x in r.json()["suggestions"]
               if x["id"] == sid][0]["status"] == "rejected", "提议标记已拒绝")

        # 重复拒绝
        r = reject(c, v, sid)
        check(r.status_code == 409, "已拒绝再拒绝：409")

        # 不存在的提议
        r = reject(c, v, 99999)
        check(r.status_code == 409, "不存在的提议：409")
        r = accept(c, v, 99999)
        check(r.status_code == 409, "接受不存在的提议：409")

        # 失效目标仍可拒绝
        r = suggest(c, v, id1, "灯光建议 B")
        sid2 = r.json()["suggestions"][-1]["id"]
        r = c.post("/api/edit", json={"version": v, "cue_id": id1, "content": "手工新内容"})
        assert r.status_code == 200
        v2 = r.json()["version"]
        r = reject(c, v2, sid2)
        check(r.status_code == 200, "目标已失效仍可拒绝")
        check([x for x in r.json()["suggestions"]
               if x["id"] == sid2][0]["status"] == "rejected", "失效提议被拒绝")


def test_same_version_competition():
    with TestClient(main.app) as c:
        print("S8 同版本竞争：接受与确认/编辑/重排/撤回最多一个成功")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]

        # 接受 vs 编辑同一提示
        r0 = suggest(c, v, id2, "建议内容")
        sid = r0.json()["suggestions"][0]["id"]
        r1 = accept(c, v, sid)
        r2 = c.post("/api/edit", json={"version": v, "cue_id": id2, "content": "编辑内容"})
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本接受与编辑：一个 200 一个 409")
        cur = c.get("/api/state").json()
        check(cur["version"] == v + 1, "竞争后版本只 +1")
        cue2 = [p for p in cur["pending"] if p["id"] == id2][0]
        check(cue2["content"] in ("建议内容", "编辑内容"), "只有一方的内容落库")

        # 接受 vs 确认（接受的是队首）
        v = cur["version"]
        r0 = suggest(c, v, id1, "对当前提示的建议")
        sid = r0.json()["suggestions"][-1]["id"]
        r1 = accept(c, v, sid)
        r2 = c.post("/api/confirm", json={"version": v})
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本接受与确认：一个 200 一个 409")
        after = c.get("/api/state").json()
        check(after["version"] == v + 1, "竞争后版本只 +1")
        if r1.status_code == 200:
            check(after["issued"] == [] and after["current"]["id"] == id1,
                  "接受胜出：提示未发出，内容被改写")
            check(after["current"]["content"] == "对当前提示的建议", "接受胜出的内容落库")
        else:
            check(len(after["issued"]) == 1 and after["current"]["id"] == id2,
                  "确认胜出：已推进，接受因版本过期被拒")

        # 拒绝 vs 编辑：拒绝不推进版本，编辑可在同版本随后成功
        v = after["version"]
        r0 = suggest(c, v, id3, "给 A3 的建议")
        sid = r0.json()["suggestions"][-1]["id"]
        r1 = reject(c, v, sid)
        check(r1.status_code == 200, "拒绝成功且版本不变")
        r2 = c.post("/api/edit", json={"version": v, "cue_id": id3, "content": "A3 编辑"})
        check(r2.status_code == 200, "拒绝未推进版本，同版本编辑随后可成功")


def test_verify_round_on_accept():
    with TestClient(main.app) as c:
        print("S9 接受当前提示重开核验轮次并清空就绪；接受其他提示不重置")
        # 先回到开场前（上一个用例停在演出中）
        s0 = c.get("/api/state").json()
        if s0["status"] != "setup":
            assert c.post("/api/reset", json={}).status_code == 200
        # 开场前启用核验
        s = c.get("/api/state").json()
        r = c.post("/api/verify/enable", json={"version": s["version"]})
        assert r.status_code == 200
        s = setup_show(c)
        v = s["version"]
        id1, id2, _ = [p["id"] for p in s["pending"]]
        check(s["verify"]["round"] == 1 and s["verify"]["cue_id"] == id1,
              "开场后第 1 轮，当前为 A1")

        # 两工种对第 1 轮报就绪
        for dept in ("lighting", "sound"):
            r = c.post("/api/verify/report",
                       json={"department": dept, "cue_id": id1, "round": 1, "ready": True})
            assert r.status_code == 200
        check(c.get("/api/state").json()["verify"]["all_ready"] is True, "两工种就绪")

        # 对"其他提示"（id2）的提议被接受：轮次不变、就绪保留
        r = suggest(c, v, id2, "A2 建议")
        sid = r.json()["suggestions"][0]["id"]
        r = accept(c, v, sid)
        check(r.status_code == 200, "接受非当前提示成功")
        v = r.json()["version"]
        vw = r.json()["verify"]
        check(vw["round"] == 1 and vw["all_ready"] is True,
              "接受其他提示不重开轮次、不清空就绪")

        # 对当前提示（id1）的提议被接受：轮次 +1、就绪清空
        r = suggest(c, v, id1, "A1 建议")
        sid = r.json()["suggestions"][-1]["id"]
        r = accept(c, v, sid)
        check(r.status_code == 200, "接受当前提示成功")
        vw = r.json()["verify"]
        check(vw["round"] == 2 and vw["cue_id"] == id1 and vw["all_ready"] is False,
              "接受当前提示重开核验轮次并清空本轮就绪")
        # 旧轮次回报被 409
        r = c.post("/api/verify/report",
                   json={"department": "lighting", "cue_id": id1, "round": 1, "ready": True})
        check(r.status_code == 409, "旧轮次回报被拒")


def test_control_seat_protection():
    main.DB_PATH = os.path.join(_tmp, "control.db")
    with TestClient(main.app) as c:
        print("S10 控制席：提交永不 403；接受/拒绝缺失或旧凭据 403 且不变")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]

        r = c.post("/api/control/enable", json={"version": v})
        check(r.status_code == 200, "启用控制席")
        tok = r.json()["control_token"]
        v = r.json()["version"]

        # 无凭据页仍可提交；响应 control_granted 为 false
        r = suggest(c, v, id1, "只读页的建议")
        check(r.status_code == 200, "无凭据提交不返回 403")
        check(r.json()["control_granted"] is False, "提交响应如实回报资格 false")
        sid = r.json()["suggestions"][0]["id"]

        # 带失效凭据同样能提交
        r = c.post("/api/suggestions",
                   json={"version": v, "cue_id": id1, "content": "旧凭据页的建议"},
                   headers={"X-Control-Token": "garbage"})
        check(r.status_code == 200 and r.json()["control_granted"] is False,
              "失效凭据也可提交，不 403、不获得资格")
        sid2 = r.json()["suggestions"][-1]["id"]

        # 接受 / 拒绝无凭据 403
        r = accept(c, v, sid)
        check(r.status_code == 403, "无凭据接受返回 403")
        r = reject(c, v, sid)
        check(r.status_code == 403, "无凭据拒绝返回 403")
        snap = c.get("/api/state").json()
        check(snap["version"] == v and all(x["status"] == "pending" for x in snap["suggestions"]),
              "403 后版本与提议状态都不变")

        # 错误凭据 403
        r = accept(c, v, sid, token="wrong")
        check(r.status_code == 403, "错误凭据接受 403")

        # 接管后旧凭据 403
        r = c.post("/api/control/takeover", json={"version": v})
        check(r.status_code == 200, "另一页接管")
        tok2 = r.json()["control_token"]
        v = r.json()["version"]
        r = accept(c, v, sid, token=tok)
        check(r.status_code == 403, "被接管的旧凭据接受 403")

        # 新凭据可审核
        r = accept(c, v, sid, token=tok2)
        check(r.status_code == 200, "新控制席接受成功")
        v = r.json()["version"]
        r = reject(c, v, sid2, token=tok2)
        check(r.status_code == 200, "新控制席拒绝成功")
        check(r.json()["version"] == v, "拒绝不改版本")


def test_write_failure_rollback():
    main.DB_PATH = os.path.join(_tmp, "rollback.db")
    with TestClient(main.app) as c:
        print("S11 写入失败整体回滚：接受不留半次结果")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = suggest(c, v, id1, "建议内容 X")
        sid = r.json()["suggestions"][0]["id"]

        # 让"写提示内容"失败：UPDATE cues 触发器抛错
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_cue_update BEFORE UPDATE ON cues "
                "BEGIN SELECT RAISE(ABORT, 'injected cue failure'); END"
            )
        with TestClient(main.app, raise_server_exceptions=False) as fc:
            r = accept(fc, v, sid)
        check(r.status_code == 500, "接受时写提示失败返回错误")
        with main.db() as conn:
            conn.execute("DROP TRIGGER fail_cue_update")
        snap = c.get("/api/state").json()
        cue1 = [p for p in snap["pending"] if p["id"] == id1][0]
        check(cue1["content"] == "灯光渐亮", "回滚：提示内容未改")
        check([x for x in snap["suggestions"] if x["id"] == sid][0]["status"] == "pending",
              "回滚：提议仍为待审核")
        check(snap["version"] == v, "回滚：版本不变")

        # 回滚后可正常接受
        r = accept(c, v, sid)
        check(r.status_code == 200, "回滚后接受成功")
        check(r.json()["current"]["content"] == "建议内容 X", "建议内容落库")


def test_persistence_and_clearing():
    db_file = os.path.join(_tmp, "persist.db")
    main.DB_PATH = db_file
    with TestClient(main.app) as c:
        print("S12 持久化与清空：重启保留；归档/重置/开新场清空，不进档案")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = suggest(c, v, id1, "会被归档清掉的建议")
        sid = r.json()["suggestions"][0]["id"]
        check(sid is not None, "提交成功")

    # 重启
    with TestClient(main.app) as c:
        s = c.get("/api/state").json()
        check(len(s["suggestions"]) == 1 and s["suggestions"][0]["status"] == "pending",
              "重启后提议保留（刷新、重启不丢）")

        # 确认发出一条后归档
        v = s["version"]
        s = confirm(c, v)
        v = s["version"]
        r = c.post("/api/archive", json={"version": v, "name": "提议归档场"})
        check(r.status_code == 200, "归档成功")
        v = r.json()["version"]
        check(c.get("/api/state").json()["suggestions"] == [], "归档后当前提议清空")
        aid = c.get("/api/archives").json()["archives"][0]["id"]
        detail = c.get(f"/api/archives/{aid}").json()
        check("suggestions" not in detail, "提议不写入档案结构")

        # 以档案开新场：仍为空
        r = c.post(f"/api/archives/{aid}/start", json={"version": v})
        check(r.status_code == 200, "以档案开新场")
        check(c.get("/api/state").json()["suggestions"] == [],
              "以档案开新场不复制提议")

        # 手工开新场（重置后 setup）：提交一条再重置
        s = c.get("/api/state").json()
        r = c.post("/api/reset", json={})
        assert r.status_code == 200
        s = setup_show(c)
        v = s["version"]
        r = suggest(c, v, s["pending"][0]["id"], "重置前建议")
        check(r.status_code == 200 and len(r.json()["suggestions"]) == 1, "再提交一条")
        r = c.post("/api/reset", json={})
        check(r.status_code == 200, "重置成功")
        check(c.get("/api/state").json()["suggestions"] == [], "重置后提议清空")

        # 手工录入新场也清空
        s = setup_show(c)
        v = s["version"]
        r = suggest(c, v, s["pending"][0]["id"], "临结束建议")
        assert r.status_code == 200
        s2 = setup_show(c, [
            {"number": "C1", "content": "全新场 1"},
            {"number": "C2", "content": "全新场 2"},
        ])
        check(s2["suggestions"] == [], "手工录入新场清空上场提议")


def test_auto_issue_interleave():
    db_file = os.path.join(_tmp, "auto.db")
    main.DB_PATH = db_file
    # 可控时钟驱动 auto_tick
    t = [1000.0]
    main._clock = lambda: t[0]
    with TestClient(main.app) as c:
        print("S13 与自动发出交错：自动发出后接受 409，不重复处理")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        # 开启自动（间隔 10 秒）
        r = c.post("/api/auto", json={"version": v, "enabled": True, "interval": 10})
        assert r.status_code == 200
        v = r.json()["version"]
        # 对当前提示提交提议
        r = suggest(c, v, id1, "和自动发出竞争的建议")
        sid = r.json()["suggestions"][0]["id"]
        # 时钟走到到期后，后台 tick 发出当前提示
        t[0] += 11
        check(main.auto_tick() is True, "自动发出一条")
        after = c.get("/api/state").json()
        check(after["version"] == v + 1 and len(after["issued"]) == 1,
              "自动发出推进版本、提示已发出")
        # 接受该提议：目标已发出 -> 409，提议不变
        r = accept(c, after["version"], sid)
        check(r.status_code == 409, "自动发出后接受返回 409")
        check([x for x in r.json()["state"]["suggestions"]
               if x["id"] == sid][0]["status"] == "pending", "提议保持待审核")
        check(r.json()["state"]["version"] == v + 1, "409 不再推进版本")


test_submit_basic()
test_submit_paused_allowed()
test_submit_errors()
test_accept_basic()
test_unrelated_ops_keep_valid()
test_target_changed_or_issued()
test_reject()
test_same_version_competition()
test_verify_round_on_accept()
test_control_seat_protection()
test_write_failure_rollback()
test_persistence_and_clearing()
test_auto_issue_interleave()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
