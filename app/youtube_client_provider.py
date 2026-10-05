"""Google 연결 정보(OAuth Desktop client)를 어디서 가져올지 정하는 곳.

- 지금(기본): 사용자가 고른 'Google 연결 파일'(데스크톱 앱 JSON) 위치를 채널마다 기억한다 (고급 모드에서도 계속 지원).
- 앞으로(BEGINNER DISTRIBUTION MODE): 배포자가 EXE 옆에 bundled_oauth_client.json을 함께 주면,
  사용자는 파일을 고르지 않고 [Google 계정 연결]만 누르면 된다. 이때 채널에는 BUNDLED_MARKER만 저장된다.
- 이 저장소에는 실제 client 파일을 넣지 않는다 (.gitignore: bundled_oauth_client.json, client_secret*.json).
"""
from __future__ import annotations

import sys
from pathlib import Path

from .youtube_oauth import OAuthClient, OAuthError, load_client_file

BUNDLED_FILE_NAME = "bundled_oauth_client.json"
BUNDLED_MARKER = "bundled:default"


def _search_dirs() -> list[Path]:
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).resolve().parent)
    dirs.append(Path(__file__).resolve().parent.parent)
    return dirs


def bundled_client_path() -> Path | None:
    return next((d / BUNDLED_FILE_NAME for d in _search_dirs() if (d / BUNDLED_FILE_NAME).is_file()), None)


def has_bundled_client() -> bool:
    return bundled_client_path() is not None


def resolve_client(client_file: str) -> OAuthClient:
    """채널에 저장된 값 → OAuthClient. BUNDLED_MARKER면 배포용 기본 client, 아니면 사용자가 고른 파일."""
    if client_file == BUNDLED_MARKER:
        p = bundled_client_path()
        if p is None:
            raise OAuthError("이 프로그램에 기본 Google 연결 정보가 없습니다. Google 연결 파일을 직접 선택하세요.")
        return load_client_file(p)
    return load_client_file(client_file)
