# -*- coding: utf-8 -*-
"""簡易圖形介面（tkinter，Python 內建，無需額外安裝、不連網）。

執行方式：
    twmask-gui
    或 python -m tw_pii_masker.gui
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from .cli import HANDLERS, output_path_for
from .engine import MaskingEngine
from .report import Report

_FILETYPES = [
    ("支援的檔案", "*.docx *.xlsx *.xlsm *.pdf *.txt *.csv"),
    ("Word 文件", "*.docx"),
    ("Excel 活頁簿", "*.xlsx *.xlsm"),
    ("PDF 文件", "*.pdf"),
    ("文字/CSV", "*.txt *.csv"),
]

_MODE_LABELS = (
    ("partial", "部分遮罩（例：A12****789、王○○）"),
    ("full", "全遮罩（整段以 * 取代）"),
    ("label", "類型標籤（例：[身分證字號]）"),
)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("台灣個資遮罩工具（地端執行，不連網）")
        root.geometry("720x520")

        top = ttk.Frame(root, padding=10)
        top.pack(fill="x")
        ttk.Button(top, text="選擇檔案…", command=self.pick_files).pack(side="left")
        ttk.Button(top, text="選擇資料夾…", command=self.pick_folder).pack(side="left", padx=6)
        self.count_var = tk.StringVar(value="尚未選擇檔案")
        ttk.Label(top, textvariable=self.count_var).pack(side="left", padx=10)

        mode_frame = ttk.LabelFrame(root, text="遮罩模式", padding=10)
        mode_frame.pack(fill="x", padx=10)
        self.mode_var = tk.StringVar(value="partial")
        for value, label in _MODE_LABELS:
            ttk.Radiobutton(mode_frame, text=label, value=value,
                            variable=self.mode_var).pack(anchor="w")

        opt_frame = ttk.LabelFrame(root, text="選項", padding=10)
        opt_frame.pack(fill="x", padx=10, pady=(6, 0))
        self.policy_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt_frame, variable=self.policy_var,
            text="一併遮罩保單號碼／受理號碼（個資法屬間接識別資料；"
                 "業務上需保留可取消勾選）").pack(anchor="w")

        run_frame = ttk.Frame(root, padding=10)
        run_frame.pack(fill="x")
        self.run_btn = ttk.Button(run_frame, text="開始遮罩", command=self.start)
        self.run_btn.pack(side="left")
        ttk.Label(run_frame, text="輸出檔會加上 _masked 後綴，原始檔不會被修改"
                  ).pack(side="left", padx=10)

        self.log = tk.Text(root, height=18, state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.files: list[Path] = []

    # ------------------------------------------------------------------
    def pick_files(self):
        picked = filedialog.askopenfilenames(title="選擇要遮罩的檔案", filetypes=_FILETYPES)
        if picked:
            self.files = [Path(p) for p in picked]
            self.count_var.set("已選擇 %d 個檔案" % len(self.files))

    def pick_folder(self):
        folder = filedialog.askdirectory(title="選擇資料夾（含子資料夾）")
        if folder:
            self.files = [
                f for f in sorted(Path(folder).rglob("*"))
                if f.is_file() and f.suffix.lower() in HANDLERS
                and not f.stem.endswith("_masked")
            ]
            self.count_var.set("已選擇 %d 個檔案" % len(self.files))

    def append_log(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ------------------------------------------------------------------
    def start(self):
        if not self.files:
            messagebox.showwarning("尚未選擇檔案", "請先選擇要遮罩的檔案或資料夾。")
            return
        self.run_btn.configure(state="disabled")
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        exclude = None if self.policy_var.get() else ["policy_no"]
        report = Report(mode=self.mode_var.get())
        done = failed = total_hits = 0
        for src in self.files:
            # 每個檔案用獨立引擎：姓名一致遮罩的登記表不應跨檔案累積
            engine = MaskingEngine(mode=self.mode_var.get(), exclude=exclude)
            fr = report.start_file(str(src))
            self._log_async("處理中：%s" % src.name)
            try:
                out = output_path_for(src, None, "_masked")
                handler = HANDLERS[src.suffix.lower()]
                handler(str(src), str(out), engine, report, keep_metadata=False)
                counts = fr.counts()
                hits = len(fr.findings)
                total_hits += hits
                done += 1
                detail = "、".join("%s×%d" % (k, v) for k, v in counts.items()) or "未偵測到個資"
                self._log_async("  [完成] %s → 輸出 %s" % (detail, out.name))
                for w in fr.warnings:
                    self._log_async("  [注意] %s" % w)
            except Exception as exc:
                failed += 1
                self._log_async("  [失敗] %s" % exc)
        self._log_async("-" * 50)
        self._log_async("完成：成功 %d、失敗 %d，共遮罩 %d 筆個資。" % (done, failed, total_hits))
        self.root.after(0, lambda: self.run_btn.configure(state="normal"))

    def _log_async(self, text: str):
        self.root.after(0, lambda: self.append_log(text))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
