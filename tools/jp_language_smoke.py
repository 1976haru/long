"""Optional real-local-model smoke test. Never downloads a model or contacts YouTube."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.jp_language_provider import OllamaLocalProvider
from app.jp_language_service import JapaneseLanguageService, QWEN_8B


def main() -> int:
    provider = OllamaLocalProvider()
    if not provider.health() or not any(x.split(":")[0] == "qwen3" for x in provider.list_models()):
        print("SKIPPED_NOT_INSTALLED")
        return 0
    model = next(x for x in provider.list_models() if x.split(":")[0] == "qwen3")
    result = JapaneseLanguageService(provider, model=model).analyze("夜に聴くと落ち着きます。")
    print("PASS" if len(result.candidates) == 3 else "FAIL")
    return 0 if len(result.candidates) == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
