"""观众字幕后端回归测试（FastAPI TestClient + 独立临时数据库）。

覆盖：
- 设置（POST /api/subtitles）：仅演出中（含暂停）的**待发**提示可设置；
  文本去除首尾空白后 1～200 字符，null 清除；空白 / 非字符串 / 超长 400
  （先于版本检查、不改数据）；非演出中 / 目标非待发 / 不存在 / 版本过期
  409 附最新状态且不写入；成功"设置 + 版本 +1"同事务，**不推进、不换
  核验轮次、不重新自动计时、不改变当前画面**；受控制席保护（403）；
- 绑定与失效：字幕绑定提示 id；手工编辑或接受提议改写该提示（即使同文）
  使字幕失效（设置保留）；重排、暂停、记问题、撤回不影响有效性；
- 发出（手工 / 自动共用）：有效字幕随发出替换观众画面、失效清屏，画面
  与发出同事务（写入失败画面 / 发出 / 版本都不变），不额外阻挡发出；
- 撤回清屏但保留设置（画面已空时不重复推进序号）；归档 / 重置 / 手工
  录入新场 / 以档案开新场清屏并清除设置、字幕不进档案；
- 观众只读接口 GET /api/screen 只返回字幕与跨场不归零的递增画面序号；
- 重启保留设置与画面（旧库迁移默认空设置、空画面、序号 0）。

运行：python3 tests/subtitles.test.py
"""
import os
import sqlite3
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-subtitle-test-")
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


def set_sub(c, version, cue_id, subtitle, token=None):
    headers = {"X-Control-Token": token} if token else {}
    return c.post(
        "/api/subtitles",
        json={"version": version, "cue_id": cue_id, "subtitle": subtitle},
        headers=headers,
    )


def pend(s, cue_id):
    return [p for p in s["pending"] if p["id"] == cue_id][0]


def screen(c):
    return c.get("/api/screen").json()


def test_setup_and_validation():
    main.DB_PATH = os.path.join(_tmp, "valid.db")
    with TestClient(main.app) as c:
        print("Z1 设置规则：去空白、1～200、null 清除；非法 400 不改数据")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        check(s["screen"] == {"subtitle": None, "seq": 0}, "初始画面为空、序号 0")
        check(s["pending"][0]["subtitle"] is None
              and s["pending"][0]["subtitle_valid"] is False, "初始无字幕设置")

        for bad in ("   ", "x" * 201, 123, True, ["字"], {"a": 1}):
            r = set_sub(c, v, id1, bad)
            check(r.status_code == 400, f"非法字幕 {bad!r} 返回 400")
        check(c.get("/api/state").json()["version"] == v, "400 不推进版本")

        r = set_sub(c, v, id1, "  欢迎光临今晚的演出  ")
        check(r.status_code == 200, "设置成功")
        s2 = r.json()
        check(s2["version"] == v + 1, "设置与版本 +1 一起保存")
        p = pend(s2, id1)
        check(p["subtitle"] == "欢迎光临今晚的演出" and p["subtitle_valid"] is True,
              "字幕去除首尾空白并标记有效")
        check(s2["screen"] == {"subtitle": None, "seq": 0}, "设置不改变当前画面")
        check(s2["current"]["content"] == "灯光渐亮", "设置不改变提示内容")

        # 覆盖设置（同文也重新生效）
        v2 = s2["version"]
        r = set_sub(c, v2, id1, "欢迎光临今晚的演出")
        check(r.status_code == 200 and r.json()["version"] == v2 + 1, "重复设置仍版本 +1")

        # 清除
        v3 = r.json()["version"]
        r = set_sub(c, v3, id1, None)
        check(r.status_code == 200, "null 清除成功")
        p = pend(r.json(), id1)
        check(p["subtitle"] is None and p["subtitle_valid"] is False, "清除后设置为空")


def test_conflicts():
    main.DB_PATH = os.path.join(_tmp, "conflict.db")
    with TestClient(main.app) as c:
        print("Z2 前置条件：非演出中 / 目标非待发 / 不存在 / 版本过期 409")
        # 开场前
        s = c.get("/api/state").json()
        if s["status"] != "setup":
            assert c.post("/api/reset", json={}).status_code == 200
            s = c.get("/api/state").json()
        r = set_sub(c, s["version"], 1, "开场前字幕")
        check(r.status_code == 409, "开场前设置 409")
        check(r.json()["state"]["screen"]["seq"] == 0, "409 附最新状态")

        s = setup_show(c)
        v = s["version"]
        id1, id2 = s["pending"][0]["id"], s["pending"][1]["id"]

        # 不存在
        check(set_sub(c, v, 99999, "x").status_code == 409, "目标不存在 409（与编辑一致）")

        # 版本过期
        assert set_sub(c, v, id2, "B 字幕").status_code == 200
        check(set_sub(c, v, id1, "过期").status_code == 409, "版本过期 409")
        check(c.get("/api/state").json()["version"] == v + 1, "409 不推进版本")

        # 已发出目标
        v2 = v + 1
        s3 = c.post("/api/confirm", json={"version": v2}).json()
        check(s3["screen"] == {"subtitle": None, "seq": 1}, "无有效字幕发出：清屏，序号 1")
        check(set_sub(c, s3["version"], id1, "已发").status_code == 409, "已发出目标 409")

        # 全部发出进入完成态后 409
        st = s3
        while st["remaining"]:
            last_id = st["current"]["id"]
            st = c.post("/api/confirm", json={"version": st["version"]}).json()
        check(st["status"] == "done", "全部发出进入完成态")
        check(set_sub(c, st["version"], last_id, "完成态").status_code == 409,
              "完成态设置 409")


def test_paused_and_no_side_effects():
    main.DB_PATH = os.path.join(_tmp, "side.db")
    with TestClient(main.app) as c:
        print("Z3 暂停中可设置；不换核验轮次；不扰动自动计时")
        # 开场前启用核验
        v = c.get("/api/state").json()["version"]
        r = c.post("/api/verify/enable", json={"version": v})
        assert r.status_code == 200
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        check(s["verify"]["round"] == 1 and s["verify"]["all_ready"] is False,
              "开场后第 1 轮、无就绪")

        # 灯光先报就绪；设置字幕不应清空就绪 / 换轮
        r = c.post("/api/verify/report",
                   json={"department": "lighting", "cue_id": id1, "round": 1, "ready": True})
        assert r.status_code == 200
        r = set_sub(c, v, id1, "第一条字幕")
        check(r.status_code == 200, "演出中设置成功")
        vw = r.json()["verify"]
        check(vw["round"] == 1 and vw["lighting"] is True and vw["sound"] is None,
              "设置字幕不换核验轮次、不清空已有就绪")
        check(r.json()["screen"]["subtitle"] is None, "设置不上屏")
        v = r.json()["version"]

        # 暂停中也可设置，且不改变暂停标记
        r = c.post("/api/pause", json={"version": v, "paused": True})
        assert r.status_code == 200
        v = r.json()["version"]
        r = set_sub(c, v, id1, "暂停中改的字幕")
        check(r.status_code == 200, "暂停中设置成功")
        check(r.json()["paused"] is True and r.json()["verify"]["round"] == 1,
              "暂停标记与核验轮次都不被设置改变")

    # 自动计时：设置字幕不重置（用可控时钟）
    main.DB_PATH = os.path.join(_tmp, "side-auto.db")
    t = [1000.0]
    main._clock = lambda: t[0]
    with TestClient(main.app) as c:
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = c.post("/api/auto", json={"version": v, "enabled": True, "interval": 10})
        assert r.status_code == 200
        v = r.json()["version"]
        # 等掉 4 秒后设置字幕：剩余应约 6 秒，而不是重新计满 10 秒
        t[0] += 4
        r = set_sub(c, v, id1, "不重置计时的字幕")
        check(r.status_code == 200, "自动模式下设置成功")
        remaining = r.json()["auto"]["remaining_seconds"]
        check(remaining is not None and remaining <= 6.1,
              f"设置字幕不重新自动计时（剩余约 6 秒，实际 {remaining}）")
        # 到满 10 秒自动发出，字幕按有效性上屏
        t[0] += 7
        check(main.auto_tick() is True, "按原计时刻度自动发出（未被设置重置）")
        st = c.get("/api/state").json()
        check(st["screen"] == {"subtitle": "不重置计时的字幕", "seq": 1},
              "自动发出上屏该字幕")
    main._clock = main.__dict__.get("_clock_orig", main._clock)


def test_invalidation():
    main.DB_PATH = os.path.join(_tmp, "invalid.db")
    with TestClient(main.app) as c:
        print("Z4 失效规则：手工编辑 / 接受提议（即使同文）失效；重排等不影响")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]

        assert set_sub(c, v, id2, "A2 字幕").status_code == 200
        v += 1
        # 重排（换位置但不改内容）：不失效
        r = c.post("/api/reorder", json={"version": v, "order": [id1, id3, id2]})
        assert r.status_code == 200
        v = r.json()["version"]
        check(pend(r.json(), id2)["subtitle_valid"] is True, "重排不使字幕失效")

        # 记问题：不失效
        r = c.post("/api/issues", json={"version": v, "cue_id": id2, "text": "问题 X"})
        assert r.status_code == 200
        v = r.json()["version"]
        check(pend(r.json(), id2)["subtitle_valid"] is True, "记现场问题不使字幕失效")

        # 手工编辑（同文）：失效
        r = c.post("/api/edit", json={"version": v, "cue_id": id2, "content": "音乐起"})
        assert r.status_code == 200
        v = r.json()["version"]
        p = pend(r.json(), id2)
        check(p["subtitle"] == "A2 字幕" and p["subtitle_valid"] is False,
              "同文手工编辑：字幕保留但失效")

        # 重新设置恢复有效
        assert set_sub(c, v, id2, "A2 新字幕").status_code == 200
        v += 1

        # 接受提议改写（即使建议与原文相同）：失效
        sid = c.post("/api/suggestions",
                     json={"version": v, "cue_id": id2, "content": "音乐起"}).json()
        sid = sid["suggestions"][-1]["id"]
        r = c.post(f"/api/suggestions/{sid}/accept", json={"version": v})
        assert r.status_code == 200
        v = r.json()["version"]
        p = pend(r.json(), id2)
        check(p["subtitle"] == "A2 新字幕" and p["subtitle_valid"] is False,
              "接受提议（建议同文）：字幕失效、设置保留")

        # 其他提示的编辑不影响 id1（无设置），给 id1 设置后编辑 id3 验证
        assert set_sub(c, v, id1, "A1 字幕").status_code == 200
        v += 1
        r = c.post("/api/edit", json={"version": v, "cue_id": id3, "content": "幕落改"})
        assert r.status_code == 200
        check(pend(r.json(), id1)["subtitle_valid"] is True, "编辑其他提示不影响其字幕")


def test_invalidation_rollback():
    main.DB_PATH = os.path.join(_tmp, "invalid-rollback.db")
    with TestClient(main.app) as c:
        print("Z4b 失效写入失败整体回滚：内容与字幕有效性都不变")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        assert set_sub(c, v, id1, "字幕").status_code == 200
        v += 1
        # 编辑的 UPDATE cues 同时写内容与 subtitle_valid：触发器使其失败，
        # 内容、字幕有效性、版本必须整体回滚（不留"内容没变但字幕失效"）。
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_cue_edit BEFORE UPDATE ON cues "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        with TestClient(main.app, raise_server_exceptions=False) as fc:
            r = fc.post("/api/edit",
                        json={"version": v, "cue_id": id1, "content": "新内容"})
        check(r.status_code == 500, "编辑写入失败返回错误")
        with main.db() as conn:
            conn.execute("DROP TRIGGER fail_cue_edit")
        st = c.get("/api/state").json()
        p = pend(st, id1)
        check(p["content"] == "灯光渐亮" and p["subtitle"] == "字幕"
              and p["subtitle_valid"] is True, "回滚：内容与字幕有效性都不变")
        check(st["version"] == v, "回滚：版本不变")
        # 字幕仍有效，发出应上屏
        s2 = c.post("/api/confirm", json={"version": v}).json()
        check(s2["screen"] == {"subtitle": "字幕", "seq": 1}, "回滚后字幕仍有效并发声上屏")


def test_issue_screen_manual_and_auto():
    main.DB_PATH = os.path.join(_tmp, "issue.db")
    t = [1000.0]
    main._clock = lambda: t[0]
    with TestClient(main.app) as c:
        print("Z5 发出上屏 / 清屏：手工与自动一致；画面随发出同事务")
        s = setup_show(c)
        v = s["version"]
        id1, id2, _ = [p["id"] for p in s["pending"]]
        assert set_sub(c, v, id1, "第一条字幕").status_code == 200
        v += 1

        # 手工发出有效字幕
        s2 = c.post("/api/confirm", json={"version": v}).json()
        check(s2["screen"] == {"subtitle": "第一条字幕", "seq": 1},
              "手工发出有效字幕：替换观众画面，序号 +1")
        check(screen(c) == {"subtitle": "第一条字幕", "seq": 1},
              "GET /api/screen 只返回字幕与序号")
        v = s2["version"]

        # id2 无设置：发出清屏（序号 +1）
        s3 = c.post("/api/confirm", json={"version": v}).json()
        check(s3["screen"] == {"subtitle": None, "seq": 2}, "无字幕发出：清屏，序号 +1")
        v = s3["version"]

        # id3 设置后由自动发出：先暂停时间，直接 auto_tick
        id3 = s3["current"]["id"]
        assert set_sub(c, v, id3, "自动字幕").status_code == 200
        v += 1
        r = c.post("/api/auto", json={"version": v, "enabled": True, "interval": 10})
        assert r.status_code == 200
        t[0] += 11
        check(main.auto_tick() is True, "后台自动发出一条")
        st = c.get("/api/state").json()
        check(st["screen"] == {"subtitle": "自动字幕", "seq": 3},
              "自动发出有效字幕同样上屏（与手工同一路径）")

        # 失效字幕由自动发出：清屏
        # 重置再开一场，设置后编辑失效，再自动发出
        assert c.post("/api/archive", json={"version": st["version"], "name": "z"})
        st = c.get("/api/state").json()
        aid = c.get("/api/archives").json()["archives"][0]["id"]
        st = c.post(f"/api/archives/{aid}/start", json={"version": st["version"]}).json()
        first = st["pending"][0]["id"]
        v = st["version"]
        assert set_sub(c, v, first, "会失效的字幕").status_code == 200
        v += 1
        assert c.post("/api/edit", json={"version": v, "cue_id": first,
                                        "content": st["pending"][0]["content"]}).status_code == 200
        v += 1
        t[0] += 0  # auto 已被归档关闭，重新开
        r = c.post("/api/auto", json={"version": v, "enabled": True, "interval": 10})
        assert r.status_code == 200
        t[0] += 11
        main.auto_tick()
        st = c.get("/api/state").json()
        check(st["screen"]["subtitle"] is None, "自动发出失效字幕：清屏")
        check(st["screen"]["seq"] >= 1, "清屏也推进画面序号")


def test_withdraw_keeps_setting():
    main.DB_PATH = os.path.join(_tmp, "withdraw.db")
    with TestClient(main.app) as c:
        print("Z6 撤回清屏但保留设置；画面非空才推进序号")
        s = setup_show(c)
        v = s["version"]
        id1, id2, _ = [p["id"] for p in s["pending"]]
        assert set_sub(c, v, id1, "会撤回的字幕").status_code == 200
        v += 1
        s2 = c.post("/api/confirm", json={"version": v}).json()
        check(s2["screen"] == {"subtitle": "会撤回的字幕", "seq": 1}, "发出上屏")
        v = s2["version"]

        r = c.post("/api/withdraw", json={"version": v, "cue_id": id1})
        check(r.status_code == 200, "撤回成功")
        check(r.json()["screen"] == {"subtitle": None, "seq": 2},
              "撤回清屏：画面序号 +1")
        p = pend(r.json(), id1)
        check(p["subtitle"] == "会撤回的字幕" and p["subtitle_valid"] is True,
              "撤回保留字幕设置与有效性（可再次上屏）")
        v = r.json()["version"]

        # 再次发出：仍有效，重新上屏
        s3 = c.post("/api/confirm", json={"version": v}).json()
        check(s3["screen"] == {"subtitle": "会撤回的字幕", "seq": 3},
              "保留的有效字幕再次发出仍上屏")
        v = s3["version"]

        # 失效字幕发出（清屏）后撤回：画面已空，序号不再重复推进
        id2_after = s3["current"]["id"]
        assert set_sub(c, v, id2_after, "X").status_code == 200
        v += 1
        assert c.post("/api/edit",
                      json={"version": v, "cue_id": id2_after, "content": "音乐起"}).status_code == 200
        v += 1
        s4 = c.post("/api/confirm", json={"version": v}).json()
        seq = s4["screen"]["seq"]
        check(s4["screen"]["subtitle"] is None, "失效字幕发出清屏")
        r = c.post("/api/withdraw",
                   json={"version": s4["version"], "cue_id": id2_after})
        check(r.json()["screen"] == {"subtitle": None, "seq": seq},
              "画面本就空白时撤回不重复推进序号")
        p = pend(r.json(), id2_after)
        check(p["subtitle"] == "X" and p["subtitle_valid"] is False,
              "撤回保留失效字幕的设置")


def test_clearing_and_archive():
    main.DB_PATH = os.path.join(_tmp, "clear.db")
    with TestClient(main.app) as c:
        print("Z7 归档 / 重置 / 开新场清屏清设置、不进档案；序号跨场不归零")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        assert set_sub(c, v, id1, "归档前字幕").status_code == 200
        v += 1
        s2 = c.post("/api/confirm", json={"version": v}).json()
        check(s2["screen"]["seq"] == 1 and screen(c)["subtitle"] == "归档前字幕",
              "发出后画面有字幕")
        v = s2["version"]

        # 归档（画面非空）：清屏 +1，序号不归零
        r = c.post("/api/archive", json={"version": v, "name": "字幕场"})
        assert r.status_code == 200
        check(r.json()["screen"] == {"subtitle": None, "seq": 2},
              "归档清屏：序号继续递增（跨场不归零）")
        v = r.json()["version"]
        aid = c.get("/api/archives").json()["archives"][0]["id"]
        detail = c.get(f"/api/archives/{aid}").json()
        check(all("subtitle" not in x for x in detail["issued"])
              and all("subtitle" not in x for x in detail["pending"]),
              "字幕不写入档案结构")

        # 以档案开新场：无字幕设置；画面空白，序号不变
        r = c.post(f"/api/archives/{aid}/start", json={"version": v})
        assert r.status_code == 200
        check(all(p["subtitle"] is None for p in r.json()["pending"]),
              "以档案开新场不复制字幕设置")
        check(r.json()["screen"] == {"subtitle": None, "seq": 2},
              "开新场画面为空时不推进序号；序号不归零")
        v = r.json()["version"]
        new_id = r.json()["pending"][0]["id"]

        # 重置
        assert set_sub(c, v, new_id, "重置前").status_code == 200
        v += 1
        r = c.post("/api/reset", json={})
        assert r.status_code == 200
        check(c.get("/api/screen").json() == {"subtitle": None, "seq": 2},
              "重置清屏（画面本空，序号不变），设置随 DELETE cues 清除")

        # 手工录入新场同样清除
        s = setup_show(c, [{"number": "B1", "content": "b1"}, {"number": "B2", "content": "b2"}])
        check(all(p["subtitle"] is None for p in s["pending"]), "手工录入新场无字幕设置")


def test_control_seat():
    main.DB_PATH = os.path.join(_tmp, "control.db")
    with TestClient(main.app) as c:
        print("Z8 控制席保护：缺失 / 失效凭据 403 且不变")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = c.post("/api/control/enable", json={"version": v})
        assert r.status_code == 200
        tok = r.json()["control_token"]
        v = r.json()["version"]

        check(set_sub(c, v, id1, "无凭据").status_code == 403, "无凭据 403")
        check(set_sub(c, v, id1, "错凭据", token="garbage").status_code == 403,
              "错误凭据 403")
        check(c.get("/api/screen").json()["seq"] == 0, "403 不改变画面")
        snap = c.get("/api/state").json()
        check(snap["version"] == v and pend(snap, id1)["subtitle"] is None,
              "403 不推进版本、不写入设置")

        r = set_sub(c, v, id1, "控制席字幕", token=tok)
        check(r.status_code == 200, "有效凭据设置成功")
        v = r.json()["version"]

        # 接管后旧凭据 403
        r = c.post("/api/control/takeover", json={"version": v})
        assert r.status_code == 200
        tok2 = r.json()["control_token"]
        check(set_sub(c, r.json()["version"], id1, "旧席", token=tok).status_code == 403,
              "被接管的旧凭据 403")
        check(set_sub(c, r.json()["version"], id1, "新席", token=tok2).status_code == 200,
              "新控制席可设置")


def test_write_failure_rollback():
    main.DB_PATH = os.path.join(_tmp, "rollback.db")
    with TestClient(main.app) as c:
        print("Z9 写入失败整体回滚：发出失败时画面也不变")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        assert set_sub(c, v, id1, "应上屏的字幕").status_code == 200
        v += 1

        # 让发出时的 meta 更新失败（触发器）：提示快照、画面、版本全部回滚
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_meta BEFORE UPDATE ON meta "
                "BEGIN SELECT RAISE(ABORT, 'injected meta failure'); END"
            )
        with TestClient(main.app, raise_server_exceptions=False) as fc:
            r = fc.post("/api/confirm", json={"version": v})
        check(r.status_code == 500, "发出写 meta 失败返回错误")
        with main.db() as conn:
            conn.execute("DROP TRIGGER fail_meta")
        snap = c.get("/api/state").json()
        check(snap["version"] == v, "回滚：版本不变")
        check(snap["current"]["id"] == id1, "回滚：提示未发出")
        check(snap["screen"] == {"subtitle": None, "seq": 0}, "回滚：画面不变")
        check(screen(c) == {"subtitle": None, "seq": 0}, "观众屏也看不到字幕")

        # 回滚后可正常发出上屏
        s2 = c.post("/api/confirm", json={"version": v}).json()
        check(s2["screen"] == {"subtitle": "应上屏的字幕", "seq": 1},
              "回滚后正常发出：字幕上屏")


def test_persistence_and_old_db():
    db_file = os.path.join(_tmp, "persist.db")
    main.DB_PATH = db_file
    with TestClient(main.app) as c:
        print("Z10 持久化与旧库迁移")
        s = setup_show(c)
        v = s["version"]
        id1, id2 = s["pending"][0]["id"], s["pending"][1]["id"]
        assert set_sub(c, v, id1, "保留的字幕").status_code == 200
        v += 1
        assert set_sub(c, v, id2, "会失效的字幕").status_code == 200
        v += 1
        assert c.post("/api/edit",
                      json={"version": v, "cue_id": id2, "content": "音乐起"}).status_code == 200
        s2 = c.post("/api/confirm", json={"version": v + 1}).json()
        assert s2["screen"]["subtitle"] == "保留的字幕"

    # 重启：设置 / 有效性 / 画面 / 序号都保留（直接查库核对已发提示的设置）
    with TestClient(main.app) as c:
        st = c.get("/api/state").json()
        check(st["screen"] == {"subtitle": "保留的字幕", "seq": 1},
              "重启后观众画面与序号保留")
        check(screen(c) == {"subtitle": "保留的字幕", "seq": 1},
              "重启后观众只读接口同样保留")
        check(pend(st, id2)["subtitle"] == "会失效的字幕"
              and pend(st, id2)["subtitle_valid"] is False, "重启后待发提示失效标记保留")
        # 已发提示的字幕设置（执行台状态的 issued 视图不含字幕，直接查库）
        conn = sqlite3.connect(db_file)
        conn.row_factory = sqlite3.Row
        d1 = conn.execute("SELECT subtitle, subtitle_valid FROM cues WHERE id = ?",
                          (id1,)).fetchone()
        d2 = conn.execute("SELECT subtitle, subtitle_valid FROM cues WHERE id = ?",
                          (id2,)).fetchone()
        check(d1["subtitle"] == "保留的字幕" and d1["subtitle_valid"] == 1,
              "重启后已发提示的字幕设置保留")
        check(d2["subtitle"] == "会失效的字幕" and d2["subtitle_valid"] == 0,
              "重启后待发提示失效标记（查库）保留")
        conn.close()

    # 旧库（无字幕列 / 无画面列）启动迁移：默认空设置、空画面、序号 0
    old_db = os.path.join(_tmp, "old.db")
    conn = sqlite3.connect(old_db)
    conn.execute(
        "CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK (id = 1), "
        "version INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'setup', "
        "paused INTEGER NOT NULL DEFAULT 0, control_enabled INTEGER NOT NULL DEFAULT 0, "
        "control_token TEXT, verify_enabled INTEGER NOT NULL DEFAULT 0, "
        "verify_round INTEGER NOT NULL DEFAULT 0, auto_enabled INTEGER NOT NULL DEFAULT 0, "
        "auto_interval INTEGER NOT NULL DEFAULT 10, auto_deadline REAL, auto_remaining REAL)"
    )
    conn.execute("INSERT INTO meta (id, version, status) VALUES (1, 3, 'live')")
    conn.execute(
        "CREATE TABLE cues (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL, "
        "content TEXT NOT NULL, position INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
        "issued_content TEXT, issued_at TEXT, issued_seq INTEGER)"
    )
    conn.execute("INSERT INTO cues (number, content, position, status) "
                 "VALUES ('O1', '旧提示', 1, 'pending')")
    conn.commit()
    conn.close()
    main.DB_PATH = old_db
    with TestClient(main.app) as c:
        st = c.get("/api/state").json()
        check(st["screen"] == {"subtitle": None, "seq": 0}, "旧库迁移：空画面、序号 0")
        check(st["pending"][0]["subtitle"] is None
              and st["pending"][0]["subtitle_valid"] is False,
              "旧库迁移：旧提示默认无字幕设置")
        check(c.get("/api/screen").json() == {"subtitle": None, "seq": 0},
              "旧库迁移后观众接口可正常轮询")


def test_screen_endpoint_minimal():
    main.DB_PATH = os.path.join(_tmp, "minimal.db")
    with TestClient(main.app) as c:
        print("Z11 观众接口最小化：只有 subtitle 与 seq，无后台信息")
        setup_show(c)
        data = screen(c)
        check(set(data.keys()) == {"subtitle", "seq"},
              f"GET /api/screen 仅返回两个字段（实际：{sorted(data.keys())}）")


test_setup_and_validation()
test_conflicts()
test_paused_and_no_side_effects()
test_invalidation()
test_invalidation_rollback()
test_issue_screen_manual_and_auto()
test_withdraw_keeps_setting()
test_clearing_and_archive()
test_control_seat()
test_write_failure_rollback()
test_persistence_and_old_db()
test_screen_endpoint_minimal()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
