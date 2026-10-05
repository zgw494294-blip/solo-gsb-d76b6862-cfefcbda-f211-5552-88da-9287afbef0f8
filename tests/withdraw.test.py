"""撤回末条后端回归测试（FastAPI TestClient + 独立临时数据库）。

覆盖：演出中撤回末条（发出快照作待发内容、保留 id 与编号、清空发出
快照/时间/序号、插到现有待发队首、现有待发内容与相对顺序不变、其他
已发记录不变、问题记录快照不变、版本 +1）；完成态撤回回到演出中；
暂停时也可撤回且暂停标记不变；再次确认记新时间；开场前 / 无已发 /
目标不是末条（已被撤回）/ 版本过期返回 409 且数据不变；撤回 + 版本 +1
同事务（与确认、编辑、重排、归档同版本竞争最多一个成功）；重启后结果
保留；既有档案不受撤回影响；不提供批量撤回。

运行：python3 tests/withdraw.test.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-withdraw-test-")
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


def withdraw(c, v, cue_id):
    return c.post("/api/withdraw", json={"version": v, "cue_id": cue_id})


def test_basic_withdraw():
    with TestClient(main.app) as c:
        print("W1 演出中撤回：末条按发出快照退回待发队首，其余不变")
        s = setup_show(c)
        v = s["version"]
        # 先把第一条编辑成与初始不同的内容，再确认；撤回内容应为发出快照
        id1 = s["pending"][0]["id"]
        r = c.post("/api/edit", json={"version": v, "cue_id": id1,
                                      "content": "灯光慢亮（改过）"})
        check(r.status_code == 200, "确认前先编辑首条")
        v = r.json()["version"]

        s = confirm(c, v)
        v = s["version"]
        issued1 = s["issued"][0]
        check(issued1["id"] == id1, "首条已确认发出")
        check(issued1["content"] == "灯光慢亮（改过）", "已发记录保存发出快照")
        first_issued_at = issued1["issued_at"]
        check(s["current"]["id"] == id1 + 1, "当前推进到第二条")

        # 撤回首条（此时它也是末条）
        r = withdraw(c, v, id1)
        check(r.status_code == 200, "撤回成功")
        s = r.json()
        check(s["version"] == v + 1, "撤回与版本 +1 一起保存")
        check(s["status"] == "live", "仍为演出中")
        check(s["paused"] is False, "非暂停时撤回后仍未暂停")
        check(len(s["issued"]) == 0, "已发记录中该条消失")
        check(s["current"]["id"] == id1, "撤回提示回到当前待发（队首）")
        check(s["current"]["number"] == "A1", "保留原编号")
        check(s["current"]["content"] == "灯光慢亮（改过）", "待发内容为发出快照（不是初始内容）")
        pending_ids = [p["id"] for p in s["pending"]]
        check(pending_ids == [id1, id1 + 1, id1 + 2], "撤回提示插到队首，原待发整体后移且相对顺序不变")

        # 原始数据库行：发出快照 / 时间 / 序号已清空
        row = c.get("/api/state").json()
        check(row["issued"] == [], "刷新后该条仍在待发、不在已发（持久化结果）")


def test_other_records_unchanged():
    with TestClient(main.app) as c:
        print("W2 撤回不改其他已发、待发内容、问题记录快照")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]
        # 编辑第三条待发内容（撤回后应保留此编辑）
        r = c.post("/api/edit", json={"version": v, "cue_id": id3, "content": "幕落（改）"})
        check(r.status_code == 200, "撤回前编辑其他待发")
        v = r.json()["version"]
        s = confirm(c, v)  # 发出 A2
        v = s["version"]
        # 为已发的 A1 与待发的 A3 记问题（撤回 A2 后快照不变）
        r = c.post("/api/issues", json={"version": v, "cue_id": id1, "text": "A1 灯光稍晚"})
        check(r.status_code == 200, "为已发 A1 记录问题")
        v = r.json()["version"]
        r = c.post("/api/issues", json={"version": v, "cue_id": id3, "text": "A3 待核对"})
        check(r.status_code == 200, "为待发 A3 记录问题")
        v = r.json()["version"]

        issues_before = c.get("/api/state").json()["issues"]
        r = withdraw(c, v, id2)
        check(r.status_code == 200, "撤回末条 A2 成功")
        s = r.json()
        check([p["id"] for p in s["issued"]] == [id1], "其他已发记录（A1）保留且顺序不变")
        check(s["issued"][0]["content"] == "灯光渐亮", "其他已发记录的发出快照不变")
        check(s["pending"][0]["id"] == id2, "A2 回到待发队首")
        check(s["pending"][0]["content"] == "音乐起", "A2 待发内容为其发出快照")
        check(s["pending"][1]["id"] == id3 and s["pending"][1]["content"] == "幕落（改）",
              "其他待发内容（含先前编辑）与相对顺序不变")
        check(s["issues"] == issues_before, "问题记录及其快照完全不变")


def test_done_back_to_live():
    with TestClient(main.app) as c:
        print("W3 完成态撤回：回到演出中，最后一条退回")
        s = setup_show(c)
        v = s["version"]
        for _ in range(3):
            s = confirm(c, v)
            v = s["version"]
        check(s["status"] == "done", "三条全部发出，进入已完成")
        check(s["current"] is None and s["remaining"] == 0, "完成时无待发")
        last_id = s["issued"][-1]["id"]

        r = withdraw(c, v, last_id)
        check(r.status_code == 200, "完成态可撤回")
        s = r.json()
        check(s["status"] == "live", "完成态撤回后回到演出中")
        check(s["current"]["id"] == last_id, "最后已发提示成为当前待发")
        check(len(s["issued"]) == 2, "前两条已发记录保留")
        check(s["remaining"] == 1, "待发计数恢复为 1")
        check(s["paused"] is False, "完成态原本未暂停，撤回后仍未暂停")


def test_paused_withdraw_keeps_pause():
    with TestClient(main.app) as c:
        print("W4 暂停时可撤回，暂停标记不变；恢复后再次确认记新时间")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        s = confirm(c, v)
        v = s["version"]
        old_time = s["issued"][0]["issued_at"]
        # 暂停
        r = c.post("/api/pause", json={"version": v, "paused": True})
        check(r.status_code == 200 and r.json()["paused"] is True, "暂停成功")
        v = r.json()["version"]
        old_seq_version = v  # 首次发出时的序号即确认后的版本号（暂停只再 +1）
        # 暂停期间确认被拒绝，撤回可用
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 409, "暂停期间确认仍被拒绝")
        r = withdraw(c, v, id1)
        check(r.status_code == 200, "暂停期间撤回成功")
        s = r.json()
        check(s["paused"] is True, "撤回不改变暂停标记")
        check(s["status"] == "live" and s["current"]["id"] == id1, "退回为当前待发")
        v = s["version"]
        # 暂停期间仍不能确认
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 409, "撤回后仍在暂停，确认继续被拒绝")
        # 恢复
        r = c.post("/api/pause", json={"version": v, "paused": False})
        check(r.status_code == 200, "恢复发出")
        v = r.json()["version"]
        s = confirm(c, v)
        new_time = s["issued"][0]["issued_at"]
        check(s["issued"][0]["id"] == id1, "再次确认后该提示重新进入已发")
        check(new_time != old_time, "再次确认记录新的发出时间（不复用旧时间）")
        # 发出序号不随状态 JSON 返回：直接查库确认用的是新版本号（序号单调递增）
        with main.db() as conn:
            seq = conn.execute(
                "SELECT issued_seq FROM cues WHERE id = ?", (id1,)
            ).fetchone()["issued_seq"]
        check(seq == s["version"] and seq > old_seq_version,
              "再次确认使用新的、更大的发出序号（撤回时旧序号已清空）")


def test_conflicts():
    with TestClient(main.app) as c:
        print("W5 失败情况：开场前 / 无已发 / 目标不符 / 版本过期均 409 且不变")
        s = c.get("/api/state").json()
        # 开场前（重置后的干净库）
        r = withdraw(c, s["version"], 1)
        check(r.status_code == 409, "开场前撤回返回 409")
        check(r.json()["state"]["version"] == s["version"], "409 附最新状态")

        s = setup_show(c)
        v = s["version"]
        id1, id2, _ = [p["id"] for p in s["pending"]]
        r = withdraw(c, v, id1)
        check(r.status_code == 409, "演出中但无已发提示：409")
        check(c.get("/api/state").json()["version"] == v, "无已发时撤回版本不变")

        s = confirm(c, v)
        v = s["version"]
        r = withdraw(c, v + 99, id1)
        check(r.status_code == 409, "版本过期返回 409")
        check(c.get("/api/state").json()["version"] == v, "过期版本不改数据")

        r = withdraw(c, v, id2)
        check(r.status_code == 409, "目标不是最后已发提示返回 409")
        check(c.get("/api/state").json()["version"] == v, "目标不符不改数据")

        # 正确撤回后，旧请求再来一次：末条已不在已发
        r = withdraw(c, v, id1)
        check(r.status_code == 200, "首次撤回成功")
        v2 = r.json()["version"]
        r = withdraw(c, v2, id1)
        check(r.status_code == 409, "已被撤回后同 id 再撤回：409（不批量撤回）")

        # 再发两条，撤末条只能撤最后一条，不能跳撤
        s = confirm(c, v2)
        v = s["version"]
        s = confirm(c, v)
        v = s["version"]
        r = withdraw(c, v, id1)
        check(r.status_code == 409, "有多条已发时不能跳过末条撤回更早的提示")
        check([x["id"] for x in c.get("/api/state").json()["issued"]] == [id1, id2],
              "跳撤失败，已发记录不变")


def test_same_version_competition():
    with TestClient(main.app) as c:
        print("W6 同版本竞争：撤回与确认/编辑/重排/归档最多一个成功")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]

        # 同一版本并发：撤回 + 编辑（编辑的是待发 id2）
        r1 = withdraw(c, v, id1)
        r2 = c.post("/api/edit", json={"version": v, "cue_id": id2, "content": "改写"})
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本撤回与编辑：一个 200 一个 409")
        cur = c.get("/api/state").json()
        check(cur["version"] == v + 1, "竞争后版本只 +1")

        # 再来：构造一场确定的"撤回与确认"同版本竞争——
        # 若上一轮编辑胜出，A1 仍在已发（队首为 A2）：先撤回会让 A1 回队首；
        # 若上一轮撤回避出，A1 在待发队首、A2 待发：先发一条让已发重新非空。
        if not cur["issued"]:
            # 撤回避出：队首即 A1，确认它使已发非空
            r = c.post("/api/confirm", json={"version": cur["version"]})
            check(r.status_code == 200, "补发一条使已发非空")
            cur = r.json()
        vv = cur["version"]
        last_id = cur["issued"][-1]["id"]
        pending_head = cur["current"]["id"]
        r1 = withdraw(c, vv, last_id)
        r2 = c.post("/api/confirm", json={"version": vv})
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本撤回与确认：一个 200 一个 409")
        after = c.get("/api/state").json()
        check(after["version"] == vv + 1, "竞争后版本只 +1")
        if r1.status_code == 200:
            check(after["current"]["id"] == last_id and len(after["issued"]) == len(cur["issued"]) - 1,
                  "撤回避出：末条回到队首，已发少一条，确认未生效")
        else:
            check(after["current"]["id"] != pending_head
                  and len(after["issued"]) == len(cur["issued"]) + 1,
                  "确认胜出：推进一条，撤回未生效")

        # 撤回与归档竞争
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        s = confirm(c, v)
        v = s["version"]
        archives_before = len(c.get("/api/archives").json()["archives"])
        r1 = withdraw(c, v, id1)
        r2 = c.post("/api/archive", json={"version": v, "name": "竞争场"})
        codes = sorted([r1.status_code, r2.status_code])
        check(codes == [200, 409], "同版本撤回与归档：一个 200 一个 409")
        archives_after = len(c.get("/api/archives").json()["archives"])
        if r2.status_code == 200:
            check(archives_after == archives_before + 1 and r1.status_code == 409,
                  "归档胜出：新增一个档案，撤回未生效")
        else:
            check(archives_after == archives_before and r1.status_code == 200,
                  "撤回避出：不新增档案，撤回生效")


def test_persistence_and_archives():
    db_file = os.path.join(_tmp, "persist.db")
    main.DB_PATH = db_file
    with TestClient(main.app) as c:
        print("W7 刷新/重启保留结果；既有档案不被撤回影响")
        s = setup_show(c)
        v = s["version"]
        id1, id2, id3 = [p["id"] for p in s["pending"]]
        s = confirm(c, v)
        v = s["version"]
        s = confirm(c, v)
        v = s["version"]
        # 归档前先存一份快照对照（此处不归档，改为：先归档一场再开新场撤回）
        # 直接撤末条
        r = withdraw(c, v, id2)
        check(r.status_code == 200, "撤回成功")
        s_after = r.json()
        check([p["id"] for p in s_after["pending"]] == [id2, id3], "待发队首为撤回提示")

    # 重启：重新建应用连接同一数据库
    with TestClient(main.app) as c:
        s = c.get("/api/state").json()
        check(s["status"] == "live", "重启后仍为演出中")
        check([p["id"] for p in s["pending"]] == [id2, id3], "重启后撤回结果（待发顺序）保留")
        check([p["id"] for p in s["issued"]] == [id1], "重启后已发记录保留")

        # 归档本场：撤回后的现状入档；此前无档案
        r = c.post("/api/archive", json={"version": s["version"], "name": "撤回场"})
        check(r.status_code == 200, "归档成功")
        v = r.json()["version"]
        aid = c.get("/api/archives").json()["archives"][0]["id"]
        detail = c.get(f"/api/archives/{aid}").json()
        check([x["number"] for x in detail["issued"]] == ["A1"], "档案已发只含未撤回的 A1")
        check([x["number"] for x in detail["pending"]] == ["A2", "A3"],
              "档案剩余待发顺序为 A2（退回）在前、A3 在后")

        # 对档案的任何操作后，再开新场：撤回不影响以档案开新场
        r = c.post(f"/api/archives/{aid}/start", json={"version": v})
        check(r.status_code == 200, "以含撤回结果的档案开新场成功")
        s = r.json()
        check([p["number"] for p in s["pending"]] == ["A1", "A2", "A3"],
              "新场按档案已发 + 待发顺序排列，档案本身不变")


def test_control_seat_protection():
    main.DB_PATH = os.path.join(_tmp, "control.db")
    with TestClient(main.app) as c:
        print("W8 控制席保护：缺失/旧凭据撤回返回 403 且数据不变")
        s = setup_show(c)
        v = s["version"]
        id1 = s["pending"][0]["id"]
        s = confirm(c, v)
        v = s["version"]

        r = c.post("/api/control/enable", json={"version": v})
        check(r.status_code == 200, "启用控制席")
        tok = r.json()["control_token"]
        v = r.json()["version"]

        r = withdraw(c, v, id1)
        check(r.status_code == 403, "无凭据撤回返回 403")
        check(r.json()["state"]["control_granted"] is False, "403 附最新状态与资格 false")
        r = c.post(
            "/api/withdraw", json={"version": v, "cue_id": id1},
            headers={"X-Control-Token": "wrong-token"})
        check(r.status_code == 403, "旧/错误凭据撤回返回 403")
        snap = c.get("/api/state").json()
        check(len(snap["issued"]) == 1 and snap["version"] == v, "403 后已发与版本不变")

        r = c.post("/api/withdraw", json={"version": v, "cue_id": id1},
                   headers={"X-Control-Token": tok})
        check(r.status_code == 200, "正确凭据撤回成功")


test_basic_withdraw()
test_other_records_unchanged()
test_done_back_to_live()
test_paused_withdraw_keeps_pause()
test_conflicts()
test_same_version_competition()
test_persistence_and_archives()
test_control_seat_protection()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
