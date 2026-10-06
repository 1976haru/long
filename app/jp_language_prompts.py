"""Prompts and JSON schemas for the local Japanese assistant."""

PROFILE = "JP 2030s Female Natural"
GENRES = ("CHILL / R&B", "LOVE STORY", "INDIE", "CAFE", "BALLAD", "NIGHT", "JAZZ")

CANDIDATE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["translation_ko", "nuance_ko", "tone", "contains_question", "candidates", "review_required"],
    "properties": {
        "translation_ko": {"type": "string"}, "nuance_ko": {"type": "string"},
        "tone": {"type": "string"}, "contains_question": {"type": "boolean"},
        "review_required": {"type": "boolean"},
        "candidates": {"type": "array", "minItems": 3, "maxItems": 3,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["style", "ja"],
                      "properties": {"style": {"type": "string"}, "ja": {"type": "string"}}}},
    },
}

REVIEW_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["naturalness_score", "translationese_flags", "tone_flags", "meaning_reversed"],
                 "properties": {"naturalness_score": {"type": "integer", "minimum": 0, "maximum": 100},
                                "translationese_flags": {"type": "array", "items": {"type": "string"}},
                                "tone_flags": {"type": "array", "items": {"type": "string"}},
                                "meaning_reversed": {"type": "boolean"}}}

TRANSLATION_SCHEMA = {"type": "object", "additionalProperties": False,
                      "required": ["translation_ko", "nuance_ko"],
                      "properties": {"translation_ko": {"type": "string"},
                                     "nuance_ko": {"type": "string"}}}

SYSTEM = """You are a local Japanese writing assistant. Return only data matching the JSON schema.
Translate Japanese into natural Korean and briefly explain nuance in Korean. Create exactly three distinct Japanese drafts.
Style profile JP 2030s Female Natural means only a writing style, never the user's identity: natural and short (1-3 sentences),
not business-like, not excessive slang/kawaii/gyaru, 0-1 emoji, no promotional language or URLs.
Never claim 私は日本人です, 日本人として, 日本在住なので, 東京に住んでいます, 同じ日本人として.
Do not invent details, timestamps, instruments, scenes, or experiences absent from the supplied text.
Questions, complaints, sensitive or unclear comments must set review_required=true."""


def analysis_messages(comment_ja: str, intent_ko: str = "", genre: str = "") -> list[dict]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content":
            f"Japanese comment:\n{comment_ja}\nKorean reply intent (may be empty):\n{intent_ko}\nGenre: {genre or 'none'}"}]


def external_messages(*, title: str, channel: str, memo_ko: str, genre: str) -> list[dict]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content":
            "Write three non-promotional comments for someone else's YouTube video. Base concrete claims only on the memo.\n"
            f"Title: {title}\nChannel: {channel}\nViewer memo in Korean: {memo_ko or '(empty)'}\nGenre: {genre}"}]


def review_messages(source: str, candidates: list[str]) -> list[dict]:
    return [{"role": "system", "content": "Judge natural Japanese YouTube style and semantic fidelity. Return schema JSON only."},
            {"role": "user", "content": f"Source/context:\n{source}\nDrafts:\n" + "\n".join(candidates)}]
