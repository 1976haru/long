from pathlib import Path
import sys

from app.ui import MainWindow


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


if __name__ == "__main__":
    app = MainWindow(app_dir())
    app.mainloop()
