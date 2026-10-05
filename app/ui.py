from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .core import (
    BuildCancelled, BuildError, VideoInfo, build_round_plan, build_time_plan,
    default_output_name_rounds, default_output_name_time, enough_disk_space,
    ensure_unique_output, format_duration, probe_video, run_concat_copy,
    strict_copy_compatibility, target_seconds,
)
from .settings import load_settings, save_settings, update_settings
from .live_supervisor import busy_message
from .live_ui import LiveWindow
from .tooling import FFMPEG_GUARD, discover_ffmpeg, prevent_windows_sleep, release_tk_variables, remember_ffmpeg
from .ui_scroll import ScrollFrame

PRODUCT_NAME = "YouTube Playlist Studio"
PRODUCT_TITLE = f"{PRODUCT_NAME} v0.3 (Playlist Long Video Maker)"  # 설정 폴더/EXE 이름은 그대로


@dataclass
class QueueJob:
    inputs: list[str]
    mode: str
    rounds: int
    hours: int
    minutes: int
    output: str
    status: str = "대기"

    @classmethod
    def from_dict(cls, d):
        status = str(d.get("status", "대기"))
        if status == "진행 중":
            status = "대기"
        return cls(
            [str(x) for x in d.get("inputs", [])],
            str(d.get("mode", "rounds")),
            int(d.get("rounds", 10)),
            int(d.get("hours", 10)),
            int(d.get("minutes", 0)),
            str(d.get("output", "")),
            status,
        )


class MainWindow(tk.Tk):
    def __init__(self, app_root: Path):
        super().__init__()
        self.app_root = Path(app_root)
        self.title(PRODUCT_TITLE)
        # 작은 화면(1366×768)에서도 열리도록 화면 높이에 맞춘다. 내용은 세로 스크롤로 끝까지 닿는다.
        sh = self.winfo_screenheight()
        self.geometry(f"1080x{max(560, min(990, sh - 90))}")
        self.minsize(820, 520)
        self.protocol("WM_DELETE_WINDOW", self._close)

        self.ffmpeg = None
        self.ffprobe = None
        self.infos: list[VideoInfo] = []
        self.jobs: list[QueueJob] = []
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.running = False
        self.live_win = None
        self.upload_win = None
        self.live_schedule_win = None
        self.upload_queue = None  # 예약 업로드 대기열: 창을 닫아도 업로드가 계속되도록 MainWindow가 가진다
        self.mode_cards = {}
        self.mode_summary = tk.StringVar()

        self.mode = tk.StringVar(value="rounds")
        self.rounds = tk.IntVar(value=10)
        self.hours = tk.IntVar(value=10)
        self.minutes = tk.IntVar(value=0)
        self.outdir = tk.StringVar()
        self.outname = tk.StringVar()
        self.summary = tk.StringVar(value="SET 영상을 추가하세요.")
        self.compat = tk.StringVar()
        self.calc = tk.StringVar()
        self.qsummary = tk.StringVar(value="대기열 0/5")
        self.status = tk.StringVar(value="대기 중")
        self.keep_awake = tk.BooleanVar(value=True)
        self.keep_going = tk.BooleanVar(value=True)

        self._ui()
        self._restore()
        self._tools()
        self.after(100, self._pump)
        self.after(300, self._refresh_summary)

    def _mode_cards(self, root):
        """상단 3개 모드 카드. ①은 지금 이 화면(기존 제작 UI 그대로), ②③은 별도 창을 연다."""
        bar = ttk.Frame(root); bar.pack(fill="x", pady=(0, 4))
        specs = (("long", "① 영상 늘리기", "SET 영상을 장시간 MP4로 제작", None, "● 현재 화면"),
                 ("live", "② 실시간 스트리밍", "Cloud / 내 PC에서 Playlist LIVE", self._open_live, "LIVE 창 열기 ▶"),
                 ("upload", "③ 예약 업로드", "한국·일본 등 여러 채널에 자동 예약", self._open_upload, "예약 업로드 열기 ▶"))
        for i, (key, title, desc, cmd, foot) in enumerate(specs):
            active = cmd is None
            bg, border, fg = ("#e8f1ff", "#2f6fdf", "#1d4fa8") if active else ("#f6f6f6", "#c4c4c4", "#202020")
            bar.columnconfigure(i, weight=1, uniform="card")
            card = tk.Frame(bar, bg=bg, highlightthickness=2, highlightbackground=border, highlightcolor=border,
                            cursor="" if active else "hand2")
            card.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            hd = tk.Frame(card, bg=bg); hd.pack(fill="x", padx=10, pady=(5, 0))
            tk.Label(hd, text=title, bg=bg, fg=fg, font=("Segoe UI", 13, "bold")).pack(side="left")
            tk.Label(hd, text=foot, bg=bg, fg=border if active else "#2f6fdf").pack(side="right")
            ft = tk.Frame(card, bg=bg); ft.pack(fill="x", padx=10, pady=(0, 5))
            tk.Label(ft, text=desc, bg=bg, fg="gray25").pack(side="left")
            if key == "live":
                sched = tk.Label(ft, text="예약 LIVE", bg=bg, fg="#2f6fdf", cursor="hand2")
                sched.pack(side="right")
                sched.bind("<Button-1>", lambda e: (self._open_live_schedule(), "break")[1])
            if cmd:
                for w in (card, hd, ft, *hd.winfo_children(), *ft.winfo_children()):
                    if w.bind("<Button-1>"):
                        continue
                    w.bind("<Button-1>", lambda e, c=cmd: c())
            self.mode_cards[key] = card
        ttk.Frame(root, height=6).pack(fill="x")

    def _upload_counts(self):
        from .youtube_upload_queue import queue_counts
        return self.upload_queue.counts() if self.upload_queue is not None else queue_counts()

    def _live_kind(self):
        """'local' = 이 PC에서 송출 중 (인터넷 업로드 대역폭 사용), 'cloud' = Cloud 서버가 송출, '' = LIVE 없음."""
        w = self._live_window()
        if w is None:
            return ""
        try:
            if w.controller.active:
                return "local"
            if w.cloud.cloud_live_active:
                return "cloud"
        except Exception:
            pass
        return ""

    def _live_state_text(self):
        w = self._live_window()
        if w is None:
            return "LIVE 창 닫힘"
        kind = self._live_kind()
        if kind == "local":
            return "● LIVE 송출 중 (내 PC)"
        if kind == "cloud":
            return "● CLOUD LIVE"
        try:
            if w.cloud.busy:
                return "LIVE Cloud 확인 중"
        except Exception:
            pass
        return "LIVE 대기"

    def _refresh_summary(self):
        try:
            waiting = sum(j.status != "완료" for j in self.jobs)
            c = self._upload_counts()
            up = " · 업로드 중" if self.upload_queue is not None and self.upload_queue.running else ""
            self.mode_summary.set(f"영상 제작 대기 {waiting} · {self._live_state_text()} · 예약 업로드 대기 {c['waiting']}{up}"
                                  f" · 예약 완료 {c['done']}")
        except tk.TclError:
            return
        self.after(1500, self._refresh_summary)

    def _ui(self):
        self.scroll = ScrollFrame(self)
        self.scroll.pack(fill="both", expand=True)
        root = ttk.Frame(self.scroll.body, padding=12)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="x")
        ttk.Label(top, text=PRODUCT_NAME, font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Label(top, text="v0.3.0").pack(side="right")
        ttk.Label(root, text="완성된 SET MP4를 회차 기준으로 무손실 반복 연결합니다.").pack(anchor="w")
        tr = ttk.Frame(root); tr.pack(fill="x", pady=(4, 8))
        self.tool_text = ttk.Label(tr, text="FFmpeg 확인 중...")
        self.tool_text.pack(side="left")
        ttk.Button(tr, text="FFmpeg 설정", command=self._pick_ffmpeg).pack(side="right")
        ttk.Label(tr, textvariable=self.mode_summary, foreground="gray25").pack(side="right", padx=(0, 10))
        self._mode_cards(root)

        f1 = ttk.LabelFrame(root, text="① SET 영상", padding=7)
        f1.pack(fill="x")
        br = ttk.Frame(f1); br.pack(fill="x", pady=(0, 5))
        ttk.Button(br, text="＋ 영상 추가", command=self._add_video).pack(side="left")
        ttk.Button(br, text="선택 삭제", command=self._del_video).pack(side="left", padx=4)
        ttk.Button(br, text="▲ 위로", command=lambda:self._move(-1)).pack(side="left", padx=(8,2))
        ttk.Button(br, text="▼ 아래로", command=lambda:self._move(1)).pack(side="left")
        ttk.Button(br, text="전체 비우기", command=self._clear_video).pack(side="right")

        cols = ("n","name","dur","res","fps","codec","size")
        self.vtree = ttk.Treeview(f1, columns=cols, show="headings", height=5)
        labels = ("#","파일명","길이","해상도","FPS","코덱","용량")
        widths = (40,390,90,100,70,110,90)
        for c, h, w in zip(cols, labels, widths):
            self.vtree.heading(c, text=h); self.vtree.column(c, width=w, anchor="w" if c=="name" else "center")
        self.vtree.pack(fill="x")
        ttk.Label(f1, textvariable=self.summary).pack(anchor="w", pady=(5,0))
        ttk.Label(f1, textvariable=self.compat).pack(anchor="w")

        f2 = ttk.LabelFrame(root, text="② 종료 기준", padding=7)
        f2.pack(fill="x", pady=(8,0))
        rr = ttk.Frame(f2); rr.pack(fill="x")
        ttk.Radiobutton(rr, text="회차 기준 (권장 · SET 중간에서 안 끊김)", variable=self.mode, value="rounds", command=self._recalc).pack(side="left")
        for n in (5,10,15,20):
            ttk.Button(rr, text=f"{n}회", width=6, command=lambda x=n:self._set_round(x)).pack(side="left", padx=2)
        ttk.Label(rr, text="직접").pack(side="left", padx=(8,2))
        rs = ttk.Spinbox(rr, from_=1, to=100, width=6, textvariable=self.rounds, command=self._recalc)
        rs.pack(side="left"); ttk.Label(rr, text="회").pack(side="left")
        rs.bind("<KeyRelease>", lambda e:self._recalc())

        hr = ttk.Frame(f2); hr.pack(fill="x", pady=(6,0))
        ttk.Radiobutton(hr, text="시간 기준 (마지막 SET 중간에서 끝날 수 있음)", variable=self.mode, value="time", command=self._recalc).pack(side="left")
        for n in (3,5,8,10,11):
            ttk.Button(hr, text=f"{n}시간", width=6, command=lambda x=n:self._set_hour(x)).pack(side="left", padx=2)
        ttk.Label(hr, text="직접").pack(side="left", padx=(8,2))
        hs = ttk.Spinbox(hr, from_=0, to=12, width=5, textvariable=self.hours, command=self._recalc)
        ms = ttk.Spinbox(hr, from_=0, to=59, width=5, textvariable=self.minutes, command=self._recalc)
        hs.pack(side="left"); ttk.Label(hr, text="시간").pack(side="left")
        ms.pack(side="left", padx=(4,0)); ttk.Label(hr, text="분").pack(side="left")
        hs.bind("<KeyRelease>", lambda e:self._recalc()); ms.bind("<KeyRelease>", lambda e:self._recalc())
        ttk.Label(f2, textvariable=self.calc, font=("Segoe UI",10,"bold")).pack(anchor="w", pady=(6,0))
        ttk.Label(f2, text="※ 화질·음질 보존을 위해 재인코딩하지 않습니다. FFmpeg -c copy만 사용합니다.").pack(anchor="w")

        f3 = ttk.LabelFrame(root, text="③ 저장 및 대기열", padding=7)
        f3.pack(fill="x", pady=(8,0))
        a = ttk.Frame(f3); a.pack(fill="x")
        ttk.Label(a, text="저장 폴더", width=10).pack(side="left")
        ttk.Entry(a, textvariable=self.outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(a, text="변경", command=self._pick_outdir).pack(side="left", padx=(5,0))
        b = ttk.Frame(f3); b.pack(fill="x", pady=(4,0))
        ttk.Label(b, text="파일명", width=10).pack(side="left")
        ttk.Entry(b, textvariable=self.outname).pack(side="left", fill="x", expand=True)
        ttk.Button(f3, text="＋ 현재 설정을 대기열에 추가", command=self._add_job).pack(fill="x", pady=(6,0))

        qf = ttk.LabelFrame(root, text="④ 자동 대기열 (최대 5개 · 1개씩 순차 실행)", padding=7)
        qf.pack(fill="both", expand=True, pady=(8,0))
        qt = ttk.Frame(qf); qt.pack(fill="x")
        ttk.Label(qt, textvariable=self.qsummary).pack(side="left")
        ttk.Button(qt, text="전체 비우기", command=self._clear_jobs).pack(side="right")
        ttk.Button(qt, text="선택 삭제", command=self._del_job).pack(side="right", padx=4)
        ttk.Button(qt, text="③ 예약 업로드로 보내기", command=self._send_to_upload).pack(side="right", padx=(0, 8))
        qcols=("n","inputs","mode","dur","output","state")
        self.qtree=ttk.Treeview(qf,columns=qcols,show="headings",height=5)
        qlabels=("#","SET","기준","예상 길이","출력 파일","상태")
        qwidths=(36,250,90,95,360,85)
        for c,h,w in zip(qcols,qlabels,qwidths):
            self.qtree.heading(c,text=h); self.qtree.column(c,width=w,anchor="w" if c in ("inputs","output") else "center")
        self.qtree.pack(fill="both", expand=True, pady=(4,0))
        op=ttk.Frame(qf); op.pack(fill="x",pady=(5,0))
        ttk.Checkbutton(op,text="작업 중 Windows 절전 방지",variable=self.keep_awake).pack(side="left")
        ttk.Checkbutton(op,text="한 작업 실패해도 다음 작업 계속",variable=self.keep_going).pack(side="left",padx=12)

        ar=ttk.Frame(root); ar.pack(fill="x",pady=(8,0))
        self.start=ttk.Button(ar,text="▶ 대기열 자동 시작",command=self._start)
        self.start.pack(side="left",fill="x",expand=True)
        self.stop=ttk.Button(ar,text="■ 현재 작업 중지",command=self._stop,state="disabled")
        self.stop.pack(side="left",padx=(5,0))
        self.bar=ttk.Progressbar(root,maximum=100)
        self.bar.pack(fill="x",pady=(6,0))
        ttk.Label(root,textvariable=self.status).pack(anchor="w")

    def _tools(self):
        pair=discover_ffmpeg(self.app_root)
        if pair:
            self.ffmpeg,self.ffprobe=pair
            self.tool_text.configure(text=f"✓ FFmpeg 준비됨 · {self.ffmpeg}",foreground="green")
            return True
        self.ffmpeg=self.ffprobe=None
        self.tool_text.configure(text="⚠ FFmpeg/ffprobe를 찾지 못했습니다.",foreground="darkorange")
        return False

    def _pick_ffmpeg(self):
        p=filedialog.askopenfilename(title="ffmpeg.exe 선택",filetypes=[("FFmpeg","ffmpeg.exe"),("모든 파일","*.*")])
        if not p:return
        pair=remember_ffmpeg(Path(p))
        if not pair:
            messagebox.showerror("FFmpeg","같은 폴더에 ffprobe.exe가 있어야 합니다."); return
        self.ffmpeg,self.ffprobe=pair
        self.tool_text.configure(text=f"✓ FFmpeg 준비됨 · {self.ffmpeg}",foreground="green")

    def _need_tools(self):
        if self.ffmpeg and self.ffprobe and self.ffmpeg.exists() and self.ffprobe.exists(): return True
        if self._tools(): return True
        self._pick_ffmpeg()
        return bool(self.ffmpeg and self.ffprobe)

    def _add_video(self):
        if not self._need_tools(): return
        paths=filedialog.askopenfilenames(title="완성 SET MP4 선택",filetypes=[("MP4","*.mp4"),("영상","*.mov *.mkv *.m4v"),("모든 파일","*.*")])
        for raw in paths:
            p=Path(raw).resolve()
            if any(x.path==p for x in self.infos): continue
            try:self.infos.append(probe_video(p,self.ffprobe))
            except Exception as e:messagebox.showerror("영상 분석 실패",str(e))
        if self.infos and not self.outdir.get():
            self.outdir.set(str(self.infos[0].path.parent/"LONG_OUTPUT"))
        self._refresh_v(); self._recalc()

    def _del_video(self):
        sel=self.vtree.selection()
        if sel:self.infos.pop(self.vtree.index(sel[0])); self._refresh_v(); self._recalc()

    def _clear_video(self):
        self.infos.clear(); self._refresh_v(); self._recalc()

    def _move(self,d):
        sel=self.vtree.selection()
        if not sel:return
        i=self.vtree.index(sel[0]); j=i+d
        if 0<=j<len(self.infos):
            self.infos[i],self.infos[j]=self.infos[j],self.infos[i]; self._refresh_v(j); self._recalc()

    def _refresh_v(self,select=None):
        for x in self.vtree.get_children():self.vtree.delete(x)
        for i,x in enumerate(self.infos,1):
            self.vtree.insert("", "end", values=(i,x.path.name,format_duration(x.duration),f"{x.width}×{x.height}",f"{x.fps:.2f}",f"{x.video_codec.upper()}/{x.audio_codec.upper() if x.audio_codec else '-'}",f"{x.size/1024**3:.2f} GB"))
        if select is not None and self.vtree.get_children():
            item=self.vtree.get_children()[select]; self.vtree.selection_set(item)
        if not self.infos:
            self.summary.set("SET 영상을 추가하세요."); self.compat.set(""); return
        dur=sum(x.duration for x in self.infos)
        self.summary.set(f"SET {len(self.infos)}개 · 1회차 {format_duration(dur)} · 여러 SET이면 목록 전체 1바퀴가 1회차")
        ok,msg=strict_copy_compatibility(self.infos)
        self.compat.set(("✓ " if ok else "⚠ ")+msg+("" if ok else " · 자동 재인코딩하지 않습니다."))

    def _set_round(self,n):
        self.mode.set("rounds"); self.rounds.set(n); self._recalc()

    def _set_hour(self,n):
        self.mode.set("time"); self.hours.set(n); self.minutes.set(0); self._recalc()

    def _plan(self,infos=None,job=None):
        infos=infos if infos is not None else self.infos
        if job:
            return build_round_plan(infos,job.rounds) if job.mode=="rounds" else build_time_plan(infos,target_seconds(job.hours,job.minutes))
        return build_round_plan(infos,int(self.rounds.get())) if self.mode.get()=="rounds" else build_time_plan(infos,target_seconds(int(self.hours.get()),int(self.minutes.get())))

    def _recalc(self):
        if not self.infos:
            self.calc.set(""); self.outname.set(""); return
        try:p=self._plan()
        except Exception as e:self.calc.set(f"⚠ {e}"); return
        if self.mode.get()=="rounds":
            name=default_output_name_rounds(self.infos[0].path,p.cycles); label=f"{p.cycles}회차"
        else:
            name=default_output_name_time(self.infos[0].path,int(self.hours.get()),int(self.minutes.get())); label="시간 기준"
        if len(self.infos)>1:name=name.replace("_FINAL.mp4","_MULTI_FINAL.mp4")
        warn=" · ⚠ 12시간 초과" if p.expected_duration>43200 else ""
        self.calc.set(f"{label} · 완성 약 {format_duration(p.expected_duration)} · 예상 용량 약 {p.expected_size/1024**3:.1f} GB{warn}")
        self.outname.set(name)

    def _pick_outdir(self):
        p=filedialog.askdirectory(initialdir=self.outdir.get() or None)
        if p:self.outdir.set(p)

    def _add_job(self):
        if self.running:return
        if len(self.jobs)>=5:messagebox.showwarning("대기열","최대 5개까지 등록할 수 있습니다.");return
        if not self.infos:messagebox.showwarning("SET","먼저 SET 영상을 추가하세요.");return
        ok,msg=strict_copy_compatibility(self.infos)
        if not ok:messagebox.showerror("무손실 연결 불가",msg+"\nCapCut에서 동일한 출력 설정으로 다시 내보내주세요.");return
        try:p=self._plan()
        except Exception as e:messagebox.showerror("설정",str(e));return
        if not self.outdir.get().strip() or not self.outname.get().strip():messagebox.showwarning("저장","저장 위치를 확인하세요.");return
        outdir=Path(self.outdir.get().strip()); outdir.mkdir(parents=True,exist_ok=True)
        name=self.outname.get().strip()
        if not name.lower().endswith(".mp4"):name+=".mp4"
        output=ensure_unique_output(outdir/name)
        queued={Path(j.output) for j in self.jobs}
        while output in queued:output=output.with_name(output.stem+"_Q"+output.suffix)
        enough,free,need=enough_disk_space(outdir,p.expected_size)
        if not enough:messagebox.showerror("저장 공간 부족",f"남음 {free/1024**3:.1f} GB / 필요 약 {need/1024**3:.1f} GB");return
        self.jobs.append(QueueJob([str(x.path) for x in self.infos],self.mode.get(),int(self.rounds.get()),int(self.hours.get()),int(self.minutes.get()),str(output)))
        self._refresh_q();self._save();self.status.set(f"대기열 추가 완료 · {len(self.jobs)}/5")

    def _del_job(self):
        if self.running:return
        sel=self.qtree.selection()
        if sel:self.jobs.pop(self.qtree.index(sel[0]));self._refresh_q();self._save()

    def _clear_jobs(self):
        if self.running:return
        self.jobs.clear();self._refresh_q();self._save()

    def _job_info(self,j):
        try:
            infos=[probe_video(Path(p),self.ffprobe) for p in j.inputs] if self.ffprobe else []
            p=self._plan(infos,j)
            return (f"{j.rounds}회" if j.mode=="rounds" else f"{j.hours}h{j.minutes:02d}m",format_duration(p.expected_duration))
        except Exception:return ("-","-")

    def _refresh_q(self):
        for x in self.qtree.get_children():self.qtree.delete(x)
        for i,j in enumerate(self.jobs,1):
            mode,dur=self._job_info(j); names=", ".join(Path(p).name for p in j.inputs)
            self.qtree.insert("","end",values=(i,names,mode,dur,Path(j.output).name,j.status))
        self.qsummary.set(f"대기열 {len(self.jobs)}/5 · FFmpeg는 항상 1개씩 순차 실행")

    def _start(self):
        if self.running:return
        if not self.jobs:messagebox.showwarning("대기열","작업을 먼저 추가하세요.");return
        if not self._need_tools():return
        idx=[i for i,j in enumerate(self.jobs) if j.status!="완료"]
        if not idx:messagebox.showinfo("대기열","모든 작업이 완료 상태입니다.");return
        if not FFMPEG_GUARD.try_acquire("long"):messagebox.showwarning("FFmpeg 사용 중",busy_message(FFMPEG_GUARD.owner)+"\n끝난 뒤 장시간 영상 제작을 시작하세요.");return
        self.running=True;self.cancel.clear();self.start.configure(state="disabled");self.stop.configure(state="normal")
        cont=bool(self.keep_going.get());awake=bool(self.keep_awake.get())
        threading.Thread(target=self._worker_guarded,args=(idx,cont,awake),daemon=True).start()

    def _live_window(self):
        if self.live_win is not None:
            try:
                if self.live_win.winfo_exists():return self.live_win
            except tk.TclError:pass
        self.live_win=None
        return None

    def _open_live(self):
        w=self._live_window()
        if w:w.deiconify();w.lift();w.focus_set();return
        self.live_win=LiveWindow(self,tools=self._live_tools)

    @staticmethod
    def _alive(w):
        try:
            return w if w is not None and w.winfo_exists() else None
        except tk.TclError:
            return None

    def _get_upload_queue(self):
        if self.upload_queue is None:
            from .youtube_accounts import ProfileStore
            from .youtube_upload_queue import UploadQueue
            self.upload_queue = UploadQueue(ProfileStore())
        return self.upload_queue

    def _open_upload(self, video_path="", title=""):
        w = self._alive(self.upload_win)
        if w is None:
            from .youtube_upload_ui import MultiChannelUploadWindow
            w = self.upload_win = MultiChannelUploadWindow(self, upload_queue=self._get_upload_queue(),
                                                           live_guard=self._live_kind)
        else:
            w.deiconify(); w.lift(); w.focus_set()
        if video_path:
            w.set_video(video_path, title)
        return w

    def _open_live_schedule(self):
        w = self._alive(self.live_schedule_win)
        if w is not None:
            w.deiconify(); w.lift(); w.focus_set(); return w
        from .youtube_live_schedule_ui import LiveScheduleWindow
        self.live_schedule_win = LiveScheduleWindow(self)
        return self.live_schedule_win

    def _send_to_upload(self):
        sel = self.qtree.selection()
        if not sel:
            messagebox.showinfo("예약 업로드", "대기열에서 완료된 작업을 선택하세요."); return None
        j = self.jobs[self.qtree.index(sel[0])]
        out = Path(j.output)
        if j.status != "완료" or not out.is_file():
            messagebox.showwarning("예약 업로드", "제작이 완료된 영상만 예약 업로드로 보낼 수 있습니다."); return None
        return self._open_upload(str(out), out.stem)

    def _live_tools(self):
        return (self.ffmpeg,self.ffprobe) if self._need_tools() else (None,None)

    def _worker_guarded(self,indices,cont,awake):
        try:self._worker(indices,cont,awake)
        finally:FFMPEG_GUARD.release("long")

    def _stop(self):
        if self.running:self.cancel.set();self.stop.configure(state="disabled");self.status.set("현재 작업 중지 중...")

    def _worker(self,indices,cont,awake):
        stopped=False
        with prevent_windows_sleep(awake):
            for pos,idx in enumerate(indices,1):
                if self.cancel.is_set():stopped=True;break
                j=self.jobs[idx];self.events.put(("state",idx,"진행 중"))
                try:
                    infos=[probe_video(Path(p),self.ffprobe) for p in j.inputs]
                    ok,msg=strict_copy_compatibility(infos)
                    if not ok:raise BuildError(msg+"\n재인코딩하지 않습니다.")
                    p=self._plan(infos,j); out=Path(j.output);out.parent.mkdir(parents=True,exist_ok=True)
                    enough,free,need=enough_disk_space(out.parent,p.expected_size)
                    if not enough:raise BuildError(f"저장 공간 부족: {free/1024**3:.1f}GB / 필요 {need/1024**3:.1f}GB")
                    if out.exists():out=ensure_unique_output(out);j.output=str(out)
                    def prog(frac,text):
                        overall=((pos-1)+frac)/len(indices);self.events.put(("progress",overall,text,pos,len(indices)))
                    r=run_concat_copy(ffmpeg=self.ffmpeg,ffprobe=self.ffprobe,infos=infos,plan=p,output=out,cancel_event=self.cancel,progress_cb=prog)
                    self.events.put(("done",idx,str(r.output),r.duration,r.size,r.elapsed))
                except BuildCancelled:self.events.put(("state",idx,"중지"));stopped=True;break
                except Exception as e:
                    self.events.put(("error",idx,str(e)))
                    if not cont:break
        self.events.put(("finish",stopped))

    def _pump(self):
        try:
            while True:
                e=self.events.get_nowait();kind=e[0]
                if kind=="state":
                    _,i,s=e;self.jobs[i].status=s;self._refresh_q();self._save()
                elif kind=="progress":
                    _,overall,text,pos,total=e;self.bar["value"]=overall*100;self.status.set(f"작업 {pos}/{total} · {text}")
                elif kind=="done":
                    _,i,out,dur,size,elapsed=e;self.jobs[i].status="완료";self.jobs[i].output=out;self._refresh_q();self._save();self.status.set(f"완료: {Path(out).name} · {format_duration(dur)} · {size/1024**3:.1f}GB · 선택 후 [③ 예약 업로드로 보내기] 가능")
                elif kind=="error":
                    _,i,msg=e;self.jobs[i].status="실패";self._refresh_q();self._save();messagebox.showerror("작업 실패",msg)
                elif kind=="finish":
                    _,stopped=e;self.running=False;self.start.configure(state="normal");self.stop.configure(state="disabled")
                    self.status.set("대기열 중지됨" if stopped else "대기열 처리 완료");self._save()
        except queue.Empty:pass
        self.after(100,self._pump)

    def _restore(self):
        data=load_settings()
        self.keep_awake.set(bool(data.get("prevent_sleep",True)));self.keep_going.set(bool(data.get("continue_on_error",True)))
        for d in data.get("queue",[])[:5]:
            try:
                j=QueueJob.from_dict(d)
                if j.status=="완료" and not Path(j.output).exists():j.status="대기"
                self.jobs.append(j)
            except Exception:pass
        self._refresh_q()

    def _save(self):
        update_settings(prevent_sleep=bool(self.keep_awake.get()),continue_on_error=bool(self.keep_going.get()),queue=[asdict(j) for j in self.jobs])

    def _close(self):
        w=self._live_window()
        if w:
            # 내 PC LIVE는 종료 확인 후 FFmpeg 정상 종료, Cloud LIVE는 [PC만 종료](기본)/[LIVE도 종료]/[취소]
            w.confirm_close(self._close_after_live,for_app=True);return
        self._close_after_live()

    def _close_after_live(self):
        if self.running and not messagebox.askyesno("작업 중","현재 작업을 중지하고 종료할까요?"):return
        uploading=self.upload_queue is not None and self.upload_queue.running
        if uploading and not messagebox.askyesno("예약 업로드 중","예약 업로드를 중지하고 종료할까요?\n다음 실행 때 [▶ 예약 업로드 시작]을 누르면 받은 위치부터 이어서 올립니다."):return
        if self.running:self.cancel.set()
        if uploading:self.upload_queue.stop(timeout=3.0)
        self._finish_close()

    def _finish_close(self):
        w=self._live_window()
        if w:w.destroy()
        for w in (self._alive(self.upload_win),self._alive(self.live_schedule_win)):
            if w:w.destroy()
        self._save();self.destroy();release_tk_variables(self)
