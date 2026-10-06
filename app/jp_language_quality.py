"""Deterministic safety and style checks for generated Japanese drafts."""
from __future__ import annotations

import re

IDENTITY_CLAIMS = ("私は日本人です", "日本人として", "日本在住なので", "東京に住んでいます", "同じ日本人として")
PROMO = ("私のチャンネルも見て", "登録してください", "遊びに来て", "相互登録", "チャンネル登録")
BOT_PHRASES = ("素晴らしいコンテンツをありがとうございます", "貴重な動画をありがとうございます")
HONORIFIC = ("拝見させていただき", "誠にありがとうございます", "存じます", "幸甚")
HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")
URL = re.compile(r"(?:https?://|www\.)", re.I)
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def normalize(text: str) -> str:
    return re.sub(r"[\s。、！？!?.〜~]+", "", text).lower()


def inspect_candidate(text: str) -> list[str]:
    flags = []
    if HANGUL.search(text): flags.append("korean_leakage")
    if URL.search(text): flags.append("url")
    if any(x in text for x in PROMO): flags.append("promotion")
    if any(x in text for x in IDENTITY_CLAIMS): flags.append("identity_claim")
    if len(text) > 220 or len([x for x in re.split(r"[。！？!?]+", text) if x.strip()]) > 3: flags.append("too_long")
    if len(EMOJI.findall(text)) > 1: flags.append("too_many_emoji")
    if "!!!" in text or "！！！" in text or "wwwww" in text.lower(): flags.append("excessive_emphasis")
    if any(x in text for x in HONORIFIC): flags.append("excessive_keigo")
    if any(x in text for x in BOT_PHRASES): flags.append("generic_bot_phrase")
    return flags


def inspect_candidates(candidates: list[str]) -> list[str]:
    flags = []
    for i, text in enumerate(candidates):
        flags.extend(f"candidate_{i + 1}:{x}" for x in inspect_candidate(text))
    values = [normalize(x) for x in candidates]
    if len(values) != 3 or len(set(values)) != len(values):
        flags.append("candidate_dedup")
    return flags

