# -*- coding: utf-8 -*-
"""PyInstaller 打包進入點：圖形介面版執行檔。

支援 --selftest：只確認圖形介面所需的 tkinter 已正確打包後即結束，
供建置流程在無顯示器的環境驗證產出的執行檔（macOS 尤其需要，
Tcl/Tk 若沒被一起打包，使用者要到雙擊時才會發現打不開）。
"""
import sys

if "--selftest" in sys.argv:
    import tkinter
    print("selftest ok: tkinter %s" % tkinter.TkVersion)
    sys.exit(0)

from tw_pii_masker.gui import main

main()
