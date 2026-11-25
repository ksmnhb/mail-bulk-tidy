# -*- coding: utf-8 -*-
"""メール一括整理 — 画面（tkinter）"""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

import core
from core import MailCleaner, ToolError

POP_WARNING = (
    "【大切な注意】このツールは、メールサーバーの受信箱にあるメールをゴミ箱フォルダへ移します。\n"
    "・Gmailやメールソフトが「POP」で受信している場合、まだ取り込まれていないメールは、"
    "移動後は取り込まれなくなります。\n"
    "・ゴミ箱に自動削除の設定があると、期限が来たメールは消えます。\n"
    "・ゴミ箱のメールも、メールボックスの容量に数えられます。"
)
BATCH_SIZES = (200, 1000)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cleaner = MailCleaner()
        self.folders = []
        self.result = None
        self.pending = []
        self.moved_total = 0
        self.busy = False
        self.q = queue.Queue()

        root.title(f"{core.APP_TITLE}  v{core.VERSION}")
        root.minsize(760, 720)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self._refresh_state()
        root.after(100, self._poll)

    # ---------- 画面づくり ----------
    def _build(self):
        pad = {"padx": 10, "pady": 6}
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        # 1. ログイン
        f1 = ttk.LabelFrame(outer, text="1. メールサーバーにログイン", padding=8)
        f1.pack(fill="x", **pad)
        ttk.Label(f1, text="サーバー名").grid(row=0, column=0, sticky="w")
        self.host = ttk.Entry(f1, width=40)
        self.host.grid(row=0, column=1, sticky="we", padx=6)
        ttk.Label(f1, text="例: xxxx.sakura.ne.jp（さくらは初期ドメイン）",
                  foreground="#666").grid(row=0, column=2, sticky="w")
        ttk.Label(f1, text="メールアドレス").grid(row=1, column=0, sticky="w", pady=4)
        self.user = ttk.Entry(f1, width=40)
        self.user.grid(row=1, column=1, sticky="we", padx=6)
        ttk.Label(f1, text="パスワード").grid(row=2, column=0, sticky="w")
        self.pw = ttk.Entry(f1, width=40, show="•")
        self.pw.grid(row=2, column=1, sticky="we", padx=6)
        self.btn_connect = ttk.Button(f1, text="ログイン", command=self.on_connect)
        self.btn_connect.grid(row=2, column=2, sticky="w")
        ttk.Label(f1, text="パスワードはどこにも保存しません。閉じると接続も切れます。",
                  foreground="#666").grid(row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))
        f1.columnconfigure(1, weight=1)

        # 2. 移動先
        f2 = ttk.LabelFrame(outer, text="2. 移動先（ゴミ箱フォルダ）を確認", padding=8)
        f2.pack(fill="x", **pad)
        self.trash_var = tk.StringVar()
        self.trash_box = ttk.Combobox(f2, textvariable=self.trash_var, state="disabled", width=40)
        self.trash_box.grid(row=0, column=0, sticky="w")
        self.trash_note = ttk.Label(f2, text="ログインすると候補が表示されます。", foreground="#666")
        self.trash_note.grid(row=0, column=1, sticky="w", padx=8)

        # 3. 検索
        f3 = ttk.LabelFrame(outer, text="3. 差出人のメールアドレスで探す（完全一致）", padding=8)
        f3.pack(fill="both", expand=True, **pad)
        row = ttk.Frame(f3)
        row.pack(fill="x")
        self.addr = ttk.Entry(row, width=44)
        self.addr.pack(side="left")
        self.addr.bind("<Return>", lambda e: self.on_search())
        self.btn_search = ttk.Button(row, text="検索", command=self.on_search)
        self.btn_search.pack(side="left", padx=6)
        self.search_info = ttk.Label(f3, text="", justify="left")
        self.search_info.pack(fill="x", pady=(6, 2))
        cols = ("date", "from", "subject")
        self.tree = ttk.Treeview(f3, columns=cols, show="headings", height=7)
        for c, label, w in zip(cols, ("日付", "差出人", "件名"), (170, 220, 320)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        ttk.Label(f3, text="↑ 移動対象の見本（先頭の数件）", foreground="#666").pack(anchor="w")

        # 4. 移動
        f4 = ttk.LabelFrame(outer, text="4. ゴミ箱へ移す（完全には削除しません）", padding=8)
        f4.pack(fill="x", **pad)
        ttk.Label(f4, text=POP_WARNING, wraplength=700, justify="left",
                  foreground="#a33").pack(anchor="w")
        self.agree = tk.BooleanVar(value=False)
        ttk.Checkbutton(f4, text="上の注意を理解しました", variable=self.agree,
                        command=self._refresh_state).pack(anchor="w", pady=4)
        row = ttk.Frame(f4)
        row.pack(fill="x")
        self.btn_moves = []
        for n in BATCH_SIZES:
            b = ttk.Button(row, text=f"{n}件 ゴミ箱へ移す", command=lambda n=n: self.on_move(n))
            b.pack(side="left", padx=(0, 8))
            self.btn_moves.append(b)
        self.move_info = ttk.Label(row, text="")
        self.move_info.pack(side="left", padx=8)
        ttk.Label(f4, text="はじめての方は、まず200件で試し、Webメールのゴミ箱に入ったことを確認してください。",
                  foreground="#666").pack(anchor="w", pady=(4, 0))

        # 状態表示
        self.status = ttk.Label(outer, text="", foreground="#245")
        self.status.pack(fill="x", padx=10)
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=(2, 0))
        ttk.Label(outer, text=f"記録ファイル: {core.LOG_FILE}", foreground="#888").pack(anchor="w", padx=10)

    # ---------- 状態の切り替え ----------
    def _refresh_state(self):
        connected = self.cleaner.imap is not None
        idle = not self.busy
        for w in (self.host, self.user, self.pw):
            w.configure(state="disabled" if connected or self.busy else "normal")
        self.btn_connect.configure(text="ログアウト" if connected else "ログイン",
                                   state="normal" if idle else "disabled")
        self.trash_box.configure(state="readonly" if connected and idle else "disabled")
        self.btn_search.configure(state="normal" if connected and idle else "disabled")
        can_move = connected and idle and self.agree.get() and bool(self.pending) and bool(self.trash_var.get())
        for b in self.btn_moves:
            b.configure(state="normal" if can_move else "disabled")
        if self.result:
            self.move_info.configure(text=f"残り {len(self.pending)} 件（このセッションで移動済み {self.moved_total} 件）")
        else:
            self.move_info.configure(text="")

    # ---------- 裏で動かす仕組み ----------
    def _run(self, label, work, done):
        self.busy = True
        self.status.configure(text=label)
        self.progress.start(12)
        self._refresh_state()

        def worker():
            try:
                value = work()
                self.q.put(lambda: self._finish(done, value, None))
            except Exception as e:  # noqa: BLE001
                self.q.put(lambda e=e: self._finish(done, None, e))
        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, done, value, err):
        self.busy = False
        self.progress.stop()
        self.progress["value"] = 0
        self.status.configure(text="")
        if err is None:
            done(value)
        elif isinstance(err, ToolError):
            messagebox.showerror(core.APP_TITLE, str(err))
        else:
            # 通信切断など想定外のエラー：接続を捨てて、やり直してもらう
            self.cleaner.logout()
            self._reset_search()
            messagebox.showerror(
                core.APP_TITLE,
                "メールサーバーとの接続が切れた可能性があります。\n"
                "もう一度ログインして、検索からやり直してください。\n"
                "ここまでに移したメールはゴミ箱にあります。もう一度検索すれば、"
                f"受信箱に残っている分だけが対象になります。\n\n（詳細: {type(err).__name__}）")
        self._refresh_state()

    def _poll(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ---------- ボタンの動作 ----------
    def on_connect(self):
        if self.cleaner.imap is not None:
            self.cleaner.logout()
            self._reset_search()
            self.folders = []
            self.trash_box.configure(values=[])
            self.trash_var.set("")
            self.trash_note.configure(text="ログインすると候補が表示されます。")
            self.status.configure(text="ログアウトしました。")
            self._refresh_state()
            return
        host, user, pw = self.host.get(), self.user.get(), self.pw.get()

        def work():
            self.cleaner.connect(host, user, pw)
            folders = self.cleaner.list_folders()
            return folders, MailCleaner.guess_trash(folders)

        def done(value):
            self.pw.configure(state="normal")
            self.pw.delete(0, "end")          # 画面からもパスワードを消す
            self.folders, guess = value
            self.trash_box.configure(values=[f.display for f in self.folders])
            if guess:
                self.trash_var.set(guess.display)
                self.trash_note.configure(text="自動で選んだ候補です。違う場合は選び直してください。")
            else:
                self.trash_note.configure(text="ゴミ箱を特定できませんでした。一覧から選んでください。")
            self.status.configure(text="ログインしました。")
        self._run("接続しています...", work, done)

    def on_search(self):
        if self.busy or self.cleaner.imap is None:
            return
        address = self.addr.get()

        def work():
            return self.cleaner.search(address)

        def done(res):
            self.result = res
            self.pending = list(res.uids)
            self.moved_total = 0
            lines = [f"サーバーの検索で見つかった件数: {res.candidates} 件",
                     f"アドレスが完全一致（移動対象）: {len(res.uids)} 件"]
            if res.excluded:
                lines.append(f"対象外: {sum(res.excluded.values())} 件（多い順）")
                for reason, n in res.excluded.most_common(5):
                    lines.append(f"    {n} 件  {reason}")
            self.search_info.configure(text="\n".join(lines))
            self.tree.delete(*self.tree.get_children())
            for row in res.preview:
                self.tree.insert("", "end", values=row)
            if len(res.uids) and len(res.uids) <= 100:
                self.status.configure(text="100件以下なので、Webメールでの操作でも整理できます。")
        self._run("検索しています（件数が多いと時間がかかります）...", work, done)

    def _trash_raw(self):
        for f in self.folders:
            if f.display == self.trash_var.get():
                return f.raw
        return None

    def on_move(self, size):
        trash = self._trash_raw()
        if not trash or not self.pending:
            return
        batch = self.pending[:size]
        ok = messagebox.askyesno(
            core.APP_TITLE,
            f"次の内容でゴミ箱へ移します。\n\n"
            f"差出人: {self.result.address}\n"
            f"件数: {len(batch)} 件（残り {len(self.pending)} 件中）\n"
            f"移動先: {self.trash_var.get()}\n\n"
            f"完全には削除しません。よろしいですか？",
            default="no")
        if not ok:
            return
        address = self.result.address

        def work():
            return self.cleaner.move_batch(address, batch, trash)

        def done(r):
            if r.ok:
                self.pending = self.pending[len(batch):]
                self.moved_total += len(batch)
                self.status.configure(
                    text=f"{len(batch)} 件をゴミ箱へ移し、受信箱から消えたこととゴミ箱の増加を確認しました。")
                if not self.pending:
                    messagebox.showinfo(core.APP_TITLE,
                                        f"{address} のメールをすべてゴミ箱へ移しました（{self.moved_total} 件）。")
            else:
                self.pending = []
                messagebox.showwarning(
                    core.APP_TITLE,
                    f"件数が一致しないため、ここで止めました。\n\n"
                    f"指示した件数: {r.requested}\n受信箱から消えた件数: {r.removed}\n"
                    f"ゴミ箱で増えた件数: {r.trash_delta}\n\n"
                    "ほかのメールソフトが同時に操作していると起こることがあります。"
                    "Webメールで受信箱とゴミ箱を確認してから、もう一度検索してください。")
        self._run(f"{len(batch)} 件をゴミ箱へ移しています...", work, done)

    def _reset_search(self):
        self.result = None
        self.pending = []
        self.moved_total = 0
        self.search_info.configure(text="")
        self.tree.delete(*self.tree.get_children())

    def on_close(self):
        if self.busy and not messagebox.askyesno(
                core.APP_TITLE, "処理中です。終了しますか？\n（移動中の分は、次回検索すると状況が分かります）"):
            return
        self.cleaner.logout()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("aqua" if root.tk.call("tk", "windowingsystem") == "aqua" else "vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
