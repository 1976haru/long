from pathlib import Path
import sys

from app.ui import MainWindow


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


if __name__ == "__main__":
    app = MainWindow(app_dir())
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":  # 배포 EXE 화면 자가 점검 (네트워크 없음)
        from app.self_test import run
        run(app, sys.argv[2])
    app.mainloop()
