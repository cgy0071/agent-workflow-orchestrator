from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
data = root / "data" / "demo"
python = sys.executable
scripts = root / "app" / "scripts"
try:
    if not data.exists():
        print("正在运行离线示例。两个模拟执行者各做一项任务，随后合并结果……", flush=True)
        subprocess.run([python, "-B", scripts / "demo_scheduler.py", "--directory", data], check=True)
    if not (data / "demo-result.json").is_file():
        sys.exit("上一次示例没有完成。关闭窗口，把 data 文件夹改名为 data-old，再双击开始。")
    subprocess.run([python, "-B", scripts / "workflow_control.py", "--state", data / "state",
                    "status", "--summary", "--format", "text"], check=True)
    print("示例完成。A、B、JOIN 三项已验收。下次双击会查看这次结果。", flush=True)
except (OSError, subprocess.CalledProcessError) as exc:
    print("启动失败。请确认已全部解压到可写目录，保留本窗口的错误信息：", exc)
    sys.exit(1)
