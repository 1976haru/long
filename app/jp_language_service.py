"""Application service for local-only Japanese translation and writing."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .jp_language_prompts import (CANDIDATE_SCHEMA, REVIEW_SCHEMA, TRANSLATION_SCHEMA, analysis_messages,
                                  external_messages, review_messages)
from .jp_language_provider import JapaneseLanguageProvider, ProviderError
from .jp_language_quality import inspect_candidates
from .settings import settings_dir

QWEN_8B = "qwen3:8b"
QWEN_4B = "qwen3:4b"
TRANSLATE_4B = "translategemma:4b"
MODEL_CHOICES = {QWEN_8B: ("추천 · 고품질", "약 5.2GB"), QWEN_4B: ("가벼운 버전", "약 2.5GB")}


@dataclass
class JapaneseResult:
    translation_ko: str
    nuance_ko: str
    tone: str
    contains_question: bool
    candidates: list[dict]
    review_required: bool
    quality_flags: list[str]


def _validate(value: dict) -> None:
    required = {"translation_ko", "nuance_ko", "tone", "contains_question", "candidates", "review_required"}
    if not isinstance(value, dict) or not required <= value.keys():
        raise ProviderError("로컬 모델 응답에 필요한 항목이 없습니다.")
    if not isinstance(value["candidates"], list) or len(value["candidates"]) != 3:
        raise ProviderError("답글 후보는 정확히 3개여야 합니다.")
    for item in value["candidates"]:
        if not isinstance(item, dict) or not isinstance(item.get("style"), str) or not isinstance(item.get("ja"), str):
            raise ProviderError("답글 후보 형식이 올바르지 않습니다.")
    if not all(isinstance(value[k], str) for k in ("translation_ko", "nuance_ko", "tone")):
        raise ProviderError("번역 응답 형식이 올바르지 않습니다.")
    if not isinstance(value["contains_question"], bool) or not isinstance(value["review_required"], bool):
        raise ProviderError("검토 상태 형식이 올바르지 않습니다.")


class DraftHistory:
    def __init__(self, path: Path | None = None, *, store_text: bool = False):
        self.path = path or settings_dir() / "jp_draft_history.json"
        self.store_text = store_text

    @staticmethod
    def digest(text: str) -> str:
        return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

    def load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def contains(self, text: str) -> bool:
        key = self.digest(text)
        return any(x.get("draft_hash") == key for x in self.load())

    def add(self, *, video_url: str, channel_title: str, draft: str, tone: str) -> None:
        rows = self.load()
        row = {"timestamp": time.time(), "video_url_hash": self.digest(video_url) if video_url else "",
               "channel_title": channel_title[:120], "draft_hash": self.digest(draft), "tone": tone}
        if self.store_text: row["draft"] = draft
        rows = (rows + [row])[-100:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class JapaneseLanguageService:
    def __init__(self, provider: JapaneseLanguageProvider, *, model: str = QWEN_8B,
                 translation_model: str = "", keep_loaded: bool = False,
                 history: DraftHistory | None = None, timeout: float = 90):
        self.provider, self.model, self.translation_model = provider, model, translation_model
        self.keep_alive = "30m" if keep_loaded else "5m"
        self.history, self.timeout = history or DraftHistory(), timeout

    def _generate(self, messages: list[dict], cancel_event=None) -> dict:
        error = None
        for _ in range(2):  # schema/JSON failure: one retry only
            try:
                value = self.provider.generate_structured(model=self.model, messages=messages,
                    schema=CANDIDATE_SCHEMA, timeout=self.timeout, cancel_event=cancel_event, keep_alive=self.keep_alive)
                _validate(value)
                return value
            except ProviderError as e:
                error = e
        raise error or ProviderError("생성하지 못했습니다.")

    def analyze(self, comment_ja: str, intent_ko: str = "", genre: str = "", cancel_event=None) -> JapaneseResult:
        if not comment_ja.strip(): raise ValueError("일본어 댓글을 입력하세요.")
        translated = None
        if self.translation_model:
            translated = self.provider.generate_structured(model=self.translation_model,
                messages=[{"role": "system", "content": "Translate Japanese naturally to Korean and explain nuance. JSON only."},
                          {"role": "user", "content": comment_ja}], schema=TRANSLATION_SCHEMA,
                timeout=self.timeout, cancel_event=cancel_event, keep_alive=self.keep_alive)
        result = self._quality_loop(analysis_messages(comment_ja, intent_ko, genre), comment_ja, cancel_event)
        if any(x in comment_ja for x in ("?", "？", "嫌", "困", "残念", "聞きづら", "わからない")):
            result.review_required = True
        if isinstance(translated, dict) and isinstance(translated.get("translation_ko"), str) and isinstance(translated.get("nuance_ko"), str):
            result.translation_ko, result.nuance_ko = translated["translation_ko"], translated["nuance_ko"]
        return result

    def external(self, *, title: str, channel: str, memo_ko: str, genre: str = "", cancel_event=None) -> JapaneseResult:
        if not any((title.strip(), channel.strip(), memo_ko.strip())):
            raise ValueError("짧게 감상을 적어주세요.")
        source = f"{title}\n{channel}\n{memo_ko}"
        messages = external_messages(title=title, channel=channel, memo_ko=memo_ko, genre=genre)
        result = self._quality_loop(messages, source, cancel_event)
        duplicates = [x["ja"] for x in result.candidates if self.history.contains(x["ja"])]
        if duplicates:
            messages += [{"role": "assistant", "content": json.dumps([x["ja"] for x in result.candidates], ensure_ascii=False)},
                         {"role": "user", "content": "These drafts repeat recent wording. Create three clearly different drafts."}]
            result = self._quality_loop(messages, source, cancel_event)
            if any(self.history.contains(x["ja"]) for x in result.candidates):
                result.quality_flags.append("history_duplicate")
                result.review_required = True
        return result

    def _quality_loop(self, messages, source, cancel_event) -> JapaneseResult:
        value = self._generate(messages, cancel_event)
        flags: list[str] = []
        for attempt in range(3):
            drafts = [x["ja"].strip() for x in value["candidates"]]
            flags = inspect_candidates(drafts)
            review = self.provider.generate_structured(model=self.model, messages=review_messages(source, drafts),
                schema=REVIEW_SCHEMA, timeout=self.timeout, cancel_event=cancel_event, keep_alive=self.keep_alive)
            score = int(review.get("naturalness_score", 0)) if isinstance(review, dict) else 0
            model_flags = list(review.get("translationese_flags", [])) + list(review.get("tone_flags", []))
            if review.get("meaning_reversed"): model_flags.append("meaning_reversed")
            flags += [str(x) for x in model_flags]
            if not flags and score >= 85: break
            if attempt >= 2: value["review_required"] = True; break
            messages = messages + [{"role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
                                   {"role": "user", "content": "Rewrite all three drafts. Fix: " + ", ".join(flags)}]
            value = self._generate(messages, cancel_event)
        return JapaneseResult(value["translation_ko"], value["nuance_ko"], value["tone"],
                              value["contains_question"], value["candidates"],
                              bool(value["review_required"] or flags), flags)
