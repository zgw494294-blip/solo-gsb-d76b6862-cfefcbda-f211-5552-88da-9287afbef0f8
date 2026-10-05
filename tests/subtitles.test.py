"""观众字幕后端回归测试（FastAPI TestClient + 独立临时数据库 + 可控时钟）。

覆盖：
- 设置（POST /api/subtitles）：仅演出中（含暂停）的待发提示可设置；正文
  去除首尾空白后 1～200 字，text 为 null 清除；"设置 + 版本 +1"同事务，
  不推进提示、不换核验轮次、不重置自动计时；空白 / 超长 400、提示不存在
  404、非演出中 / 目标已发出 / 版本过期 409 且不写入；受控制席保护（403）；
- 失效：手工编辑或接受提议改写该提示（即使同文）使字幕失效，重排不影响；
  失效后重新保存即恢复有效；
- 画面：手工与自动发出时有效字幕上屏、无字幕或已失效清屏（画面序号 +1，
  同事务）；撤回清屏但保留设置，再次发出重新上屏；归档 / 重置 / 手工开新场 /
  以档案开新场清屏并清除设置，不进档案；画面序号跨场不归零；
- 观众接口（GET /api/screen）：只返回字幕与画面序号两个字段；
- 持久化：刷新 / 重启保留设置与画面；旧库升级默认空画面、无设置；
- 写入失败整体回滚（字幕设置、发出画面均不留半次结果）。

运行：python3 tests/subtitles.test.py
"""
import os
import sqlite3
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-subtitle-test-")
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

REAL_CLOCK = main._clock


def use_fake_clock(t=1_000.0):
    box = [float(t)]
    main._clock = lambda: box[0]
    return box


def fresh_db(name):
    main.DB_PATH = os.path.join(_tmp, name)


def setup_show(c, cues=None, headers=None):
    r = c.post("/api/setup", json={"cues": cues or CUES3}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def confirm(c, v, headers=None):
    r = c.post("/api/confirm", json={"version": v}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def set_sub(c, v, cue_id, text, headers=None):
    return c.post("/api/subtitles",
                  json={"version": v, "cue_id": cue_id, "text": text},
                  headers=headers)


def screen(c):
    return c.get("/api/screen").json()


def sub_of(state, cue_id):
    for s in state["subtitles"]:
        if s["cue_id"] == cue_id:
            return s
    return None


def test_set_and_clear():
    fresh_db("s01.db")
    with TestClient(main.app) as c:
        print("T1 设置字幕：版本 +1、去空白、不动画面与提示；清除（null）同样版本 +1")
        s = setup_show(c)
        v = s["version"]
        id1, id2 = s["pending"][0]["id"], s["pending"][1]["id"]
        check(s["subtitles"] == [], "开场后无字幕设置")
        scr0 = screen(c)
        check(scr0["text"] is None, "初始画面为空")

        r = set_sub(c, v, id1, "  第一幕 · 森林  ")
        check(r.status_code == 200, "设置成功")
        d = r.json()
        check(d["version"] == v + 1, "设置使版本 +1")
        check(sub_of(d, id1) == {"cue_id": id1, "text": "第一幕 · 森林", "valid": True},
              "字幕保存且去除首尾空白、有效")
        check(d["current"]["content"] == "灯光渐亮", "设置不改提示内容")
        check(d["issued"] == [], "设置不推进提示")
        check(screen(c) == scr0, "设置不改变观众画面")
        v = d["version"]

        # 覆盖设置
        r = set_sub(c, v, id1, "替换后的字幕")
        check(r.status_code == 200 and sub_of(r.json(), id1)["text"] == "替换后的字幕",
              "重复设置覆盖旧字幕")
        v = r.json()["version"]

        # 清除
        r = set_sub(c, v, id1, None)
        check(r.status_code == 200, "null 清除成功")
        d = r.json()
        check(d["version"] == v + 1, "清除同样版本 +1")
        check(d["subtitles"] == [], "清除后无字幕设置")
        v = d["version"]

        # 无设置时清除也成功
        r = set_sub(c, v, id2, None)
        check(r.status_code == 200 and r.json()["version"] == v + 1,
              "无设置时清除仍成功且版本 +1")

        # 刷新后仍在
        r = set_sub(c, v + 1, id2, "第二幕字幕")
        assert r.status_code == 200
        fresh = c.get("/api/state").json()
        check(sub_of(fresh, id2)["text"] == "第二幕字幕", "刷新后字幕设置保留")


def test_validation_errors():
    fresh_db("s02.db")
    with TestClient(main.app) as c:
        print("T2 校验：空白 / 超长 400，不存在 404，非演出中 / 已发出 / 过期 409，均不写入")
        # 开场前
        s0 = c.get("/api/state").json()
        r = set_sub(c, s0["version"], 1, "开场前字幕")
        check(r.status_code == 409, "开场前设置 409")
        check(r.json()["state"]["status"] == "setup", "409 附最新状态")

        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]

        r = set_sub(c, v, id1, "   ")
        check(r.status_code == 400, "全空白 400")
        r = set_sub(c, v, id1, "字" * 201)
        check(r.status_code == 400, "超过 200 字 400")
        check(c.get("/api/state").json()["version"] == v, "400 不改版本")
        r = set_sub(c, v, id1, "字")
        check(r.status_code == 200, "恰好 1 字可设置")
        v = r.json()["version"]
        r = set_sub(c, v, id1, "字" * 200)
        check(r.status_code == 200, "恰好 200 字可设置")
        v = r.json()["version"]

        r = set_sub(c, v, 99999, "不存在")
        check(r.status_code == 404, "提示不存在 404")

        # 版本过期
        r = set_sub(c, v - 1, id1, "过期页面")
        check(r.status_code == 409, "版本过期 409")
        check(r.json()["state"]["version"] == v, "409 附最新状态版本")

        # 目标已发出
        s = confirm(c, v)
        v = s["version"]
        r = set_sub(c, v, id1, "已发出的提示")
        check(r.status_code == 409, "对已发出提示设置 409")
        check(sub_of(c.get("/api/state").json(), id1)["valid"] is True,
              "409 不写入（原设置不变）")

        # 暂停中仍可设置（演出中含暂停）
        r = c.post("/api/pause", json={"version": v, "paused": True})
        assert r.status_code == 200
        v = r.json()["version"]
        id2 = c.get("/api/state").json()["current"]["id"]
        r = set_sub(c, v, id2, "暂停时设置的字幕")
        check(r.status_code == 200, "暂停中可设置字幕")
        v = r.json()["version"]
        r = c.post("/api/pause", json={"version": v, "paused": False})
        assert r.status_code == 200
        v = r.json()["version"]

        # 全部发出（done）后不可设置
        s = confirm(c, v)
        s = confirm(c, s["version"])
        check(s["status"] == "done", "全部发出完成")
        r = set_sub(c, s["version"], id2, "完成后设置")
        check(r.status_code == 409, "已完成状态设置 409")


def test_control_seat_protection():
    fresh_db("s03.db")
    with TestClient(main.app) as c:
        print("T3 控制席保护：缺失 / 失效凭据 403 且数据不变")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = c.post("/api/control/enable", json={"version": v})
        assert r.status_code == 200
        tok = r.json()["control_token"]
        v = r.json()["version"]

        r = set_sub(c, v, id1, "无凭据")
        check(r.status_code == 403, "无凭据设置 403")
        r = set_sub(c, v, id1, "错误凭据", headers={"X-Control-Token": "wrong"})
        check(r.status_code == 403, "错误凭据 403")
        snap = c.get("/api/state").json()
        check(snap["version"] == v and snap["subtitles"] == [], "403 后版本与设置都不变")

        r = set_sub(c, v, id1, "控制席字幕", headers={"X-Control-Token": tok})
        check(r.status_code == 200, "当前凭据可设置")
        v = r.json()["version"]

        r = c.post("/api/control/takeover", json={"version": v})
        assert r.status_code == 200
        v = r.json()["version"]
        r = set_sub(c, v, id1, "旧凭据设置", headers={"X-Control-Token": tok})
        check(r.status_code == 403, "被接管旧凭据 403")
        check(sub_of(c.get("/api/state").json(), id1)["text"] == "控制席字幕",
              "403 后原字幕不变")


def test_no_round_or_timer_reset():
    fresh_db("s04.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("T4 设置字幕不换核验轮次、不重置自动计时")
        s0 = c.get("/api/state").json()
        r = c.post("/api/verify/enable", json={"version": s0["version"]})
        assert r.status_code == 200
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        # 开启自动（间隔 10 秒）并报两工种就绪
        r = c.post("/api/auto", json={"version": v, "enabled": True, "interval": 10})
        assert r.status_code == 200
        v = r.json()["version"]
        for dept in ("lighting", "sound"):
            r = c.post("/api/verify/report", json={
                "department": dept, "cue_id": id1,
                "round": s["verify"]["round"], "ready": True})
            assert r.status_code == 200
        before = c.get("/api/state").json()
        check(before["verify"]["all_ready"] is True, "两工种已就绪")

        clock[0] += 4  # 计时走到剩余 6 秒
        r = set_sub(c, v, id1, "不重置计时的字幕")
        check(r.status_code == 200, "设置成功")
        d = r.json()
        check(d["verify"]["round"] == before["verify"]["round"], "核验轮次不变")
        check(d["verify"]["all_ready"] is True, "本轮就绪保留")
        rem = d["auto"]["remaining_seconds"]
        check(rem is not None and 5.5 <= rem <= 6.5,
              f"自动计时未重置（剩余约 6 秒，实际 {rem}）")
        v = d["version"]

        # 时钟到期后自动发出（字幕随自动发出上屏）
        clock[0] += 7
        check(main.auto_tick() is True, "到期自动发出一条")
        check(screen(c)["text"] == "不重置计时的字幕", "自动发出同样字幕上屏")


def test_invalidation_rules():
    fresh_db("s05.db")
    with TestClient(main.app) as c:
        print("T5 失效：编辑 / 接受提议（即使同文）使失效，重排不影响，重存恢复")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        r = set_sub(c, v, id2, "A2 的字幕")
        assert r.status_code == 200
        v = r.json()["version"]

        # 重排不影响
        r = c.post("/api/reorder", json={"version": v, "order": [id2, id1, id3]})
        assert r.status_code == 200
        v = r.json()["version"]
        check(sub_of(r.json(), id2)["valid"] is True, "重排（换队首）不使字幕失效")

        # 编辑其他提示不影响
        r = c.post("/api/edit", json={"version": v, "cue_id": id3, "content": "幕落改"})
        assert r.status_code == 200
        v = r.json()["version"]
        check(sub_of(r.json(), id2)["valid"] is True, "编辑其他提示不影响")

        # 编辑该提示（同文）使失效
        r = c.post("/api/edit", json={"version": v, "cue_id": id2, "content": "音乐起"})
        assert r.status_code == 200
        v = r.json()["version"]
        check(sub_of(r.json(), id2)["valid"] is False, "同文编辑也使字幕失效")

        # 失效后重新保存恢复
        r = set_sub(c, v, id2, "A2 的字幕")
        check(r.status_code == 200 and sub_of(r.json(), id2)["valid"] is True,
              "失效后重新保存（同文）恢复有效")
        v = r.json()["version"]

        # 接受提议（同文）使失效
        r = c.post("/api/suggestions",
                   json={"version": v, "cue_id": id2, "content": "音乐起"})
        assert r.status_code == 200
        sid = r.json()["suggestions"][0]["id"]
        r = c.post(f"/api/suggestions/{sid}/accept", json={"version": v})
        assert r.status_code == 200
        v = r.json()["version"]
        check(sub_of(r.json(), id2)["valid"] is False, "接受提议（同文）也使字幕失效")


def test_issue_screen_flow():
    fresh_db("s06.db")
    with TestClient(main.app) as c:
        print("T6 发出上屏：有效字幕上屏、无字幕 / 已失效清屏，序号递增")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        r = set_sub(c, v, id1, "第一幕字幕")
        assert r.status_code == 200
        v = r.json()["version"]
        r = set_sub(c, v, id3, "第三幕字幕")
        assert r.status_code == 200
        v = r.json()["version"]
        # id3 的字幕失效（编辑同文）
        r = c.post("/api/edit", json={"version": v, "cue_id": id3, "content": "幕落"})
        assert r.status_code == 200
        v = r.json()["version"]
        seq0 = screen(c)["seq"]

        s = confirm(c, v)  # A1：有效字幕
        scr = screen(c)
        check(scr == {"seq": seq0 + 1, "text": "第一幕字幕"}, "有效字幕随发出上屏、序号 +1")
        s = confirm(c, s["version"])  # A2：无字幕
        scr = screen(c)
        check(scr == {"seq": seq0 + 2, "text": None}, "无字幕提示发出时清屏")
        s = confirm(c, s["version"])  # A3：字幕已失效
        scr = screen(c)
        check(scr == {"seq": seq0 + 3, "text": None}, "失效字幕不上屏（清屏）")
        check(s["status"] == "done", "全部发出完成")
        # 观众接口只含两个字段
        r = c.get("/api/screen")
        check(sorted(r.json().keys()) == ["seq", "text"], "观众接口只返回字幕与序号")


def test_withdraw_keeps_settings():
    fresh_db("s07.db")
    with TestClient(main.app) as c:
        print("T7 撤回：清屏但保留设置，再次发出重新上屏")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = set_sub(c, v, id1, "撤回保留的字幕")
        assert r.status_code == 200
        v = r.json()["version"]
        s = confirm(c, v)
        check(screen(c)["text"] == "撤回保留的字幕", "发出后字幕上屏")
        seq1 = screen(c)["seq"]

        r = c.post("/api/withdraw", json={"version": s["version"], "cue_id": id1})
        check(r.status_code == 200, "撤回成功")
        d = r.json()
        check(screen(c) == {"seq": seq1 + 1, "text": None}, "撤回即清屏、序号 +1")
        check(sub_of(d, id1)["valid"] is True, "撤回保留字幕设置且仍有效")

        s = confirm(c, d["version"])
        check(screen(c)["text"] == "撤回保留的字幕", "再次发出字幕重新上屏")


def test_clear_on_archive_reset_newshow():
    fresh_db("s08.db")
    with TestClient(main.app) as c:
        print("T8 归档 / 重置 / 开新场：清屏并清除设置，序号不归零，不进档案")
        # 归档
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = set_sub(c, v, id1, "会被归档清掉的字幕")
        assert r.status_code == 200
        v = r.json()["version"]
        s = confirm(c, v)
        check(screen(c)["text"] == "会被归档清掉的字幕", "发出后上屏")
        seq_before = screen(c)["seq"]
        r = c.post("/api/archive", json={"version": s["version"], "name": "字幕场"})
        check(r.status_code == 200, "归档成功")
        v = r.json()["version"]
        scr = screen(c)
        check(scr["text"] is None and scr["seq"] > seq_before, "归档清屏且序号递增")
        check(c.get("/api/state").json()["subtitles"] == [], "归档清除字幕设置")
        aid = c.get("/api/archives").json()["archives"][0]["id"]
        detail = c.get(f"/api/archives/{aid}").json()
        check("subtitles" not in detail and "screen" not in detail, "字幕不写入档案")

        # 以档案开新场：设置与画面仍为空，序号不归零
        r = c.post(f"/api/archives/{aid}/start", json={"version": v})
        check(r.status_code == 200, "以档案开新场")
        v = r.json()["version"]
        check(r.json()["subtitles"] == [], "开新场无字幕设置")
        scr = screen(c)
        check(scr["text"] is None and scr["seq"] > seq_before, "开新场画面为空、序号不归零")

        # 手工开新场（演出中直接录入）清屏清设置
        id1 = r.json()["pending"][0]["id"]
        r = set_sub(c, v, id1, "新场字幕")
        assert r.status_code == 200
        v = r.json()["version"]
        s = confirm(c, v)
        check(screen(c)["text"] == "新场字幕", "新场字幕上屏")
        seq2 = screen(c)["seq"]
        s = setup_show(c, [{"number": "B1", "content": "新场一"},
                           {"number": "B2", "content": "新场二"}])
        check(s["subtitles"] == [], "手工录入新场清除字幕设置")
        scr = screen(c)
        check(scr["text"] is None and scr["seq"] > seq2, "手工开新场清屏、序号递增")

        # 重置
        id1 = s["pending"][0]["id"]
        r = set_sub(c, s["version"], id1, "重置前字幕")
        assert r.status_code == 200
        seq3 = screen(c)["seq"]
        r = c.post("/api/reset", json={})
        check(r.status_code == 200, "重置成功")
        check(r.json()["subtitles"] == [], "重置清除字幕设置")
        scr = screen(c)
        check(scr["text"] is None and scr["seq"] > seq3, "重置清屏、序号不归零")


def test_persistence_and_old_db():
    db_file = os.path.join(_tmp, "s09.db")
    main.DB_PATH = db_file
    with TestClient(main.app) as c:
        print("T9 持久化：重启保留设置与画面；旧库默认空")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        r = set_sub(c, v, id1, "重启保留的字幕")
        assert r.status_code == 200
        v = r.json()["version"]
        s = confirm(c, v)
        check(screen(c)["text"] == "重启保留的字幕", "发出上屏")

    # 重启（同一数据库文件）
    with TestClient(main.app) as c:
        st = c.get("/api/state").json()
        check(sub_of(st, id1)["text"] == "重启保留的字幕", "重启后字幕设置保留")
        check(screen(c)["text"] == "重启保留的字幕", "重启后观众画面保留")

    # 旧库升级：无 subtitles 表、meta 无画面列
    old_file = os.path.join(_tmp, "s09-old.db")
    conn = sqlite3.connect(old_file)
    conn.executescript(
        """
        CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK (id = 1),
                           version INTEGER NOT NULL DEFAULT 0,
                           status TEXT NOT NULL DEFAULT 'setup');
        INSERT INTO meta (id, version, status) VALUES (1, 3, 'setup');
        CREATE TABLE cues (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           number TEXT NOT NULL, content TEXT NOT NULL,
                           position INTEGER NOT NULL,
                           status TEXT NOT NULL DEFAULT 'pending');
        """
    )
    conn.commit()
    conn.close()
    main.DB_PATH = old_file
    with TestClient(main.app) as c:
        st = c.get("/api/state").json()
        check(st["subtitles"] == [], "旧库升级后无字幕设置")
        check(screen(c) == {"seq": 0, "text": None}, "旧库默认空画面、序号 0")
        check(st["version"] == 3, "旧库数据保留")


def test_write_failure_rollback():
    fresh_db("s10.db")
    with TestClient(main.app) as c:
        print("T10 写入失败整体回滚：设置与发出画面都不留半次结果")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]

        # 让字幕写入失败：INSERT 触发器抛错
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_sub_insert BEFORE INSERT ON subtitles "
                "BEGIN SELECT RAISE(ABORT, 'injected subtitle failure'); END"
            )
        with TestClient(main.app, raise_server_exceptions=False) as fc:
            r = set_sub(fc, v, id1, "会失败的字幕")
        check(r.status_code == 500, "字幕写入失败返回错误")
        with main.db() as conn:
            conn.execute("DROP TRIGGER fail_sub_insert")
        snap = c.get("/api/state").json()
        check(snap["version"] == v and snap["subtitles"] == [],
              "回滚：版本与字幕设置都不变")

        # 让发出时的画面写入失败：meta UPDATE 触发器抛错
        r = set_sub(c, v, id1, "正常字幕")
        assert r.status_code == 200
        v = r.json()["version"]
        scr0 = screen(c)
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_meta_update BEFORE UPDATE ON meta "
                "BEGIN SELECT RAISE(ABORT, 'injected meta failure'); END"
            )
        with TestClient(main.app, raise_server_exceptions=False) as fc:
            r = fc.post("/api/confirm", json={"version": v})
        check(r.status_code == 500, "发出时写库失败返回错误")
        with main.db() as conn:
            conn.execute("DROP TRIGGER fail_meta_update")
        snap = c.get("/api/state").json()
        check(snap["version"] == v and snap["issued"] == [],
              "回滚：版本与已发记录都不变（未发出）")
        check(screen(c) == scr0, "回滚：观众画面不变")

        # 回滚后可正常发出并上屏
        s = confirm(c, v)
        check(screen(c)["text"] == "正常字幕", "回滚后发出正常上屏")


def test_auto_issue_screen():
    fresh_db("s11.db")
    clock = use_fake_clock()
    with TestClient(main.app) as c:
        print("T11 自动发出：到期上屏 / 清屏规则与手工一致")
        s = setup_show(c)
        v = s["version"]
        id1, id2 = s["pending"][0]["id"], s["pending"][1]["id"]
        r = set_sub(c, v, id1, "自动上屏字幕")
        assert r.status_code == 200
        v = r.json()["version"]
        r = c.post("/api/auto", json={"version": v, "enabled": True, "interval": 10})
        assert r.status_code == 200
        v = r.json()["version"]

        clock[0] += 11
        check(main.auto_tick() is True, "自动发出第一条")
        check(screen(c)["text"] == "自动上屏字幕", "自动发出字幕上屏")
        clock[0] += 11
        check(main.auto_tick() is True, "自动发出第二条（无字幕）")
        check(screen(c)["text"] is None, "无字幕自动发出清屏")


test_set_and_clear()
test_validation_errors()
test_control_seat_protection()
test_no_round_or_timer_reset()
test_invalidation_rules()
test_issue_screen_flow()
test_withdraw_keeps_settings()
test_clear_on_archive_reset_newshow()
test_persistence_and_old_db()
test_write_failure_rollback()
test_auto_issue_screen()

main._clock = REAL_CLOCK

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
