import threading

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app, init_db

SUBJECT = "订单入站"
FULL = "order_id,amount,currency"


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth(client):
    tok = client.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


def cutoff(client, auth):
    r = client.get("/api/cutoff", headers=auth)
    assert r.status_code == 200, r.text
    return r.json()


def switch(client, auth, **body):
    return client.post("/api/cutoff/switch", headers=auth, json=body)


def validate(client, auth, subject=SUBJECT, fields=FULL):
    return client.post("/api/ingress/validate", headers=auth, json={"subject": subject, "payload_fields": fields})


def test_visible_set_seeded_and_cutout_blocks_official_pass(client, auth):
    """摘流目录、可见集、入站裁决必须同一口径：已摘流主题不得正式通过。"""
    snap = cutoff(client, auth)
    assert SUBJECT in [v["subject"] for v in snap["visible"]]
    assert validate(client, auth).json()["result"] == "通过"

    r = switch(client, auth, subject=SUBJECT, action="摘流", path="甲", expected_revision=snap["revision"])
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "正式"

    snap = cutoff(client, auth)
    assert SUBJECT not in [v["subject"] for v in snap["visible"]]
    assert {"subject": SUBJECT, "cut": 1} in [{"subject": d["subject"], "cut": d["cut"]} for d in snap["directory"]]

    blocked = validate(client, auth)
    assert blocked.status_code == 409, blocked.text
    assert "摘流偏了" in blocked.json()["detail"]
    assert blocked.json().get("result") != "通过"

    # 入站详情（批次）、对账账册、摘流目录、可见集必须互相一致
    batches = client.get("/api/ingress", headers=auth).json()["items"]
    assert batches[0]["result"] == "拒收"
    recon = client.get("/api/reconcile", headers=auth).json()
    assert recon["cutoff_active"] == 1
    assert recon["visible_subjects"] == len(snap["visible"])
    assert recon["cutoff_visible_aligned"] is True
    assert recon["cutoff_deviations"] >= 1
    assert recon["rejected"] >= 1


def test_non_visible_subject_cannot_sneak_in(client, auth):
    # 完全未知主题没有启用正式契约，按无契约拒之门外
    unknown = validate(client, auth, subject="幽灵主题", fields="x")
    assert unknown.status_code == 404

    # 绕过摘流目录直接登记启用正式契约的主题属于未纳入可见集，禁止偷接
    with app_module.connect() as conn:
        conn.execute("INSERT INTO contracts(version,subject,mode,track,status,required_fields) VALUES (?,?,?,?,?,?)",
                     ("v9", "幽灵主题", "严格", "正式", "启用", "x"))
    r = validate(client, auth, subject="幽灵主题", fields="x")
    assert r.status_code == 409, r.text
    assert "未纳入" in r.json()["detail"]
    snap = cutoff(client, auth)
    assert any(d["kind"] == "偷接" for d in snap["deviations"])


def test_path_a_failure_rolls_back_cutoff_and_visible_together(client, auth, monkeypatch):
    """甲路径：摘流清单与可见集同一事务，任一失败整笔回滚。"""
    before = cutoff(client, auth)

    def boom(conn, subject, visible):
        raise RuntimeError("visible set write failed")

    with monkeypatch.context() as m:
        m.setattr(app_module, "_write_visible", boom)
        r = switch(client, auth, subject=SUBJECT, action="摘流", path="甲", expected_revision=before["revision"])
    assert r.status_code == 500

    after = cutoff(client, auth)
    assert after["revision"] == before["revision"]
    assert SUBJECT in [v["subject"] for v in after["visible"]]
    assert all(not (d["subject"] == SUBJECT and d["cut"] == 1) for d in after["directory"])
    assert all(l["state"] != "正式" for l in after["ledger"])
    assert validate(client, auth).json()["result"] == "通过"


def test_path_b_draft_keeps_old_visible_until_atomic_commit(client, auth):
    """乙路径：草稿阶段仍按旧可见清单裁决，确认后一次原子切正式可见集。"""
    before = cutoff(client, auth)
    d = switch(client, auth, subject=SUBJECT, action="摘流", path="乙", expected_revision=before["revision"])
    assert d.status_code == 200, d.text
    draft_id = d.json()["draft_id"]
    assert d.json()["state"] == "草稿"

    # 切换完成前仍按旧可见清单裁决
    assert validate(client, auth).json()["result"] == "通过"
    assert SUBJECT in [v["subject"] for v in cutoff(client, auth)["visible"]]

    c = client.post("/api/cutoff/commit", headers=auth,
                    json={"draft_id": draft_id, "expected_revision": d.json()["revision"]})
    assert c.status_code == 200, c.text
    assert c.json()["state"] == "正式"

    assert SUBJECT not in [v["subject"] for v in cutoff(client, auth)["visible"]]
    assert validate(client, auth).status_code == 409


def test_commit_requires_draft_path(client, auth):
    snap = cutoff(client, auth)
    r = switch(client, auth, subject=SUBJECT, action="摘流", path="甲", expected_revision=snap["revision"])
    assert r.status_code == 200
    c = client.post("/api/cutoff/commit", headers=auth, json={"draft_id": r.json()["ledger_id"]})
    assert c.status_code == 409


def test_same_day_path_cannot_change(client, auth):
    snap = cutoff(client, auth)
    first = switch(client, auth, subject=SUBJECT, action="摘流", path="甲", expected_revision=snap["revision"])
    assert first.status_code == 200

    other = switch(client, auth, subject=SUBJECT, action="解除", path="乙", expected_revision=first.json()["revision"])
    assert other.status_code == 409
    assert "摘流偏了" in other.json()["detail"]

    snap = cutoff(client, auth)
    assert any(d["kind"] == "摘流偏了" for d in snap["deviations"])
    # 偏了不得产生任何目录/可见集变化
    assert snap["directory"][0]["cut"] == 1
    assert SUBJECT not in [v["subject"] for v in snap["visible"]]


def test_concurrent_switch_later_only_records_contention(client, auth):
    """两名值班同时抢改摘流清单：排队后到者只记摘流争用，不得覆盖先到结果。"""
    rev = cutoff(client, auth)["revision"]
    barrier = threading.Barrier(2)
    results = []

    def worker(action):
        barrier.wait()
        results.append((action, switch(client, auth, subject=SUBJECT, action=action, path="甲", expected_revision=rev)))

    threads = [threading.Thread(target=worker, args=(a,)) for a in ("摘流", "解除")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(r.status_code for _, r in results)
    assert codes == [200, 409], [(a, r.status_code, r.text) for a, r in results]
    loser = next(r for _, r in results if r.status_code == 409)
    assert "摘流争用" in loser.json()["detail"]

    snap = cutoff(client, auth)
    assert len(snap["contention"]) == 1
    assert snap["revision"] == rev + 1
    winner = next(a for a, r in results if r.status_code == 200)
    assert snap["directory"][0]["cut"] == (1 if winner == "摘流" else 0)


def test_reconcile_reports_cutoff_visible_alignment(client, auth):
    snap = cutoff(client, auth)
    switch(client, auth, subject=SUBJECT, action="摘流", path="甲", expected_revision=snap["revision"])
    recon = client.get("/api/reconcile", headers=auth).json()
    assert recon["cutoff_visible_aligned"] is True
    assert recon["cutoff_active"] == 1
    assert recon["cutoff_drafts_pending"] == 0


def test_policy_page_matches_directory_and_visible(client, auth):
    snap = cutoff(client, auth)
    switch(client, auth, subject=SUBJECT, action="摘流", path="甲", expected_revision=snap["revision"])
    pol = client.get("/api/cutoff/policy", headers=auth).json()
    snap = cutoff(client, auth)
    assert pol["revision"] == snap["revision"]
    assert pol["subjects"] == [d["subject"] for d in snap["directory"]]
    assert pol["policy"]["cut"].startswith("已摘流主题禁止正式通过")
    assert "可见集由摘流切换簿维护" in pol["policy"]["visible"]


def test_display_only_change_would_not_pass_switch(client, auth):
    """只改展示不改切换则不过：绕过切换簿单改目录，对账立刻报错位。"""
    before = cutoff(client, auth)
    assert validate(client, auth, subject="订单入站", fields="order_id,amount,currency").json()["result"] == "通过"

    # 绕过切换簿只改摘流目录（展示侧），不动可见集
    with app_module.connect() as conn:
        conn.execute("UPDATE cut_directory SET cut=1, path='手工' WHERE subject=?", (SUBJECT,))

    recon = client.get("/api/reconcile", headers=auth).json()
    assert recon["cutoff_visible_aligned"] is False
    # 目录已是权威：裁决立即拒绝，不会因“只看展示”而放行
    assert validate(client, auth).status_code == 409
    assert cutoff(client, auth)["revision"] == before["revision"]


def test_restart_does_not_reflow_cutoff_subject_into_visible(client, auth):
    """重启（再次 init_db）不得把已摘流主题塞回可见集。"""
    snap = cutoff(client, auth)
    switch(client, auth, subject=SUBJECT, action="摘流", path="甲", expected_revision=snap["revision"])
    init_db()
    after = cutoff(client, auth)
    assert SUBJECT not in [v["subject"] for v in after["visible"]]
    assert after["revision"] == snap["revision"] + 1
    assert validate(client, auth).status_code == 409
