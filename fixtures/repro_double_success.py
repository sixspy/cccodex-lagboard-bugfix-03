"""复现“双开都成功”：两名值班并行恢复同一源。

用法：
    PYTHONPATH=backend /tmp/venv/bin/python fixtures/repro_double_success.py

修复前应看到两条 200、两条成功审计；修复后应看到一条 200、一条 409，
且成功审计只有一条。
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "fixtures"))

import app as backend  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from double_restore_fixture import fire_two_restores, freeze_source  # noqa: E402


def main():
    tmp = tempfile.TemporaryDirectory()
    backend.DB_PATH = Path(tmp.name) / "repro.db"
    backend.init_db()
    client = TestClient(backend.app)
    token = client.post(
        "/api/login", json={"username": "admin", "password": "123456"}
    ).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    freeze_source(client, headers, "订单源")
    results = fire_two_restores(
        client, headers, "订单源", desks=("调度席", "补采席")
    )
    catchups = client.get("/api/catchups", headers=headers).json()["items"]
    successes = [
        r for r in catchups if r["source"] == "订单源" and r["status"] == "成功"
    ]
    source = next(
        s
        for s in client.get("/api/sources", headers=headers).json()["items"]
        if s["name"] == "订单源"
    )

    print("并行恢复返回：")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"成功审计条数：{len(successes)}")
    print(f"源当前输出态：{source['output_mode']}")
    print(
        {
            "双开都成功": len([r for r in results if r["status"] == 200]) == 2,
            "成功审计条数": len(successes),
        }
    )


if __name__ == "__main__":
    main()
