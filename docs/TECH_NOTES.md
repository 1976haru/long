# 기술 선택 메모

## 조사한 방향

GitHub의 대표적인 Python/FFmpeg 계열 프로젝트를 비교했다.

- `kkroening/ffmpeg-python`: FFmpeg 명령 그래프를 Python API로 구성하는 래퍼
- `PyAV-Org/PyAV`: FFmpeg 라이브러리를 프레임/패킷 수준으로 다루는 Python 바인딩
- `rpmfusion/python-ffmpeg-progress-yield`: FFmpeg `-progress` 출력을 이용한 진행률 처리 아이디어

## v0.3 선택

이 프로그램은 영상 편집기가 아니라 **동일 규격 MP4를 무손실로 반복 연결**하는 것이 목적이다. 그래서 런타임 Python 영상 라이브러리를 추가하지 않고 직접 `subprocess`로 FFmpeg를 호출한다.

사용하는 핵심 방식:

- FFmpeg concat demuxer
- `-c copy`
- `-progress pipe:1`
- ffprobe 사전/사후 검사
- 대기열 순차 실행

장점:

1. Python이 영상 프레임을 메모리에 올리지 않는다.
2. 디코딩/재인코딩이 없어 CPU/GPU와 메모리 부담이 낮다.
3. 재인코딩에 따른 화질·음질 세대 손실이 없다.
4. 설치 의존성이 적다.
5. Codex/Claude Code가 실제 FFmpeg 명령을 추적하고 디버깅하기 쉽다.

## 향후 원칙

새 라이브러리는 기능상 명확한 이점이 있을 때만 추가한다. 편의를 위해 PyAV/ffmpeg-python을 도입해 현재의 저메모리·무손실 원칙을 약화시키지 않는다.
