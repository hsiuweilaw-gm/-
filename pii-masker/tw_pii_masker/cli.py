# -*- coding: utf-8 -*-
"""命令列介面。

用法範例：
    twmask 保單.docx                      # 產生 保單_masked.docx
    twmask 客戶名單.xlsx 理賠.pdf         # 一次處理多個檔案
    twmask ~/文件/待遮罩 -r               # 遞迴處理整個資料夾
    twmask 保單.docx -m label             # 以 [身分證字號] 這類標籤取代
    twmask 保單.docx --dry-run            # 只偵測、不輸出檔案
    twmask 保單.docx --report result.json # 另存 JSON 報告
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Dict, List

from . import __version__
from .detectors import ALL_TYPES
from .engine import MaskingEngine
from .report import Report
from .handlers.docx_handler import mask_docx
from .handlers.pdf_handler import mask_pdf
from .handlers.text_handler import mask_text_file
from .handlers.xlsx_handler import mask_xlsx

HANDLERS: Dict[str, Callable] = {
    ".docx": mask_docx,
    ".xlsx": mask_xlsx,
    ".xlsm": mask_xlsx,
    ".pdf": mask_pdf,
    ".txt": mask_text_file,
    ".csv": mask_text_file,
}

_LEGACY_HINT = {
    ".doc": "請先用 Word 另存為 .docx 再處理",
    ".xls": "請先用 Excel 另存為 .xlsx 再處理",
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="twmask",
        description="台灣個資檔案遮罩工具（符合個資法之去識別化輔助，全程地端執行、不連網）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="支援格式：.docx .xlsx .xlsm .pdf .txt .csv\n"
               "個資類型清單請執行：twmask --list-types",
    )
    p.add_argument("paths", nargs="*", help="要處理的檔案或資料夾")
    p.add_argument("-o", "--out-dir", help="輸出資料夾（預設與原檔同資料夾）")
    p.add_argument("--suffix", default="_masked", help="輸出檔名後綴（預設 _masked）")
    p.add_argument("-m", "--mode", choices=("partial", "full", "label"),
                   default="partial",
                   help="遮罩模式：partial 部分遮罩（預設）／full 全遮罩／label 類型標籤")
    p.add_argument("-t", "--types", help="只啟用指定類型（逗號分隔，如 national_id,mobile）")
    p.add_argument("-x", "--exclude-types", help="停用指定類型（逗號分隔）")
    p.add_argument("--all-dates", action="store_true",
                   help="遮罩所有完整日期（預設只遮出生日期相關）")
    p.add_argument("--dry-run", action="store_true", help="只偵測並顯示結果，不輸出檔案")
    p.add_argument("--report", metavar="FILE", help="另存 JSON 報告檔")
    p.add_argument("--show-original", action="store_true",
                   help="JSON 報告中包含原始個資內容（報告檔請妥善保管！）")
    p.add_argument("--keep-metadata", action="store_true",
                   help="保留文件屬性中的作者等中繼資料（預設會清除）")
    p.add_argument("-r", "--recursive", action="store_true", help="遞迴處理資料夾")
    p.add_argument("--list-types", action="store_true", help="列出所有可偵測的個資類型")
    p.add_argument("-V", "--version", action="version", version="twmask " + __version__)
    return p


def collect_files(paths: List[str], recursive: bool) -> List[Path]:
    files: List[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            it = path.rglob("*") if recursive else path.glob("*")
            for f in sorted(it):
                if f.is_file() and f.suffix.lower() in HANDLERS \
                        and not f.stem.endswith("_masked"):
                    files.append(f)
        elif path.is_file():
            files.append(path)
        else:
            print("找不到檔案或資料夾：%s" % path, file=sys.stderr)
    return files


def output_path_for(src: Path, out_dir: str, suffix: str) -> Path:
    directory = Path(out_dir).expanduser() if out_dir else src.parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (src.stem + suffix + src.suffix)


def run(argv: List[str] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_types:
        print("可偵測的個資類型（--types / --exclude-types 使用機器名稱）：")
        for name, label in ALL_TYPES.items():
            print("  %-16s %s" % (name, label))
        return 0

    if not args.paths:
        build_parser().print_help()
        return 1

    types = [t.strip() for t in args.types.split(",")] if args.types else None
    exclude = [t.strip() for t in args.exclude_types.split(",")] if args.exclude_types else None
    try:
        engine = MaskingEngine(types=types, exclude=exclude,
                               mode=args.mode, all_dates=args.all_dates)
    except ValueError as exc:
        print("參數錯誤：%s" % exc, file=sys.stderr)
        return 2

    files = collect_files(args.paths, args.recursive)
    if not files:
        print("沒有可處理的檔案。", file=sys.stderr)
        return 1

    report = Report(show_original=args.show_original, mode=args.mode)
    failed = 0
    for src in files:
        report.start_file(str(src))
        ext = src.suffix.lower()
        handler = HANDLERS.get(ext)
        if handler is None:
            hint = _LEGACY_HINT.get(ext, "不支援的格式")
            report.set_error("%s：%s" % (ext, hint))
            failed += 1
            continue
        try:
            if args.dry_run:
                _dry_run(src, handler, engine, report)
            else:
                out = output_path_for(src, args.out_dir, args.suffix)
                if out.resolve() == src.resolve():
                    report.set_error("輸出檔與原始檔相同，已跳過（請改用 --suffix 或 -o）")
                    failed += 1
                    continue
                handler(str(src), str(out), engine, report,
                        keep_metadata=args.keep_metadata)
                report.set_output(str(out))
        except Exception as exc:
            report.set_error(str(exc))
            failed += 1

    report.print_summary()
    if args.dry_run:
        print("（--dry-run 模式：未輸出任何檔案）")
    if args.report:
        report.save_json(args.report)
        print("JSON 報告已存至：%s" % args.report)
        if args.show_original:
            print("[注意] 報告內含原始個資，請妥善保管、用畢刪除。")
    return 1 if failed else 0


def _dry_run(src: Path, handler: Callable, engine: MaskingEngine,
             report: Report) -> None:
    """dry-run：實際跑一次處理流程，但輸出到暫存檔後即刪除。"""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_out = Path(tmp) / ("out" + src.suffix)
        handler(str(src), str(tmp_out), engine, report, keep_metadata=True)


def main() -> None:
    if sys.platform == "win32":
        # Windows 主控台編碼（cp950）遇到無法顯示的字元時以 ? 取代，避免中斷
        try:
            sys.stdout.reconfigure(errors="replace")
            sys.stderr.reconfigure(errors="replace")
        except Exception:
            pass
    code = run()
    # 打包成 .exe 後，拖曳檔案或雙擊執行時讓使用者看得到結果再關閉視窗
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        try:
            input("\n按 Enter 鍵關閉視窗...")
        except Exception:  # 無互動式輸入（如 CI）時直接結束
            pass
    sys.exit(code)


if __name__ == "__main__":
    main()
