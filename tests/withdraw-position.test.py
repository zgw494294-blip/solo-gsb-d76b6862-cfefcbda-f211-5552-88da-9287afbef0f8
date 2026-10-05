"""撤回位置回归测试：反复确认/撤回同一提示不再导致待发顺序改变或撤回溢出。

缺陷背景：旧实现撤回时以 MAX(position)+1 为基数整体平移待发、重排时在
MAX(position) 之上追加位置。录入 A1、A2、A3，确认 A1（再把待发重排为
A3、A2）后反复"撤回 A1 / 确认 A1"，位置每轮近似翻倍：

- 重排路径累计第 61 次撤回时，位置运算越过 SQLite 有符号整数上限
  （2^63-1），结果被转成浮点，原本仍可区分的两个大位置舍入为同值，
  ORDER BY position 回退按 id 排序，待发由 A1、A3、A2 错成 A1、A2、A3；
- 不重排时第 62 次撤回的 Python 大整数无法绑定为 SQLite INTEGER，直接抛
  OverflowError，撤回与版本加一虽同事务回滚但接口 500。

修复：撤回与重排都不再以 MAX(position) 为基数，而是对**全部待发**按显示
顺序重新分配紧凑位置 1..n（每场最多 12 条，永远远离整数上限）；既有库中
已接近 SQLite 整数上限但仍可区分的位置也能正常撤回。

覆盖：
- 不重排 / 中途重排两条路径各 70 轮"撤回 + 再确认"，每轮提交最新版本，
  核对待发内容/相对顺序、已发记录、版本递增与库内紧凑位置；
- 每轮先用过期版本撤回验证 409 附最新状态且数据不变；
- 两条路径都在第 35 轮撤回后重启（重新建应用连接同一数据库）继续；
- 既有大位置库（近 2^63-1 且仍可区分）下的撤回、重排、完成态撤回、
  再确认；位置已溢出成同值浮点的旧库也能撤回并被压实（不恢复已丢失的
  历史顺序）；大位置撤回后重启保留；
- 撤回写入失败（cue 更新失败 / meta 版本更新失败）整体回滚：提示、位置、
  状态、版本一律不变，失败后可正常撤回，不吞掉异常；
- 控制席 403、完成态回演出中、暂停标记不变等既有行为在新位置分配下保持。

运行：python3 tests/withdraw-position.test.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-withdraw-pos-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "cues.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402

failures = 0
SQLITE_MAX_INT = 2 ** 63 - 1


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


def setup_three(c):
    s = c.post("/api/setup", json={"cues": CUES3})
    assert s.status_code == 200, s.text
    st = s.json()
    return st, [p["id"] for p in st["pending"]]


def confirm(c, v, headers=None):
    r = c.post("/api/confirm", json={"version": v}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def withdraw(c, v, cue_id, headers=None):
    return c.post(
        "/api/withdraw", json={"version": v, "cue_id": cue_id}, headers=headers
    )


def db_positions(status=None):
    """直接查库：返回 [(id, position), ...]，按显示排序口径。"""
    with main.db() as conn:
        rows = conn.execute(
            "SELECT id, position, status FROM cues ORDER BY position ASC, id ASC"
        ).fetchall()
    return [
        (r["id"], r["position"]) for r in rows if status is None or r["status"] == status
    ]


def assert_compact_pending(id_order):
    """库内待发位置必须恰好是 1..n 的整数（永不随操作次数增长）。"""
    got = db_positions("pending")
    expected = [(cue_id, pos) for pos, cue_id in enumerate(id_order, start=1)]
    check(got == expected, "库内待发位置紧凑为 1..%d：%s" % (len(id_order), got))
    check(all(isinstance(pos, int) for _, pos in got), "位置全部为整数，未退化为浮点")


def force_positions(mapping):
    """直接改库写入指定位置（模拟既有库的历史大位置）。"""
    with main.db() as conn:
        for cue_id, pos in mapping.items():
            conn.execute("UPDATE cues SET position = ? WHERE id = ?", (pos, cue_id))


# ------------------------------------------------- 两条路径各 70 轮 + 重启

def run_loop(reorder: bool):
    """不重排 / 重排两条路径各 70 轮撤回+再确认，第 35 轮撤回后重启继续。"""
    label = "重排路径（A3、A2）" if reorder else "不重排路径（A2、A3）"
    main.DB_PATH = os.path.join(_tmp, "loop-%s.db" % ("reorder" if reorder else "plain"))
    after_withdraw = ["A1", "A3", "A2"] if reorder else ["A1", "A2", "A3"]
    after_confirm = ["A3", "A2"] if reorder else ["A2", "A3"]
    ids_withdraw = None

    with TestClient(main.app) as c:
        _, ids = setup_three(c)
        a1, a2, a3 = ids
        ids_withdraw = [a1, a3, a2] if reorder else [a1, a2, a3]
        st = c.get("/api/state").json()
        v = confirm(c, st["version"])["version"]
        if reorder:
            r = c.post("/api/reorder", json={"version": v, "order": [a3, a2]})
            check(r.status_code == 200, "%s：确认 A1 后重排待发为 A3、A2" % label)
            v = r.json()["version"]

    ROUNDS = 70
    for i in range(1, ROUNDS + 1):
        with TestClient(main.app) as c:
            # 每轮先提交过期版本：必须 409 附最新状态，数据不变。
            r = withdraw(c, v - 1, a1)
            check(r.status_code == 409, "%s 第 %d 轮：过期版本撤回 409" % (label, i))
            latest = c.get("/api/state").json()
            check(
                r.json()["state"]["version"] == v == latest["version"],
                "409 附带最新状态（版本号最新）",
            )
            check(
                [p["number"] for p in latest["pending"]] == after_confirm,
                "409 不改数据（待发仍为 %s）" % after_confirm,
            )

            r = withdraw(c, v, a1)
            check(r.status_code == 200, "%s 累计第 %d 次撤回成功（不溢出）" % (label, i))
            st = r.json()
            check(st["version"] == v + 1, "撤回与版本 +1 一起保存")
            check(st["status"] == "live" and st["paused"] is False, "仍为演出中、未暂停")
            check([x["id"] for x in st["issued"]] == [], "已发记录清空（A1 退回）")
            check(
                [p["number"] for p in st["pending"]] == after_withdraw,
                "撤回后待发为 %s（第 %d 次仍不改变）" % (after_withdraw, i),
            )
            check(
                st["current"]["id"] == a1 and st["current"]["content"] == "灯光渐亮",
                "A1 按发出快照回到队首，保留 id 与编号",
            )
            assert_compact_pending(ids_withdraw)
            v = st["version"]

        # 第 35 轮撤回后重启：另起应用连接同一库，验证保留后继续完成本轮再确认。
        if i == 35:
            with TestClient(main.app) as c:
                st = c.get("/api/state").json()
                check(
                    [p["number"] for p in st["pending"]] == after_withdraw
                    and st["version"] == v and [x["id"] for x in st["issued"]] == [],
                    "%s：第 35 轮撤回结果重启后保留（顺序/版本/已发）" % label,
                )
                assert_compact_pending(ids_withdraw)
                st = confirm(c, v)
                check(st["version"] == v + 1, "%s：重启后再确认成功并 +1" % label)
                v = st["version"]
            continue

        with TestClient(main.app) as c:
            st = confirm(c, v)
            check(st["version"] == v + 1, "%s 第 %d 轮：再确认成功并 +1" % (label, i))
            check(
                [p["number"] for p in st["pending"]] == after_confirm,
                "再确认后其余待发内容与相对顺序不变",
            )
            check([x["id"] for x in st["issued"]] == [a1], "再确认后已发仅 A1")
            v = st["version"]

    print("  · %s：70 轮反复撤回/确认（含第 35 轮后重启）全部稳定" % label)


# ------------------------------------------------------------- 大位置旧库

def test_huge_positions_live():
    main.DB_PATH = os.path.join(_tmp, "huge-live.db")
    with TestClient(main.app) as c:
        print("L1 既有大位置库（近 2^63-1 且仍可区分）：撤回/重排/再确认正常，位置被压实")
        _, (a1, a2, a3) = setup_three(c)
        v = confirm(c, c.get("/api/state").json()["version"])["version"]
        # 已发 A1 与两条待发位置都顶在整数上限附近，且彼此仍可区分。
        force_positions(
            {a1: SQLITE_MAX_INT - 2, a2: SQLITE_MAX_INT - 1, a3: SQLITE_MAX_INT}
        )
        st = c.get("/api/state").json()
        check([p["id"] for p in st["pending"]] == [a2, a3], "大位置仍可区分：撤回前待发为 A2、A3")
        r = withdraw(c, v, a1)
        check(r.status_code == 200, "近上限位置撤回不再 OverflowError")
        st = r.json()
        check(
            [p["id"] for p in st["pending"]] == [a1, a2, a3],
            "撤回后 A1 在队首，其余待发相对顺序不变（A2、A3）",
        )
        assert_compact_pending([a1, a2, a3])
        v = st["version"]

        # 大位置基线下重排（旧代码会在 MAX(position) 上追加而再次溢出）。
        r = c.post("/api/reorder", json={"version": v, "order": [a3, a2, a1]})
        check(r.status_code == 200, "大位置压实后重排正常")
        v = r.json()["version"]
        assert_compact_pending([a3, a2, a1])

        # 再走 5 轮撤回/确认，顺序与位置始终稳定。
        for _ in range(5):
            v = confirm(c, v)["version"]
            st = withdraw(c, v, a3).json()
            check(st["version"] == v + 1, "大位置修复后反复撤回仍成功并 +1")
            check(
                [p["id"] for p in st["pending"]] == [a3, a2, a1],
                "反复撤回后待发仍为 A3、A2、A1",
            )
            assert_compact_pending([a3, a2, a1])
            v = st["version"]

    # 重启保留压实结果。
    with TestClient(main.app) as c:
        st = c.get("/api/state").json()
        check([p["id"] for p in st["pending"]] == [a3, a2, a1], "大位置撤回/压实后重启顺序保留")
        assert_compact_pending([a3, a2, a1])


def test_huge_positions_done():
    main.DB_PATH = os.path.join(_tmp, "huge-done.db")
    with TestClient(main.app) as c:
        print("L2 大位置 + 完成态撤回：回到演出中，末条回到队首")
        _, (a1, a2, a3) = setup_three(c)
        v = c.get("/api/state").json()["version"]
        for _ in range(3):
            v = confirm(c, v)["version"]
        check(c.get("/api/state").json()["status"] == "done", "三条全部发出进入完成态")
        force_positions(
            {a1: SQLITE_MAX_INT - 5, a2: SQLITE_MAX_INT - 2, a3: SQLITE_MAX_INT}
        )
        r = withdraw(c, v, a3)
        check(r.status_code == 200, "大位置完成态撤回成功")
        st = r.json()
        check(st["status"] == "live", "完成态撤回后回到演出中")
        check(st["current"]["id"] == a3, "末条 A3 成为当前待发")
        check([x["id"] for x in st["issued"]] == [a1, a2], "前两条已发记录保留")
        assert_compact_pending([a3])


def test_float_collapsed_positions():
    main.DB_PATH = os.path.join(_tmp, "float-collapse.db")
    with TestClient(main.app) as c:
        print("L3 位置已溢出成同值浮点的旧库：可正常撤回并压实（不恢复已丢失顺序）")
        _, (a1, a2, a3) = setup_three(c)
        v = confirm(c, c.get("/api/state").json()["version"])["version"]
        # 模拟旧缺陷第 61 次撤回后的现场：两个待发位置是同一个浮点。
        with main.db() as conn:
            conn.execute(
                "UPDATE cues SET position = 13835058055282163712.0 WHERE id IN (?, ?)",
                (a2, a3),
            )
        r = withdraw(c, v, a1)
        check(r.status_code == 200, "浮点位置库撤回不报错")
        st = r.json()
        v = st["version"]
        order = [p["id"] for p in st["pending"]]
        assert_compact_pending(order)
        # 压实后再反复撤回/确认必须稳定，顺序不再漂移。
        for _ in range(5):
            v = confirm(c, v)["version"]
            st = withdraw(c, v, a1).json()
            check(
                [p["id"] for p in st["pending"]] == order,
                "压实后各轮待发顺序稳定，不再次漂移",
            )
            assert_compact_pending(order)
            v = st["version"]


# ------------------------------------------------------------- 失败回滚

def test_failure_rollback():
    main.DB_PATH = os.path.join(_tmp, "rollback.db")
    with TestClient(main.app) as c:
        print("R1 撤回写入失败整体回滚：提示、位置、状态、版本一律不变，异常不被吞掉")
        _, (a1, a2, a3) = setup_three(c)
        v = confirm(c, c.get("/api/state").json()["version"])["version"]
        # 顶到上限附近：旧代码一旦未回滚位置，下一次必然溢出。
        force_positions(
            {a1: SQLITE_MAX_INT - 2, a2: SQLITE_MAX_INT - 1, a3: SQLITE_MAX_INT}
        )
        pos_before = db_positions()

        # 情形一：撤回过程中 cues 更新失败（注入触发器 ABORT）。
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_cue_update BEFORE UPDATE ON cues "
                "BEGIN SELECT RAISE(ABORT, 'injected cue failure'); END"
            )
        with TestClient(main.app, raise_server_exceptions=False) as fc:
            r = withdraw(fc, v, a1)
            check(r.status_code == 500, "cue 更新失败时接口报错（不限制操作、不吞异常）")
        state_after = c.get("/api/state").json()
        check(state_after["version"] == v, "失败后版本不变")
        check(
            state_after["status"] == "live" and len(state_after["issued"]) == 1,
            "失败后已发/待发状态不变（A1 仍已发）",
        )
        check(db_positions() == pos_before, "失败后位置不变（仍是近上限大整数）")

        # 情形二：cues 已改写但 meta 版本更新失败——同一事务，cues 也必须回滚。
        with main.db() as conn:
            conn.execute("DROP TRIGGER fail_cue_update")
            conn.execute(
                "CREATE TRIGGER fail_meta_update BEFORE UPDATE ON meta "
                "BEGIN SELECT RAISE(ABORT, 'injected meta failure'); END"
            )
        with TestClient(main.app, raise_server_exceptions=False) as fc:
            r = withdraw(fc, v, a1)
            check(r.status_code == 500, "meta 更新失败同样报错")
        state_after = c.get("/api/state").json()
        check(state_after["version"] == v, "版本 +1 与撤回同事务：失败则版本不变")
        check(
            len(state_after["issued"]) == 1 and state_after["current"]["id"] == a2,
            "失败后撤回不生效（A1 未退回队首）",
        )
        check(db_positions() == pos_before, "失败后已改写的 cues 位置一并回滚")

        # 移除注入后，原参数（最新版本 + cue_id）可立即正常撤回。
        with main.db() as conn:
            conn.execute("DROP TRIGGER fail_meta_update")
        r = withdraw(c, v, a1)
        check(r.status_code == 200, "故障移除后撤回正常")
        st = r.json()
        check(st["version"] == v + 1, "成功撤回版本 +1")
        check([p["id"] for p in st["pending"]] == [a1, a2, a3], "撤回结果正确")
        assert_compact_pending([a1, a2, a3])


# --------------------------------------- 既有行为：暂停 / 完成态 / 403 / 409

def test_existing_behaviors_kept():
    main.DB_PATH = os.path.join(_tmp, "behaviors.db")
    with TestClient(main.app) as c:
        print("B1 暂停标记、完成态回演出中、403/409 与最新状态在新位置分配下保持")
        _, (a1, a2, a3) = setup_three(c)
        v = c.get("/api/state").json()["version"]

        # 暂停时撤回：暂停标记不变。
        v = confirm(c, v)["version"]
        r = c.post("/api/pause", json={"version": v, "paused": True})
        check(r.status_code == 200, "暂停成功")
        v = r.json()["version"]
        r = withdraw(c, v, a1)
        check(r.status_code == 200 and r.json()["paused"] is True, "暂停时撤回成功且仍暂停")
        v = r.json()["version"]
        assert_compact_pending([a1, a2, a3])
        check(c.post("/api/confirm", json={"version": v}).status_code == 409, "仍暂停，确认继续 409")
        r = c.post("/api/pause", json={"version": v, "paused": False})
        check(r.status_code == 200, "恢复成功")
        v = r.json()["version"]

        # 启用控制席：无凭据撤回 403 且数据不变，带凭据成功。
        v = confirm(c, v)["version"]
        r = c.post("/api/control/enable", json={"version": v})
        check(r.status_code == 200, "启用控制席")
        tok = r.json()["control_token"]
        v = r.json()["version"]
        r = withdraw(c, v, a1)
        check(r.status_code == 403, "无凭据撤回 403")
        check(
            r.json()["state"]["control_granted"] is False
            and r.json()["state"]["version"] == v,
            "403 附最新状态且数据不变",
        )

        # 带凭据继续：撤回 A1、再发完到完成态，验证完成态与跳撤 409。
        r = withdraw(c, v, a1, headers={"X-Control-Token": tok})
        check(r.status_code == 200, "带凭据撤回成功")
        v = r.json()["version"]
        for _ in range(3):
            v = confirm(c, v, headers={"X-Control-Token": tok})["version"]
        st = c.get("/api/state", headers={"X-Control-Token": tok}).json()
        check(st["status"] == "done", "三条发完进入完成态")
        r = withdraw(c, v, a1, headers={"X-Control-Token": tok})
        check(r.status_code == 409, "完成态跳过末条撤 A1：409")
        check(
            r.json()["state"]["version"] == v and r.json()["state"]["status"] == "done",
            "409 附最新状态（done），数据不变",
        )
        r = withdraw(c, v, a3, headers={"X-Control-Token": tok})
        check(r.status_code == 200 and r.json()["status"] == "live", "撤末条 A3：完成态回到演出中")
        v = r.json()["version"]
        assert_compact_pending([a3])
        r = withdraw(c, v, a3, headers={"X-Control-Token": tok})
        check(r.status_code == 409, "已撤回后同 id 再撤：409（不批量撤回）")
        check(r.json()["state"]["version"] == v, "409 后版本不变")


run_loop(False)
run_loop(True)
test_huge_positions_live()
test_huge_positions_done()
test_float_collapsed_positions()
test_failure_rollback()
test_existing_behaviors_kept()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
