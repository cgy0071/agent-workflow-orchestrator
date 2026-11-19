from pathlib import Path
import sqlite3
import subprocess
import sys

from workflow_scheduler import snapshot


def open_demo(data, launch):
    fresh = not data.exists()
    if fresh:
        launch(data)
    view = snapshot(data / "state")
    expected = {"A", "B", "JOIN"}
    nodes = view["dags"].get("demo", {}).get("nodes", {})
    tasks = {item["task_id"]: item for item in view["tasks"]}
    if (set(nodes) != expected or set(tasks) != expected or
            any(nodes[key] != "accepted" or not tasks[key]["acceptance_valid"] for key in expected)):
        raise ValueError("台账中没有三项有效验收记录。请保留 data 目录排查。")
    return fresh, tasks["A"]["registered_at"]


def main():
    root = Path(__file__).resolve().parents[1]

    def launch(data):
        print("正在运行离线示例……", flush=True)
        subprocess.run([sys.executable, "-B", root / "app/scripts/demo_scheduler.py",
                        "--directory", data], check=True)

    try:
        fresh, started = open_demo(root / "data/demo", launch)
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print("无法确认示例结果。请保留本窗口的错误信息：", exc)
        return 1
    print("本次运行已完成。" if fresh else "正在查看上次运行；没有重新派发任务。")
    print("A、B、JOIN 三项已验收。首次任务登记时间：", started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
