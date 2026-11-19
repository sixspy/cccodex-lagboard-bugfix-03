"""双开都成功的夹具：两名值班并行恢复同一源。

这个模块只做并发夹具，不碰业务实现，供复现脚本与失败测共用。
"""

import threading


def _json_or_text(resp):
    try:
        return resp.json()
    except Exception:
        return {"text": resp.text}


def freeze_source(client, headers, source, lag_minutes=90):
    """用一条严重滞后到达把源打成冻结，作为恢复前状态。"""
    return client.post(
        "/api/arrivals",
        headers=headers,
        json={
            "source": source,
            "source_clock": "2026-11-17T00:00:00",
            "ingest_clock": f"2026-11-17T{lag_minutes // 60:02d}:{lag_minutes % 60:02d}:00",
        },
    )


def fire_two_restores(client, headers, source, desks=("调度席", "补采席"), path=None):
    """两名值班同时提交同一源恢复，返回两边的 HTTP 结果。

    用 Barrier 保证两边尽量同时发出；即使服务端把请求排队串行，
    也要由落库的锁与唯一成功口径保证只有一次成功。
    """
    barrier = threading.Barrier(len(desks))
    results = []
    guard = threading.Lock()

    def worker(desk):
        payload = {"desk": desk}
        if path is not None:
            payload["path"] = path
        try:
            barrier.wait(timeout=10)
            resp = client.post(f"/api/catchup/{source}", headers=headers, json=payload)
            item = {
                "desk": desk,
                "status": resp.status_code,
                "body": _json_or_text(resp),
            }
        except Exception as exc:  # 让测试看到线程内异常，而不是静默丢结果
            item = {"desk": desk, "error": repr(exc)}
        with guard:
            results.append(item)

    threads = [threading.Thread(target=worker, args=(desk,)) for desk in desks]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    if len(results) != len(desks):
        raise AssertionError(f"并发恢复未全部返回: {results!r}")
    return results
