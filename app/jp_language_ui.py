"""Tkinter UI for the local Japanese assistant (no external posting)."""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from urllib.parse import urlparse

from .jp_language_prompts import GENRES, PROFILE
from .jp_language_provider import GenerationCancelled, OllamaLocalProvider
from .jp_language_service import JapaneseLanguageService, MODEL_CHOICES, QWEN_4B, QWEN_8B
from .settings import load_settings, update_settings
from .ui_theme import ensure as ensure_theme
from .youtube_accounts import ProfileStore
from .jp_youtube_workflow import (ExternalJapaneseCommentService, ExternalVideo,
                                  JapaneseCommentHistory, parse_youtube_video_id)

LOCAL_NOTE = "이 문장은 내 PC의 무료 AI로 처리됩니다. 별도의 유료 AI API 키가 필요하지 않습니다."
OLLAMA_URL = "https://ollama.com/download/windows"


def language_settings() -> dict:
    return dict(load_settings().get("japanese_assistant") or {})


def make_service() -> JapaneseLanguageService:
    cfg = language_settings()
    return JapaneseLanguageService(OllamaLocalProvider(), model=cfg.get("model", QWEN_8B),
        translation_model=cfg.get("translation_model", ""), keep_loaded=bool(cfg.get("keep_loaded", False)))


class JapaneseSetupWizard(tk.Toplevel):
    def __init__(self, master, *, provider=None):
        super().__init__(master); ensure_theme(self)
        self.title("무료 일본어 도우미 설정")
        self.geometry("610x430")
        self.provider = provider or OllamaLocalProvider()
        cfg = language_settings()
        self.model = tk.StringVar(value=cfg.get("model", QWEN_8B))
        self.optional_translate = tk.BooleanVar(value=bool(cfg.get("translation_model")))
        self.keep_loaded = tk.BooleanVar(value=bool(cfg.get("keep_loaded", False)))
        self.step = 0; self.cancel_event = threading.Event(); self._q = queue.Queue()
        self.body = ttk.Frame(self, padding=16); self.body.pack(fill="both", expand=True)
        nav = ttk.Frame(self, padding=10); nav.pack(fill="x")
        ttk.Button(nav, text="이전", command=lambda: self.show(self.step - 1)).pack(side="left")
        ttk.Button(nav, text="다음", command=lambda: self.show(self.step + 1)).pack(side="right")
        self.show(0); self.after(200, self._pump)

    def show(self, step):
        self.step = max(0, min(2, step))
        for x in self.body.winfo_children(): x.destroy()
        ttk.Label(self.body, text=f"STEP {self.step + 1} / 3", font="PLS.Title").pack(anchor="w")
        if self.step == 0:
            ttk.Label(self.body, text="무료 AI 준비 확인\n\n" + LOCAL_NOTE, wraplength=560, justify="left").pack(anchor="w", pady=10)
            self.status = tk.StringVar(value="확인 전")
            ttk.Label(self.body, textvariable=self.status).pack(anchor="w")
            row = ttk.Frame(self.body); row.pack(anchor="w", pady=12)
            ttk.Button(row, text="설치 후 다시 확인", command=self.check).pack(side="left")
            ttk.Button(row, text="Ollama 설치 페이지 열기", command=lambda: webbrowser.open(OLLAMA_URL)).pack(side="left", padx=6)
            ttk.Button(row, text="나중에", command=self.destroy).pack(side="left")
        elif self.step == 1:
            ttk.Label(self.body, text="모델 선택", font="PLS.Strong").pack(anchor="w", pady=8)
            for model, (label, size) in MODEL_CHOICES.items():
                ttk.Radiobutton(self.body, text=f"{label}  Qwen3 {model.split(':')[1].upper()}  · 다운로드 {size}",
                                variable=self.model, value=model).pack(anchor="w", pady=4)
            ttk.Checkbutton(self.body, text="고급: 번역 정확도 강화 모델 사용 (TranslateGemma 4B · 약 3.3GB 추가)",
                            variable=self.optional_translate).pack(anchor="w", pady=(15, 2))
            ttk.Checkbutton(self.body, text="고급: 일본어 모델을 계속 메모리에 유지", variable=self.keep_loaded).pack(anchor="w")
            self.pull_status = tk.StringVar()
            ttk.Label(self.body, textvariable=self.pull_status).pack(anchor="w", pady=8)
            row = ttk.Frame(self.body); row.pack(anchor="w")
            ttk.Button(row, text="다운로드 시작", command=self.pull).pack(side="left")
            ttk.Button(row, text="생성 취소", command=self.cancel_event.set).pack(side="left", padx=6)
        else:
            ttk.Label(self.body, text="테스트", font="PLS.Strong").pack(anchor="w", pady=8)
            self.test_status = tk.StringVar(value="[모델 상태 확인]을 눌러 준비 상태를 확인하세요.")
            ttk.Label(self.body, textvariable=self.test_status, wraplength=560).pack(anchor="w")
            ttk.Button(self.body, text="모델 상태 확인", command=self.check_models).pack(anchor="w", pady=10)
            ttk.Button(self.body, text="설정 저장", command=self.save).pack(anchor="w")

    def check(self):
        self.status.set("확인 중…")
        threading.Thread(target=lambda: self._q.put(("health", self.provider.health())), daemon=True).start()

    def check_models(self):
        def work():
            try: self._q.put(("models", self.provider.list_models()))
            except Exception as e: self._q.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def pull(self):
        if not messagebox.askyesno("모델 다운로드", f"{self.model.get()} 모델을 다운로드할까요?\n큰 파일을 받습니다.", parent=self): return
        self.cancel_event.clear()
        def work():
            try:
                models = [self.model.get()]
                if self.optional_translate.get(): models.append("translategemma:4b")
                for model in models:
                    self._q.put(("pull_label", model))
                    self.provider.pull_model(model, cancel_event=self.cancel_event,
                        progress=lambda p: self._q.put(("pull", p)))
                self._q.put(("pull_done", None))
            except Exception as e: self._q.put(("error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def save(self):
        update_settings(japanese_assistant={"model": self.model.get(),
            "translation_model": "translategemma:4b" if self.optional_translate.get() else "",
            "keep_loaded": bool(self.keep_loaded.get())})
        self.destroy()

    def _pump(self):
        try:
            while True:
                tag, value = self._q.get_nowait()
                if tag == "health": self.status.set("✓ Ollama 준비됨" if value else "무료 일본어 AI 프로그램이 아직 설치되지 않았습니다.")
                elif tag == "models": self.test_status.set("현재: " + (", ".join(value) if value else "설치된 모델 없음"))
                elif tag == "pull": self.pull_status.set(f"다운로드 {value.percent}%  {value.completed / 1e9:.1f} / {value.total / 1e9:.1f} GB" if value.total else value.status)
                elif tag == "pull_label": self.pull_status.set(f"{value} 다운로드 준비 중…")
                elif tag == "pull_done": self.pull_status.set("✓ 다운로드 완료")
                else: messagebox.showerror("무료 일본어 도우미", str(value), parent=self)
        except queue.Empty: pass
        if self.winfo_exists(): self.after(200, self._pump)


class _GeneratorWindow(tk.Toplevel):
    def __init__(self, master, *, service=None):
        super().__init__(master); ensure_theme(self)
        self.service = service or make_service(); self.cancel_event = threading.Event(); self._q = queue.Queue()
        self.result = None; self.candidate_boxes = []
        self.after(200, self._pump)

    def _run(self, fn, done=None):
        self.cancel_event.clear(); self.message.set("내 PC에서 처리 중…"); self.generate_btn.configure(state="disabled")
        def work():
            try: self._q.put((True, fn(), done))
            except Exception as e: self._q.put((False, e, done))
        threading.Thread(target=work, name="jp-language", daemon=True).start()

    def cancel(self): self.cancel_event.set(); self.message.set("취소 요청 중…")

    def _pump(self):
        try:
            while True:
                ok, value, done = self._q.get_nowait(); self.generate_btn.configure(state="normal")
                if ok:
                    if done: done(value)
                    else: self.result = value; self.show_result(value)
                else:
                    msg = str(value)
                    if "권한" in msg or "insufficientPermissions" in msg:
                        msg = "이 YouTube 채널은 댓글 작성 권한을 아직 허용하지 않았습니다. [댓글 권한 추가하고 다시 연결]을 눌러 이 채널만 다시 연결하세요."
                    if "memory" in msg.lower() or "메모리" in msg:
                        msg += "\n이 PC에서는 가벼운 모델을 사용하는 것이 좋습니다. [Qwen3 4B로 변경] 또는 [다시 시도]를 선택하세요."
                    self.message.set(msg)
        except queue.Empty: pass
        try:
            if self.winfo_exists(): self.after(200, self._pump)
        except tk.TclError: pass

    def show_result(self, result):
        self.message.set("직접 확인 권장" if result.review_required else "✓ 3개 문장을 만들었습니다.")
        for box, item in zip(self.candidate_boxes, result.candidates):
            box.delete("1.0", "end"); box.insert("1.0", item["ja"])

    def switch_to_4b(self):
        cfg = language_settings()
        cfg["model"] = QWEN_4B
        update_settings(japanese_assistant=cfg)
        self.service.model = QWEN_4B
        self.message.set("Qwen3 4B로 변경했습니다. 모델이 없다면 설정에서 다운로드하세요.")


class JapaneseReplyAssistant(_GeneratorWindow):
    def __init__(self, master, *, comment: str, on_select=None, service=None):
        super().__init__(master, service=service); self.title("무료 일본어 도우미")
        self.on_select = on_select; self.geometry("760x720")
        root = ttk.Frame(self, padding=12); root.pack(fill="both", expand=True)
        ttk.Label(root, text="무료 일본어 도우미", font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text=LOCAL_NOTE, foreground="darkgreen", wraplength=720).pack(anchor="w")
        ttk.Button(root, text="무료 일본어 도우미 설정", command=lambda: JapaneseSetupWizard(self)).pack(anchor="e")
        ttk.Label(root, text="일본어 원문").pack(anchor="w"); self.comment = tk.Text(root, height=4, wrap="word"); self.comment.insert("1.0", comment); self.comment.pack(fill="x")
        self.translation = tk.StringVar(); self.nuance = tk.StringVar()
        ttk.Label(root, text="한국어 번역").pack(anchor="w", pady=(8, 0)); ttk.Label(root, textvariable=self.translation, wraplength=710).pack(anchor="w")
        ttk.Label(root, text="뉘앙스").pack(anchor="w", pady=(8, 0)); ttk.Label(root, textvariable=self.nuance, wraplength=710).pack(anchor="w")
        ttk.Label(root, text="한국어로 답변 의도 입력 (비워도 답글 추천 가능)").pack(anchor="w", pady=(8, 0)); self.intent = tk.Text(root, height=3); self.intent.pack(fill="x")
        self.message = tk.StringVar(); ttk.Label(root, textvariable=self.message, foreground="darkorange").pack(anchor="w")
        row = ttk.Frame(root); row.pack(fill="x", pady=6)
        self.generate_btn = ttk.Button(row, text="일본어 답글 만들기 / 답글 추천", command=self.generate); self.generate_btn.pack(side="left")
        ttk.Button(row, text="생성 취소", command=self.cancel).pack(side="left", padx=6)
        ttk.Button(row, text="Qwen3 4B로 변경", command=self.switch_to_4b).pack(side="left")
        for label in ("자연스럽게", "따뜻하게", "조금 더 편하게"):
            frame = ttk.LabelFrame(root, text=label, padding=5); frame.pack(fill="x", pady=3)
            box = tk.Text(frame, height=3, wrap="word"); box.pack(side="left", fill="x", expand=True); self.candidate_boxes.append(box)
            ttk.Button(frame, text="선택", command=lambda b=box: self.select(b)).pack(side="right", padx=5)

    def generate(self):
        self._run(lambda: self.service.analyze(self.comment.get("1.0", "end").strip(), self.intent.get("1.0", "end").strip(), cancel_event=self.cancel_event))

    def show_result(self, result):
        super().show_result(result); self.translation.set(result.translation_ko); self.nuance.set(result.nuance_ko)

    def select(self, box):
        text = box.get("1.0", "end").strip()
        if self.on_select: self.on_select(text)
        self.destroy()


class ExternalJapaneseCommentHelper(_GeneratorWindow):
    def __init__(self, master, *, service=None, workflow=None, profiles=None, open_channels=None):
        super().__init__(master, service=service); self.title("일본 영상 댓글 도우미"); self.geometry("760x720")
        self.profiles = profiles or ProfileStore()
        self.workflow = workflow or ExternalJapaneseCommentService(self.profiles)
        self._open_channels = open_channels
        self.current_video = None; self.selected_text = ""; self.selected_box = None; self.last_record = None
        root = ttk.Frame(self, padding=12); root.pack(fill="both", expand=True)
        ttk.Label(root, text="일본 영상 댓글 도우미", font="PLS.Title").pack(anchor="w")
        ttk.Label(root, text=LOCAL_NOTE + " 자동으로 여러 영상에 댓글을 다는 기능은 없습니다.", foreground="darkgreen").pack(anchor="w")
        profiles_list = self.workflow.japanese_profiles(); self._profile_ids = [p.profile_id for p in profiles_list]
        self.author = tk.StringVar(value=profiles_list[0].label if profiles_list else "")
        ttk.Label(root, text="댓글을 작성할 YouTube 채널").pack(anchor="w", pady=(6, 0))
        self.author_combo = ttk.Combobox(root, textvariable=self.author, state="readonly", values=[p.label for p in profiles_list])
        self.author_combo.pack(fill="x")
        ttk.Button(root, text="댓글 권한 추가하고 다시 연결", command=self.reconnect).pack(anchor="e", pady=2)
        self.url = tk.StringVar(); self.title_var = tk.StringVar(); self.channel = tk.StringVar(); self.genre = tk.StringVar(value=GENRES[0]); self.message = tk.StringVar()
        for label, var in (("YouTube 영상 URL", self.url), ("영상 제목", self.title_var), ("대상 채널", self.channel)):
            ttk.Label(root, text=label).pack(anchor="w", pady=(6, 0)); ttk.Entry(root, textvariable=var).pack(fill="x")
        ttk.Button(root, text="영상 정보 가져오기", command=self.fetch_video).pack(anchor="w", pady=4)
        ttk.Label(root, text="내 감상 메모").pack(anchor="w", pady=(6, 0)); self.memo = tk.Text(root, height=4); self.memo.pack(fill="x")
        ttk.Label(root, text="일본 20~30대 여성에게 자연스럽게 느껴지는 부드러운 댓글 스타일").pack(anchor="w", pady=(6, 0)); ttk.Combobox(root, textvariable=self.genre, values=GENRES, state="readonly").pack(anchor="w")
        ttk.Label(root, textvariable=self.message, foreground="darkorange").pack(anchor="w")
        row = ttk.Frame(root); row.pack(fill="x", pady=6)
        self.generate_btn = ttk.Button(row, text="일본어 댓글 만들기", command=self.generate); self.generate_btn.pack(side="left")
        ttk.Button(row, text="생성 취소", command=self.cancel).pack(side="left", padx=4)
        ttk.Button(row, text="Qwen3 4B로 변경", command=self.switch_to_4b).pack(side="left")
        ttk.Button(row, text="YouTube에서 영상 열기", command=self.open_video).pack(side="right")
        self.meanings = []
        for label in ("담백하게", "감성적으로", "편안하게"):
            frame = ttk.LabelFrame(root, text=label, padding=5); frame.pack(fill="x", pady=3)
            inner = ttk.Frame(frame); inner.pack(side="left", fill="x", expand=True)
            box = tk.Text(inner, height=2); box.pack(fill="x"); self.candidate_boxes.append(box)
            meaning = tk.StringVar(); self.meanings.append(meaning)
            ttk.Label(inner, textvariable=meaning, foreground="gray30", wraplength=600).pack(anchor="w")
            buttons = ttk.Frame(frame); buttons.pack(side="right", padx=5)
            ttk.Button(buttons, text="선택", command=lambda b=box: self.select_candidate(b)).pack()
            ttk.Button(buttons, text="복사", command=lambda b=box: self.copy(b)).pack(pady=2)
        action = ttk.Frame(root); action.pack(fill="x", pady=6)
        ttk.Button(action, text="실제 YouTube에 게시", command=self.post).pack(side="left")
        ttk.Button(action, text="방금 댓글 삭제", command=self.delete_last).pack(side="left", padx=4)
        ttk.Button(action, text="작성한 일본 댓글", command=self.open_history).pack(side="right")

    def selected_profile_id(self):
        values = list(self.author_combo.cget("values") or ())
        return self._profile_ids[values.index(self.author.get())] if self.author.get() in values else ""

    def reconnect(self):
        if self._open_channels:
            return self._open_channels()
        from .help_ui import ask_connect_guide
        from .youtube_channels_ui import ChannelManagerWindow
        self.channel_win = ChannelManagerWindow(self, profiles=self.profiles, connect_guide=ask_connect_guide)
        return self.channel_win

    def fetch_video(self):
        pid = self.selected_profile_id()
        self.message.set("영상 정보를 가져오고 있습니다…")
        self._run(lambda: self.workflow.video(pid, self.url.get()), self._video_done)

    def _video_done(self, video):
        self.current_video = video; self.title_var.set(video.title); self.channel.set(video.channel_title)
        self.message.set("✓ 영상 정보를 가져왔습니다. 실제로 느낀 감상을 한 줄 적어주세요.")

    def generate(self):
        if not self.memo.get("1.0", "end").strip():
            self.message.set("짧은 감상을 적어주면 더 자연스러운 댓글을 만들 수 있습니다.")
        self._run(lambda: self.service.external(title=self.title_var.get(), channel=self.channel.get(),
            memo_ko=self.memo.get("1.0", "end").strip(), genre=self.genre.get(), cancel_event=self.cancel_event))

    def show_result(self, result):
        super().show_result(result)
        for var, item in zip(self.meanings, result.candidates):
            var.set("한국어: " + (item.get("ko") or "직접 의미를 확인하세요."))

    def select_candidate(self, box):
        self.selected_box = box
        self.selected_text = box.get("1.0", "end").strip()
        self.message.set("✓ 후보를 선택했습니다. 필요하면 직접 수정한 뒤 게시를 누르세요.")

    def copy(self, box):
        text = box.get("1.0", "end").strip(); self.clipboard_clear(); self.clipboard_append(text)
        self.service.history.add(video_url=self.url.get(), channel_title=self.channel.get(), draft=text, tone=PROFILE)
        self.message.set("✓ 복사했습니다. YouTube에서 직접 확인한 뒤 게시하세요.")

    def open_video(self):
        url = self.url.get().strip(); p = urlparse(url)
        if p.scheme == "https" and p.hostname in ("youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com"):
            webbrowser.open(url)
        else: messagebox.showinfo("영상 열기", "올바른 YouTube 주소를 입력하세요.", parent=self)

    def _ensure_video(self):
        if self.current_video and self.current_video.video_id == parse_youtube_video_id(self.url.get()):
            return self.current_video
        return ExternalVideo(parse_youtube_video_id(self.url.get()), self.title_var.get(), self.channel.get(), "")

    def post(self):
        pid, video = self.selected_profile_id(), self._ensure_video()
        text = self.selected_box.get("1.0", "end").strip() if self.selected_box else self.selected_text
        if not text:
            messagebox.showinfo("댓글 게시", "3개 후보 중 하나를 선택하세요.", parent=self); return
        try: preview = self.workflow.preview(pid, video, text)
        except Exception as e: messagebox.showwarning("댓글 품질 확인", str(e), parent=self); return
        warning = "\n\n⚠ 이 영상에 이전 댓글 기록이 있습니다." if preview["previous_warning"] else ""
        count = preview["daily_count"]
        if count >= 10: warning += f"\n\n오늘 외부 댓글: {count}개. 각 영상에 실제 감상을 남기는 것을 권장합니다."
        body = (f"작성 채널: {preview['author']}\n대상 영상: {preview['target_video']}\n"
                f"대상 채널: {preview['target_channel']}\n\n댓글:\n{preview['comment']}{warning}")
        if not messagebox.askyesno("최종 확인", body + "\n\n실제 YouTube에 게시할까요?", parent=self): return
        self.message.set("댓글을 게시하고 있습니다…")
        self._run(lambda: self.workflow.post_confirmed(pid, video, text, confirmed=True), self._posted)

    def _posted(self, record):
        self.last_record = record; self.message.set("✓ 댓글을 게시했습니다.")
        messagebox.showinfo("댓글 게시 완료", f"댓글을 게시했습니다.\n\n영상: {record.video_title}\n작성 채널: {record.author_channel_title}", parent=self)

    def delete_last(self):
        if not self.last_record: messagebox.showinfo("댓글 삭제", "이 창에서 방금 작성한 댓글이 없습니다.", parent=self); return
        if not messagebox.askyesno("댓글 삭제", "방금 작성한 댓글을 삭제할까요?", parent=self): return
        self._run(lambda: self.workflow.delete_confirmed(self.last_record.author_profile_id,
                  self.last_record.comment_id, confirmed=True), lambda r: self.message.set("✓ 댓글 삭제 완료"))

    def open_history(self):
        d = tk.Toplevel(self); d.title("작성한 일본 댓글")
        tree = ttk.Treeview(d, columns=("date", "author", "channel", "video", "text", "state"), show="headings")
        for col, name in zip(tree["columns"], ("날짜", "내 채널", "대상 채널", "영상 제목", "댓글 일부", "상태")):
            tree.heading(col, text=name); tree.column(col, width=120)
        for r in reversed(self.workflow.history.all()):
            tree.insert("", "end", values=(time.strftime("%Y-%m-%d", time.localtime(r.posted_at)), r.author_channel_title,
                r.target_channel, r.video_title, (r.text or "")[:50], {"POSTED":"게시됨", "DELETED":"삭제됨", "FAILED":"게시 실패"}.get(r.status, r.status)))
        tree.pack(fill="both", expand=True); d.geometry("850x360")
