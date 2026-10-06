"""Generate an offline HTML sheet for human A/B/C tone review using an installed local model."""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.jp_language_provider import OllamaLocalProvider
from app.jp_language_service import JapaneseLanguageService


def main() -> int:
    provider = OllamaLocalProvider()
    models = provider.list_models() if provider.health() else []
    qwen = next((x for x in models if x.split(":")[0] == "qwen3"), "")
    if not qwen:
        print("SKIPPED_NOT_INSTALLED")
        return 0
    samples = json.loads((Path(__file__).parents[1] / "tests/data/jp_quality_samples.json").read_text(encoding="utf-8"))[:20]
    service = JapaneseLanguageService(provider, model=qwen)
    rows = []
    for sample in samples:
        result = service.analyze(sample["ja"])
        drafts = "<br>".join(html.escape(x["ja"]) for x in result.candidates)
        rows.append(f"<tr><td>{html.escape(sample['ja'])}</td><td>{html.escape(result.translation_ko)}</td>"
                    f"<td>{html.escape(result.nuance_ko)}</td>"
                    f"<td>{drafts}</td><td>{html.escape(', '.join(result.quality_flags))}</td>"
                    "<td><select class=grade><option></option><option>A</option><option>B</option><option>C</option></select></td></tr>")
    out = Path("jp_tone_review.html")
    out.write_text("<!doctype html><meta charset=utf-8><title>JP tone review</title>"
                   "<h1>JP 2030s Female Natural 수동 평가</h1><p>A 자연스러움 · B 약간 어색 · C 재작성 필요</p>"
                   "<p>추가 체크: Identity claim 0 · 과장 0 · 홍보 0 · 한국어 잔존 0 · 사실 지어내기 0</p>"
                   "<table border=1 cellpadding=6><tr><th>원문</th><th>한국어 번역</th><th>뉘앙스</th><th>답글 후보 1/2/3</th><th>flags</th><th>평가</th></tr>"
                   + "".join(rows) + "</table><p>선택 결과는 이 브라우저에 자동 저장됩니다.</p>"
                   "<script>document.querySelectorAll('.grade').forEach((x,i)=>{x.value=localStorage['jp-grade-'+i]||'';x.onchange=()=>localStorage['jp-grade-'+i]=x.value})</script>", encoding="utf-8")
    print(out.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
