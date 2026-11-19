"""双成功切断失败测。

顺序口径：先由 fixtures/double_restore_fixture.py 复现双开都成功，
再跑本文件。修复前这些用例必须红；修复后全绿。
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app, init_db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures"))
from double_restore_fixture import fire_two_restores, freeze_source  # noqa: E402


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _client():
    client = TestClient(app)
    token = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    return client, {"Authorization": f"Bearer {token}"}


def _source(client, headers, name):
    return next(
        s
        for s in client.get("/api/sources", headers=headers).json()["items"]
        if s["name"] == name
    )


def test_same_source_double_restore_only_one_success_and_first_audit_kept():
    """唯一成功失败测：双开只允许一次成功，后交整笔拒绝，首次审计保留。"""
    client, headers = _client()
    freeze_source(client, headers, "订单源")

    results = fire_two_restores(
        client, headers, "订单源", desks=("调度席", "补采席")
    )
    statuses = sorted(r["status"] for r in results)
    assert statuses == [200, 409], results

    success = next(r for r in results if r["status"] == 200)
    rejected = next(r for r in results if r["status"] == 409)
    assert "双成功争用" in str(rejected["body"]), rejected
    assert success["body"]["output_mode"] == "可写"

    catchups = [
        r
        for r in client.get("/api/catchups", headers=headers).json()["items"]
        if r["source"] == "订单源" and r["status"] == "成功"
    ]
    assert len(catchups) == 1, catchups
    # 首次审计就是成功响应里那条，且没有被第二次恢复改写
    assert catchups[0]["id"] == success["body"]["audit_id"], catchups
    assert catchups[0]["restored_mode"] == "可写"
    assert _source(client, headers, "订单源")["output_mode"] == "可写"


def test_dispatch_and_supplement_desks_contention_trace_required():
    """双路抢恢失败测：调度席与补采席并行恢复同源，后交只记双成功争用。"""
    client, headers = _client()
    freeze_source(client, headers, "支付源")

    results = fire_two_restores(
        client, headers, "支付源", desks=("调度席", "补采席")
    )
    assert sorted(r["status"] for r in results) == [200, 409], results

    attempts = [
        a
        for a in client.get("/api/audit-appendix", headers=headers).json()["items"]
        if a["source"] == "支付源"
    ]
    successes = [a for a in attempts if a["outcome"] == "成功"]
    contentions = [a for a in attempts if a["outcome"] == "双成功争用"]
    assert len(successes) == 1, attempts
    assert len(contentions) == 1, attempts  # 缺双成争用留痕则红
    assert contentions[0]["desk"] in {"调度席", "补采席"}
    assert contentions[0]["desk"] != successes[0]["desk"]
    assert "双成功争用" in contentions[0]["reason"]


def test_second_whole_transaction_rejected_not_just_message():
    """后交整笔拒绝：不写第二条成功审计、不再推版本、源只改一次。"""
    client, headers = _client()
    freeze_source(client, headers, "订单源")

    first = client.post(
        "/api/catchup/订单源", headers=headers, json={"desk": "调度席"}
    )
    assert first.status_code == 200, first.text
    rev_after_first = _source(client, headers, "订单源")["rev"]

    second = client.post(
        "/api/catchup/订单源", headers=headers, json={"desk": "补采席"}
    )
    assert second.status_code == 409, second.text
    assert "双成功争用" in second.text

    assert _source(client, headers, "订单源")["rev"] == rev_after_first
    catchups = [
        r
        for r in client.get("/api/catchups", headers=headers).json()["items"]
        if r["source"] == "订单源" and r["status"] == "成功"
    ]
    assert len(catchups) == 1, catchups


def test_five_read_surfaces_only_see_one_success():
    """水位卡片、审计附页、补采列表、源目录、交接卡只留一次成功。"""
    client, headers = _client()
    freeze_source(client, headers, "物流源")
    results = fire_two_restores(
        client, headers, "物流源", desks=("调度席", "补采席")
    )
    assert sorted(r["status"] for r in results) == [200, 409], results

    water = client.get("/api/water-level-card", headers=headers).json()["items"]
    assert (
        next(x for x in water if x["source"] == "物流源")["restore_success_count"]
        == 1
    )

    appendix = client.get("/api/audit-appendix", headers=headers).json()["items"]
    assert sum(
        1
        for x in appendix
        if x["source"] == "物流源" and x["outcome"] == "成功"
    ) == 1

    supplement = client.get("/api/supplement-list", headers=headers).json()["items"]
    assert sum(
        1
        for x in supplement
        if x["source"] == "物流源" and x["status"] == "成功"
    ) == 1

    catalog = client.get("/api/source-catalog", headers=headers).json()["items"]
    assert (
        next(x for x in catalog if x["source"] == "物流源")["restore_success_count"]
        == 1
    )

    handover = client.get("/api/handover-card", headers=headers).json()
    assert sum(1 for x in handover["restored"] if x["source"] == "物流源") == 1


def test_path_deviation_rejected_and_logged_as_double_success_drift():
    """偏离双成切断卡则拒绝并记双成偏了；甲径乙径不得混用。"""
    client, headers = _client()
    freeze_source(client, headers, "订单源")

    resp = client.post(
        "/api/catchup/订单源",
        headers=headers,
        json={"desk": "调度席", "path": "乙"},
    )
    assert resp.status_code == 409, resp.text

    attempts = client.get("/api/audit-appendix", headers=headers).json()["items"]
    drift = [a for a in attempts if a["outcome"] == "双成偏了"]
    assert len(drift) == 1, attempts
    assert "双成偏了" in drift[0]["reason"]
    # 拒绝必须零改：源仍冻结，补采列表没有成功
    assert _source(client, headers, "订单源")["output_mode"] == "冻结"
    assert not [
        r
        for r in client.get("/api/catchups", headers=headers).json()["items"]
        if r["source"] == "订单源" and r["status"] == "成功"
    ]


def test_cutover_card_sealed_and_immutable():
    """择定后写入双成切断卡并封档；当天不许改口。"""
    client, headers = _client()
    card = client.get("/api/cutover-card", headers=headers).json()
    assert card["path"] == "甲" and card["sealed"] == 1, card
    assert card["sealed_at"] == "2026-11-19"

    import sqlite3

    import app as backend

    with backend.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE cutover_cards SET path='乙' WHERE name='restore-unique-success'")
    # 改口失败后卡仍是甲径
    assert client.get("/api/cutover-card", headers=headers).json()["path"] == "甲"


def test_success_then_restore_again_does_not_add_second_success():
    """成功后源为可写：再点恢复只记双成功争用，不得再造成功账。"""
    client, headers = _client()
    freeze_source(client, headers, "订单源")
    first = client.post("/api/catchup/订单源", headers=headers, json={"desk": "调度席"})
    assert first.status_code == 200, first.text

    again = client.post("/api/catchup/订单源", headers=headers, json={"desk": "补采席"})
    assert again.status_code == 409, again.text
    assert again.json()["outcome"] == "双成功争用"

    successes = [
        a
        for a in client.get("/api/audit-appendix", headers=headers).json()["items"]
        if a["source"] == "订单源" and a["outcome"] == "成功"
    ]
    assert len(successes) == 1


def test_restore_on_never_frozen_source_refuses():
    """源本就未冻结：恢复应「无需恢复」零改，不凭空造一次成功。"""
    client, headers = _client()
    resp = client.post("/api/catchup/订单源", headers=headers, json={"desk": "调度席"})
    assert resp.status_code == 409, resp.text
    assert resp.json()["outcome"] == "无需恢复"
    assert not client.get("/api/catchups", headers=headers).json()["items"]


def test_new_arrival_opens_exactly_one_new_success():
    """唯一成功按事件版本计：再冻结一次后可成功一次，仍不会双成功。"""
    client, headers = _client()
    freeze_source(client, headers, "订单源")
    first = client.post("/api/catchup/订单源", headers=headers, json={"desk": "调度席"})
    assert first.status_code == 200
    again = client.post("/api/catchup/订单源", headers=headers, json={"desk": "补采席"})
    assert again.status_code == 409

    freeze_source(client, headers, "订单源")  # 新事件 → 新版本
    third = client.post("/api/catchup/订单源", headers=headers, json={"desk": "补采席"})
    assert third.status_code == 200, third.text
    fourth = client.post("/api/catchup/订单源", headers=headers, json={"desk": "调度席"})
    assert fourth.status_code == 409

    successes = [
        a
        for a in client.get("/api/audit-appendix", headers=headers).json()["items"]
        if a["source"] == "订单源" and a["outcome"] == "成功"
    ]
    assert len(successes) == 2
    assert len({a["rev"] for a in successes}) == 2  # 每版本各一次，不重复
