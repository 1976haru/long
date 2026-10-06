"""Local language-model provider abstraction and Ollama implementation.

Only loopback Ollama is accepted by default.  No comment text is logged here.
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator


class ProviderError(RuntimeError):
    pass


class ProviderUnavailable(ProviderError):
    pass


class GenerationCancelled(ProviderError):
    pass


@dataclass(frozen=True)
class PullProgress:
    status: str
    completed: int = 0
    total: int = 0

    @property
    def percent(self) -> int:
        return round(self.completed * 100 / self.total) if self.total else 0


class JapaneseLanguageProvider:
    def health(self) -> bool:
        raise NotImplementedError

    def list_models(self) -> list[str]:
        raise NotImplementedError

    def generate_structured(self, *, model: str, messages: list[dict], schema: dict,
                            timeout: float = 90, cancel_event: threading.Event | None = None,
                            keep_alive: str = "5m") -> dict:
        raise NotImplementedError

    def pull_model(self, model: str, *, cancel_event: threading.Event | None = None,
                   progress: Callable[[PullProgress], None] | None = None) -> None:
        raise NotImplementedError


class OllamaLocalProvider(JapaneseLanguageProvider):
    def __init__(self, base_url: str = "http://127.0.0.1:11434", opener=None):
        base = base_url.rstrip("/")
        if base not in ("http://127.0.0.1:11434", "http://localhost:11434"):
            raise ValueError("기본 모드에서는 이 PC의 Ollama만 사용할 수 있습니다.")
        self.base_url = base
        self._open = opener or urllib.request.urlopen

    def _request(self, path: str, payload: dict | None = None, *, timeout: float = 10):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers={"Content-Type": "application/json"},
                                     method="GET" if data is None else "POST")
        try:
            return self._open(req, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
            raise ProviderUnavailable("무료 일본어 AI가 준비되지 않았습니다.") from e

    def health(self) -> bool:
        try:
            with self._request("/api/tags", timeout=2) as response:
                return 200 <= getattr(response, "status", 200) < 300
        except ProviderError:
            return False

    def list_models(self) -> list[str]:
        with self._request("/api/tags", timeout=5) as response:
            value = json.loads(response.read().decode("utf-8"))
        return [str(m.get("name") or m.get("model")) for m in value.get("models", [])
                if m.get("name") or m.get("model")]

    def generate_structured(self, *, model: str, messages: list[dict], schema: dict,
                            timeout: float = 90, cancel_event: threading.Event | None = None,
                            keep_alive: str = "5m") -> dict:
        if cancel_event and cancel_event.is_set():
            raise GenerationCancelled("생성을 취소했습니다.")
        payload = {"model": model, "messages": messages, "stream": False,
                   "format": schema, "keep_alive": keep_alive,
                   "options": {"temperature": 0.35}}
        with self._request("/api/chat", payload, timeout=timeout) as response:
            raw = response.read()
        if cancel_event and cancel_event.is_set():
            raise GenerationCancelled("생성을 취소했습니다.")
        try:
            envelope = json.loads(raw.decode("utf-8"))
            content = envelope["message"]["content"]
            return content if isinstance(content, dict) else json.loads(content)
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ProviderError("로컬 모델이 올바른 JSON 응답을 만들지 못했습니다.") from e

    def pull_model(self, model: str, *, cancel_event: threading.Event | None = None,
                   progress: Callable[[PullProgress], None] | None = None) -> None:
        response = self._request("/api/pull", {"model": model, "stream": True}, timeout=24 * 3600)
        try:
            for line in response:
                if cancel_event and cancel_event.is_set():
                    raise GenerationCancelled("다운로드를 취소했습니다.")
                if not line.strip():
                    continue
                item = json.loads(line.decode("utf-8"))
                if item.get("error"):
                    raise ProviderError(str(item["error"]))
                if progress:
                    progress(PullProgress(str(item.get("status", "")), int(item.get("completed", 0)),
                                          int(item.get("total", 0))))
        finally:
            response.close()

