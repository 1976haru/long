from __future__ import annotations

import io
import json
import threading

import pytest

from app.jp_language_provider import GenerationCancelled, OllamaLocalProvider
from app.jp_language_quality import inspect_candidates
from app.jp_language_service import DraftHistory, JapaneseLanguageService


GOOD = {"translation_ko": "밤에 들으면 왠지 울 것 같아", "nuance_ko": "부드럽고 감성적인 혼잣말 느낌입니다.",
        "tone": "감성적", "contains_question": False, "review_required": False,
        "candidates": [{"style": "natural", "ja": "夜に聴くと、また違って感じるよね。"},
                       {"style": "warm", "ja": "夜に聴くと、なんだか胸にくるよね。"},
                       {"style": "casual", "ja": "わかる。夜だと少し違って聴こえるよね。"}]}
REVIEW = {"naturalness_score": 92, "translationese_flags": [], "tone_flags": [], "meaning_reversed": False}


class FakeProvider:
    def __init__(self, values=None): self.values = list(values or [GOOD, REVIEW]); self.calls = []
    def health(self): return True
    def list_models(self): return ["qwen3:8b"]
    def generate_structured(self, **kwargs): self.calls.append(kwargs); return self.values.pop(0)


def test_jako_nuance_and_three_candidates():
    result = JapaneseLanguageService(FakeProvider()).analyze("この曲、夜に聴くとなんか泣きそうになる")
    assert result.translation_ko and result.nuance_ko
    assert len(result.candidates) == 3 and not result.review_required


def test_optional_translation_model_is_used_for_translation():
    translated = {"translation_ko": "자연 번역", "nuance_ko": "뉘앙스"}
    fake = FakeProvider([translated, GOOD, REVIEW])
    result = JapaneseLanguageService(fake, translation_model="translategemma:4b").analyze("いいね")
    assert fake.calls[0]["model"] == "translategemma:4b" and result.translation_ko == "자연 번역"


@pytest.mark.parametrize(("text", "flag"), [
    ("정말 좋아요", "korean_leakage"), ("https://example.com", "url"),
    ("私は日本人です", "identity_claim"), ("私のチャンネルも見てください", "promotion"),
    ("最高😀😀", "too_many_emoji"), ("最高!!!!!", "excessive_emphasis"),
    ("誠にありがとうございます", "excessive_keigo")])
def test_quality_guards(text, flag):
    assert any(x.endswith(flag) for x in inspect_candidates([text, "いい曲ですね。", "また聴きたいです."]))


def test_candidate_dedup():
    assert "candidate_dedup" in inspect_candidates(["いいね！", "いいね。", "いいね"])


def test_history_uses_hash_by_default(tmp_path):
    h = DraftHistory(tmp_path / "history.json")
    h.add(video_url="https://youtu.be/x", channel_title="channel", draft="素敵です", tone="natural")
    assert h.contains("素敵です")
    assert "素敵です" not in (tmp_path / "history.json").read_text(encoding="utf-8")


class Response(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


def test_provider_health_models_and_structured_json():
    replies = [Response(b'{}'), Response(json.dumps({"models": [{"name": "qwen3:8b"}]}).encode()),
               Response(json.dumps({"message": {"content": json.dumps(GOOD)}}).encode())]
    p = OllamaLocalProvider(opener=lambda req, timeout: replies.pop(0))
    assert p.health() and p.list_models() == ["qwen3:8b"]
    assert p.generate_structured(model="qwen3:8b", messages=[], schema={})["tone"] == "감성적"


def test_cancel_before_generation():
    event = threading.Event(); event.set()
    with pytest.raises(GenerationCancelled):
        OllamaLocalProvider(opener=lambda *a, **k: None).generate_structured(
            model="qwen3:8b", messages=[], schema={}, cancel_event=event)


def test_loopback_only():
    with pytest.raises(ValueError): OllamaLocalProvider("http://192.168.0.2:11434")


def test_dataset_has_at_least_40_original_examples():
    from pathlib import Path
    data = json.loads((Path(__file__).parent / "data" / "jp_quality_samples.json").read_text(encoding="utf-8"))
    assert len(data) >= 40
    assert {"thanks", "question", "complaint", "emoji", "ambiguous"} <= {x["category"] for x in data}
