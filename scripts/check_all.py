# -*- coding: utf-8 -*-
"""DeRT 一键自检：pytest 全套 + 起服务冒烟（影子流量闭环）。

用法（项目根目录）：
    python scripts/check_all.py            # 全部
    python scripts/check_all.py --no-tests # 跳过 pytest，只做冒烟
    python scripts/check_all.py --no-smoke # 只跑 pytest

冒烟内容：临时状态目录起 8640 端口 → GET /tasks → 提交 fake 任务 →
等收尾 → 校验快照（done / tier_usage / shadow）→ 关服务。
退出码 0 = 全过；非 0 = 有失败（stdout 打印明细）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SMOKE_PORT = 8640
SMOKE_BASE = f"http://127.0.0.1:{SMOKE_PORT}"


def run_tests() -> bool:
    print("=" * 60)
    print("[1/2] pytest 全套")
    print("=" * 60)
    r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"],
                       cwd=ROOT)
    ok = r.returncode == 0
    print(f"pytest {'PASS' if ok else 'FAIL'}")
    return ok


def _get(path: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(SMOKE_BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict, timeout: float = 10) -> dict:
    req = urllib.request.Request(
        SMOKE_BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def run_smoke() -> bool:
    print("=" * 60)
    print("[2/2] 服务冒烟（fake provider + 影子流量闭环）")
    print("=" * 60)
    state_dir = tempfile.mkdtemp(prefix="dert_smoke_")
    env = {**os.environ,
           "DERT_SERVICE_STATE_DIR": state_dir,
           "DERT_APPROVAL_TIMEOUT": "2"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "service.app:app",
         "--port", str(SMOKE_PORT), "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # 1) 服务就绪
        deadline = time.time() + 15
        while True:
            try:
                _get("/tasks", timeout=2)
                break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("服务 15s 内未就绪")
                time.sleep(0.4)
        print("  服务就绪 ✓")

        # 2) 任务库
        tasks = _get("/tasks")
        assert tasks and tasks[0]["id"].startswith("HB/"), "任务库 HB 应排最前"
        print(f"  任务库 {len(tasks)} 条（HB 最前）✓")

        # 3) fake 全链路 + 影子流量
        run = _post("/runs", {"task_id": "T1-1", "provider": "fake",
                              "deadline": 10})
        rid = run["run_id"]
        snap = {}
        deadline = time.time() + 15
        while time.time() < deadline:
            snap = _get(f"/runs/{rid}")
            if snap.get("status") in ("done", "error"):
                break
            time.sleep(0.2)
        assert snap.get("status") == "done", f"run 未收尾: {snap.get('status')}"
        assert snap.get("tier_usage") == {"fast": 2}, \
            f"tier_usage 异常: {snap.get('tier_usage')}"
        assert snap.get("shadow", {}).get("verdict") == "ok", \
            f"影子流量未落: {snap.get('shadow')}"
        print(f"  fake 全链路 ✓  shadow={snap['shadow']}")

        # 4) 难度史文件
        hist_file = Path(state_dir) / "difficulty_history.json"
        hist = json.loads(hist_file.read_text(encoding="utf-8"))
        assert hist.get("T1-1", {}).get("ok", 0) >= 1, "难度史未写 T1-1"
        print("  难度史落盘 ✓")

        # 5) 静态页与图床
        with urllib.request.urlopen(SMOKE_BASE + "/", timeout=5) as resp:
            assert resp.status == 200
        print("  UI 静态页 ✓")
        print("冒烟 PASS")
        return True
    except Exception as exc:
        print(f"冒烟 FAIL: {type(exc).__name__}: {exc}")
        return False
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser(description="DeRT 一键自检")
    ap.add_argument("--no-tests", action="store_true", help="跳过 pytest")
    ap.add_argument("--no-smoke", action="store_true", help="跳过服务冒烟")
    args = ap.parse_args()
    t0 = time.time()
    results = []
    if not args.no_tests:
        results.append(("pytest", run_tests()))
    if not args.no_smoke:
        results.append(("smoke", run_smoke()))
    print("=" * 60)
    for name, ok in results:
        print(f"  {name:8s} {'✅ PASS' if ok else '❌ FAIL'}")
    print(f"总耗时 {time.time() - t0:.1f}s —— "
          f"{'全部通过' if all(ok for _, ok in results) else '存在失败'}")
    sys.exit(0 if all(ok for _, ok in results) else 1)


if __name__ == "__main__":
    main()
