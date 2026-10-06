"""Optional real Qwen + local Fake YouTube integration smoke. Never contacts real YouTube."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from app.jp_language_provider import OllamaLocalProvider
from app.jp_language_service import JapaneseLanguageService
from app.jp_youtube_workflow import ExternalJapaneseCommentService, ExternalVideo, JapaneseCommentHistory
from app.youtube_accounts import ChannelProfile
from app.youtube_api import YouTubeApiClient
from youtube_fakes import FakeYouTube


class Profiles:
    def __init__(self, profile): self.profile = profile
    def all(self): return [self.profile]
    def get(self, profile_id): return self.profile if profile_id == self.profile.profile_id else None


def main() -> int:
    provider = OllamaLocalProvider()
    models = provider.list_models() if provider.health() else []
    model = next((x for x in models if x.split(":")[0] == "qwen3"), "")
    if not model:
        print("SKIPPED_NOT_INSTALLED")
        return 0
    language = JapaneseLanguageService(provider, model=model)
    own = language.analyze("夜に聴くと落ち着きます。", "들어줘서 고맙다고 답해줘")
    external = language.external(title="Night Mix", channel="Fake Channel", memo_ko="밤에 듣기 편안했다", genre="NIGHT")
    fake = FakeYouTube()
    try:
        channel = {"id": "UCjp000000000000000000JP", "title": "CHILI LAB"}; token = "fake-local-token"
        profile = ChannelProfile("abcdef123456", "JP", channel_id=channel["id"], channel_title=channel["title"], language="ja", timezone="Asia/Tokyo")
        fake.valid_tokens.add(token); fake.token_channels[token] = channel
        vid = "AbCdEf12345"
        fake.videos[vid] = {"id": vid, "snippet": {"title": "Night Mix", "channelTitle": "Fake Channel",
            "channelId": "UCtarget00000000000001", "description": "", "categoryId": "10"},
            "status": {"privacyStatus": "public"}, "channel": "UCtarget00000000000001"}
        api_factory = lambda p, s: YouTubeApiClient(lambda force_refresh=False: token, base_url=fake.api_base, sleep=lambda _: None)
        with tempfile.TemporaryDirectory() as td:
            flow = ExternalJapaneseCommentService(Profiles(profile), api_factory=api_factory,
                history=JapaneseCommentHistory(Path(td) / "history.json"))
            video = ExternalVideo(vid, "Night Mix", "Fake Channel", "UCtarget00000000000001")
            rec = flow.post_confirmed(profile.profile_id, video, external.candidates[0]["ja"], confirmed=True)
        ok = len(own.candidates) == 3 and len(external.candidates) == 3 and bool(rec.comment_id)
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        fake.close()


if __name__ == "__main__":
    raise SystemExit(main())
