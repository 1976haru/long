from __future__ import annotations

import os
from pathlib import Path
import sys
from tkinter import messagebox

# 친구 테스트판은 본인용 설정과 완전히 다른 AppData 폴더를 사용한다.
os.environ["PLAYLIST_STUDIO_FRIEND_TEST"] = "1"
os.environ["PLAYLIST_STUDIO_SETTINGS_DIR"] = "PlaylistLongVideoMaker_FriendTest"

from app.ui import MainWindow


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def show_start_guide(app: MainWindow) -> None:
    messagebox.showinfo(
        "친구 테스트판 — 시작 순서",
        "이 버전은 ① 영상 늘리기와 ② 실시간 스트리밍만 테스트합니다.\n\n"
        "1. 먼저 ① 영상 늘리기에서 TEST_SAMPLE_10SEC.mp4를 2회로 만들어 보세요.\n"
        "2. 다음 ② 실시간 스트리밍 → '내 PC'로 테스트하세요.\n"
        "3. YouTube에서는 처음에 비공개 또는 일부공개 LIVE를 권장합니다.\n"
        "4. Cloud LIVE는 PC LIVE가 정상인 것을 확인한 뒤 테스트하세요.\n\n"
        "개인 Stream Key, SSH Key, 서버 주소, YouTube 계정 정보는 이 배포판에 포함되어 있지 않습니다."
    )


if __name__ == "__main__":
    app = MainWindow(app_dir())
    app.after(900, lambda: show_start_guide(app))
    app.mainloop()
