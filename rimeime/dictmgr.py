"""词库管理界面：查看/搜索/添加/删除/导出 + 保存并重启输入法。

    python -m rimeime.dictmgr        # 独立窗口（托盘菜单也会拉起本进程）

数据源：runtime/rime_user/shangkey.dict.yaml（经 importer 托管）。
「应用」流程：写盘 -> 若 IME 在运行则 Ctrl+Alt+Q 停止 -> 独立重新部署
-> 重新拉起 IME。全程后台线程，UI 不冻结。
"""

import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from . import autostart, importer
from .paths import PROJECT_ROOT


class DictManager:
    def __init__(self):
        self.entries = importer.load_managed_entries()  # {(词, 拼音): 词频}
        self.dirty = False
        self.root = tk.Tk()
        self.root.title("ShangKey 词库管理")
        self.root.geometry("720x520")
        self.root.minsize(560, 420)
        self._build()
        self._reload_tree()

    # ---------------------------------------------------------------- UI

    def _build(self):
        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill="x")
        ttk.Label(top, text="搜索:").pack(side="left")
        self.var_search = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.var_search)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        self.var_search.trace_add("write", lambda *_: self._reload_tree())
        self.var_count = tk.StringVar(value="")
        ttk.Label(top, textvariable=self.var_count).pack(side="right")

        mid = ttk.Frame(self.root, padding=(8, 0, 8, 4))
        mid.pack(fill="both", expand=True)
        cols = ("word", "pinyin", "weight")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings")
        for cid, text, w, anchor in (
                ("word", "词条", 200, "w"),
                ("pinyin", "拼音", 260, "w"),
                ("weight", "词频", 80, "e")):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor=anchor)
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Delete>", lambda e: self._delete())

        btns = ttk.Frame(self.root, padding=(8, 4))
        btns.pack(fill="x")
        ttk.Button(btns, text="添加…", command=self._add_dialog).pack(
            side="left")
        ttk.Button(btns, text="删除选中", command=self._delete).pack(
            side="left", padx=6)
        ttk.Button(btns, text="导出 txt…", command=self._export).pack(
            side="left")
        self.var_status = tk.StringVar(value="")
        ttk.Label(btns, textvariable=self.var_status).pack(
            side="left", padx=12)
        ttk.Checkbutton(
            btns, text="开机自启", command=self._toggle_autostart,
            variable=tk.BooleanVar(value=autostart.is_enabled()),
            name="autostart_check").pack(side="right")

        bottom = ttk.Frame(self.root, padding=(8, 4, 8, 8))
        bottom.pack(fill="x")
        ttk.Button(bottom, text="应用并重启输入法",
                   command=self._apply).pack(side="right")
        ttk.Button(bottom, text="关闭", command=self.root.destroy).pack(
            side="right", padx=6)

    def _reload_tree(self):
        q = self.var_search.get().strip().lower()
        q_compact = q.replace(" ", "")  # nihao 也能搜到拼音 "ni hao" 的词
        rows = sorted(self.entries.items(),
                      key=lambda kv: (-kv[1], kv[0][0]))
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for (word, pinyin), weight in rows:
            if q and q not in word.lower() and q_compact not in pinyin.replace(" ", ""):
                continue
            self.tree.insert("", "end", values=(word, pinyin, weight))
            shown += 1
        self.var_count.set(f"共 {len(rows)} 条" + (f"，显示 {shown} 条" if q else ""))

    def _set_status(self, text):
        self.var_status.set(text)
        self.root.update_idletasks()

    # ------------------------------------------------------------- 操作

    def _selected_keys(self):
        sel = []
        for iid in self.tree.selection():
            word, pinyin, _w = self.tree.item(iid, "values")
            sel.append((word, pinyin))
        return sel

    def _delete(self):
        sel = self._selected_keys()
        if not sel:
            return
        if not messagebox.askyesno("删除",
                                   f"确定删除选中的 {len(sel)} 条词条？"):
            return
        for key in sel:
            self.entries.pop(key, None)
        self.dirty = True
        self._reload_tree()

    def _add_dialog(self):
        dlg = tk.Toplevel(self.root)
        dlg.title("添加词条")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()
        frm = ttk.Frame(dlg, padding=12)
        frm.pack()
        v_word, v_py, v_wt = (tk.StringVar(), tk.StringVar(),
                              tk.StringVar(value=str(importer.DEFAULT_WEIGHT)))
        for r, (label, var, width) in enumerate((
                ("词条:", v_word, 24), ("拼音(留空自动):", v_py, 24),
                ("词频:", v_wt, 10))):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="e",
                                            pady=3)
            ttk.Entry(frm, textvariable=var, width=width).grid(
                row=r, column=1, pady=3, sticky="w")
        v_word.trace_add("write", lambda *_: self._suggest_pinyin(v_word, v_py))

        def ok():
            word = v_word.get().strip()
            if not word:
                return
            py = importer.normalize_pinyin(v_py.get().strip(), word)
            if not py:
                py = importer.normalize_pinyin(
                    " ".join(importer.lazy_pinyin(word)) if importer.lazy_pinyin
                    else "", word)
            if not py:
                messagebox.showerror("错误", f"无法确定「{word}」的拼音，请手工填写",
                                     parent=dlg)
                return
            try:
                weight = int(float(v_wt.get().strip() or
                                   importer.DEFAULT_WEIGHT))
            except ValueError:
                weight = importer.DEFAULT_WEIGHT
            self.entries[(word, py)] = weight
            self.dirty = True
            self.var_search.set("")
            self._reload_tree()
            dlg.destroy()

        frm.columnconfigure(1, weight=1)
        ttk.Button(frm, text="确定", command=ok).grid(row=3, column=0,
                                                     columnspan=2, pady=(8, 0))
        dlg.wait_visibility()
        v_word_focus = frm.winfo_children()[1]
        v_word_focus.focus_set()

    @staticmethod
    def _suggest_pinyin(v_word, v_py):
        """词条输入后拼音留空时，自动给出预览（仅当用户没手填）。"""
        if v_py.get().strip():
            return
        word = v_word.get().strip()
        if word and importer.lazy_pinyin and importer.has_cjk(word):
            v_py.set(" ".join(importer.lazy_pinyin(word)))

    def _export(self):
        path = filedialog.asksaveasfilename(
            title="导出词库", defaultextension=".txt",
            initialfile="shangkey_dict.txt",
            filetypes=[("文本文件", "*.txt"), ("全部文件", "*.*")])
        if not path:
            return
        rows = sorted(self.entries.items(),
                      key=lambda kv: (-kv[1], kv[0][0]))
        Path(path).write_text(
            "\n".join(f"{w}\t{p}\t{int(wt)}" for (w, p), wt in rows) + "\n",
            encoding="utf-8")
        self._set_status(f"已导出 {len(rows)} 条 -> {Path(path).name}")

    def _toggle_autostart(self):
        chk = self.root.nametowidget("autostart_check")
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable()
        chk.configure(variable=tk.BooleanVar(value=autostart.is_enabled()))

    # --------------------------------------------------------- 应用/重启

    def _apply(self):
        if not self.dirty:
            messagebox.showinfo("提示", "词库没有改动。")
            return
        if not messagebox.askyesno(
                "应用", "写入词库并重新部署？\n若输入法正在运行会先退出再自动重启。"):
            return
        importer.write_managed_dict(self.entries)
        importer.ensure_schema_patch()
        self.dirty = False
        threading.Thread(target=self._deploy_thread, daemon=True).start()

    def _deploy_thread(self):
        try:
            self.root.after(0, lambda: self._set_status("正在部署…"))
            running = importer.ime_running()
            if running:
                self.root.after(0, lambda: self._set_status(
                    "正在退出输入法…"))
                if not importer.stop_ime():
                    self.root.after(0, lambda: messagebox.showerror(
                        "错误", "输入法未能退出，部署取消。"))
                    return
            _r, elapsed = importer.redeploy()
            msg = f"部署完成（{elapsed:.1f}s）"
            if running:
                self.root.after(0, lambda: self._set_status("正在重启输入法…"))
                self._start_ime()
                msg += "，输入法已重启"
            self.root.after(0, lambda: self._set_status(msg))
        except Exception as ex:
            err = str(ex)
            self.root.after(0, lambda: messagebox.showerror("部署失败", err))

    @staticmethod
    def _start_ime():
        """拉起新的 IME 进程（与 autostart 同一命令形态）。"""
        cmd = autostart.launch_command()
        flags = 0x00000008  # DETACHED_PROCESS：脱离当前控制台
        subprocess.Popen(cmd, shell=True, creationflags=flags,
                         close_fds=True)
        time.sleep(0.5)  # 给新进程一点启动时间，避免锁竞争提示误报

    def run(self):
        self.root.mainloop()


def main():
    app = DictManager()
    app.run()


if __name__ == "__main__":
    main()
