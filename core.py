# -*- coding: utf-8 -*-
"""
メール一括整理ツール — メールサーバー操作の中心部分（画面なし）

・IMAPで一時的に接続し、終わったら切断する（設定やパスワードは保存しない）
・証明書を検証した暗号化接続のみ
・差出人アドレスの完全一致で対象を決める（From欄に複数アドレス・解析不能なものは対象外）
・ゴミ箱へ「移動」するだけ。完全削除・ゴミ箱を空にする処理は持たない
・移動のたびに「受信箱から消えたか」「ゴミ箱の件数が増えたか」を確認する
"""

import base64
import csv
import imaplib
import os
import re
import ssl
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import getaddresses
from pathlib import Path

APP_NAME = "MailBulkTidy"
APP_TITLE = "メール一括整理"
VERSION = "0.1.0"

SOURCE_FOLDER = "INBOX"
FETCH_CHUNK = 1000          # 差出人確認のために一度に読むヘッダの件数
MAX_UIDSET_LEN = 4000       # 1コマンドに載せるUID指定の最大文字数
PREVIEW_COUNT = 20
TIMEOUT_SEC = 60

LOG_DIR = Path.home() / APP_NAME
LOG_FILE = LOG_DIR / "move_log.csv"

imaplib.Commands.setdefault("MOVE", ("SELECTED",))
imaplib.Commands.setdefault("EXPUNGE", ("SELECTED",))  # UID EXPUNGE 用


class ToolError(Exception):
    """利用者にそのまま見せてよいエラー"""


# ---------- 小さな道具 ----------

def decode_folder_name(name: str) -> str:
    """IMAPのフォルダ名（修正UTF-7）を表示用の日本語に戻す"""
    def repl(m):
        s = m.group(1)
        if s == "":
            return "&"
        s = s.replace(",", "/")
        s += "=" * (-len(s) % 4)
        try:
            return base64.b64decode(s).decode("utf-16-be")
        except Exception:
            return m.group(0)
    return re.sub(r"&([^-]*)-", repl, name)


def compress_uids(uids) -> str:
    """[1,2,3,7,8] → '1:3,7:8'"""
    nums = sorted({int(u) for u in uids})
    parts, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        parts.append(f"{start}:{prev}" if start != prev else str(start))
        start = prev = n
    parts.append(f"{start}:{prev}" if start != prev else str(start))
    return ",".join(parts)


def split_uid_sets(uids, max_len=MAX_UIDSET_LEN):
    """UID指定が長くなりすぎないよう、いくつかのコマンドに分ける"""
    nums = sorted({int(u) for u in uids})
    groups, current = [], []
    for n in nums:
        trial = current + [n]
        if current and len(compress_uids(trial)) > max_len:
            groups.append(current)
            current = [n]
        else:
            current = trial
    if current:
        groups.append(current)
    return [compress_uids(g) for g in groups]


def to_text(raw: bytes) -> str:
    try:
        return str(make_header(decode_header(raw.decode("ascii", "replace"))))
    except Exception:
        return raw.decode("utf-8", "replace")


def quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def check(typ, data, what):
    if typ != "OK":
        raise ToolError(f"{what} に失敗しました（サーバーの応答: {_short(data)}）")
    return data


def _short(data) -> str:
    try:
        text = b" ".join(d if isinstance(d, bytes) else str(d).encode() for d in data if d)
        return text.decode("utf-8", "replace")[:200]
    except Exception:
        return "不明"


def parse_uid_fetch(data):
    """FETCHの結果 → {UID(bytes): ヘッダのバイト列}"""
    result = {}
    for item in data:
        if not isinstance(item, tuple):
            continue
        m = re.search(rb"UID (\d+)", item[0])
        if m:
            result[m.group(1)] = item[1]
    return result


def header_fields(raw: bytes):
    headers = {}
    for line in re.split(rb"\r?\n(?![ \t])", raw):
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower()] = to_text(re.sub(rb"\r?\n[ \t]+", b" ", v.strip()))
    return headers


def classify_from(from_text: str, target: str):
    """From欄を判定 → (対象か, 対象外の理由)"""
    addrs = [a.strip().lower() for _, a in getaddresses([from_text]) if a.strip()]
    if not addrs:
        return False, "差出人を読み取れない"
    if len(addrs) > 1:
        return False, "差出人が複数: " + ", ".join(addrs)[:80]
    if "@" not in addrs[0]:
        return False, "差出人の形式が不正: " + addrs[0][:80]
    if addrs[0] == target:
        return True, ""
    return False, addrs[0]


def make_ssl_context():
    """証明書を検証する暗号化設定（アプリ化しても証明書が見つかるよう certifi を優先）"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ---------- ログ ----------

def _csv_safe(value):
    """表計算ソフトで開いたときに数式として動かないようにする"""
    s = str(value)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


def write_log(sender, requested, removed, trash_delta, result, log_file: Path = None):
    log_file = Path(log_file or LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    new_file = not log_file.exists()
    with open(log_file, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["日時", "差出人", "移動を指示した件数",
                        "受信箱から消えた件数", "ゴミ箱で増えた件数", "結果"])
        w.writerow([_csv_safe(x) for x in (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            sender, requested, removed, trash_delta, result)])
    if os.name == "posix":
        try:
            os.chmod(log_file, 0o600)   # 本人だけが読めるように
        except OSError:
            pass


# ---------- 結果の入れ物 ----------

@dataclass
class Folder:
    raw: str          # サーバー上の名前
    display: str      # 表示名
    is_trash_flag: bool


@dataclass
class SearchResult:
    address: str
    candidates: int
    uids: list
    excluded: Counter = field(default_factory=Counter)
    preview: list = field(default_factory=list)   # [(日付, 差出人, 件名)]


@dataclass
class MoveResult:
    requested: int
    removed: int
    trash_delta: int

    @property
    def ok(self):
        return self.removed == self.requested and self.trash_delta == self.requested


# ---------- 本体 ----------

class MailCleaner:
    def __init__(self, imap_factory=None, log_file: Path = None):
        self._factory = imap_factory or (
            lambda host: imaplib.IMAP4_SSL(host, 993, ssl_context=make_ssl_context(),
                                           timeout=TIMEOUT_SEC))
        self.imap = None
        self.caps = set()
        self.log_file = log_file

    # 接続 -------------------------------------------------
    def connect(self, host: str, user: str, password: str):
        host, user = host.strip(), user.strip()
        if not host or not user or not password:
            raise ToolError("サーバー名・メールアドレス・パスワードをすべて入力してください。")
        try:
            self.imap = self._factory(host)
        except ssl.SSLCertVerificationError:
            raise ToolError(
                "サーバーの証明書を確認できませんでした。\n"
                "サーバー名が正しいか確認してください。さくらの場合は独自ドメインではなく、"
                "初期ドメイン（xxxx.sakura.ne.jp）を入力してください。")
        except Exception as e:
            raise ToolError(f"サーバーに接続できませんでした（{type(e).__name__}）。"
                            "サーバー名とネット接続を確認してください。")
        try:
            self.imap.login(user, password)
        except imaplib.IMAP4.error:
            self._safe_logout()
            raise ToolError("ログインできませんでした。メールアドレスとパスワードを確認してください。\n"
                            "※何度も失敗するとサーバー側で一時的に接続が制限されることがあります。")
        self.caps = self._capabilities()
        if "MOVE" not in self.caps and "UIDPLUS" not in self.caps:
            self._safe_logout()
            raise ToolError("このメールサーバーは、他のメールを巻き込まずに移動する機能"
                            "（MOVE / UIDPLUS）に対応していないため、利用できません。")

    def _capabilities(self):
        typ, dat = self.imap.capability()
        if typ == "OK" and dat and dat[-1]:
            return set(dat[-1].decode("ascii", "replace").upper().split())
        return {c.upper() for c in getattr(self.imap, "capabilities", ())}

    def logout(self):
        self._safe_logout()

    def _safe_logout(self):
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:
                pass
        self.imap = None

    # フォルダ ---------------------------------------------
    def list_folders(self):
        data = check(*self.imap.list(), "フォルダ一覧の取得")
        folders = []
        for line in data:
            if not line:
                continue
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            m = re.match(r'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$', text)
            if not m or "\\Noselect" in m.group("flags"):
                continue
            name = m.group("name").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            folders.append(Folder(name, decode_folder_name(name), "\\Trash" in m.group("flags")))
        return folders

    @staticmethod
    def guess_trash(folders):
        """候補を推測するだけ。最終決定は利用者が画面で行う"""
        for f in folders:
            if f.is_trash_flag:
                return f
        for f in folders:
            if re.search(r"trash|ゴミ箱|ごみ箱", f.display, re.I):
                return f
        return None

    # 検索 -------------------------------------------------
    def search(self, address: str, progress=None) -> SearchResult:
        target = address.strip().lower()
        if "@" not in target or " " in target:
            raise ToolError("メールアドレス全体（@を含む）を入力してください。")
        check(*self.imap.select(SOURCE_FOLDER), "受信箱を開く")
        data = check(*self.imap.uid("SEARCH", None, "FROM", quote(target)), "検索")
        candidates = data[0].split() if data and data[0] else []

        uids, excluded = [], Counter()
        for i in range(0, len(candidates), FETCH_CHUNK):
            chunk = candidates[i:i + FETCH_CHUNK]
            if progress:
                progress(i, len(candidates))
            fdata = check(*self.imap.uid("FETCH", compress_uids(chunk),
                                         "(UID BODY.PEEK[HEADER.FIELDS (FROM)])"),
                          "差出人の確認")
            for uid, raw in parse_uid_fetch(fdata).items():
                ok, reason = classify_from(header_fields(raw).get(b"from", ""), target)
                if ok:
                    uids.append(uid)
                else:
                    excluded[reason] += 1
        uids.sort(key=int)
        return SearchResult(target, len(candidates), uids, excluded, self.preview(uids))

    def preview(self, uids, count=PREVIEW_COUNT):
        if not uids:
            return []
        typ, data = self.imap.uid("FETCH", compress_uids(uids[:count]),
                                  "(UID BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT)])")
        if typ != "OK":
            return []
        rows = []
        for _, raw in sorted(parse_uid_fetch(data).items(), key=lambda x: int(x[0])):
            h = header_fields(raw)
            rows.append((h.get(b"date", ""), h.get(b"from", ""), h.get(b"subject", "")))
        return rows

    # 移動 -------------------------------------------------
    def folder_count(self, folder: str) -> int:
        typ, data = self.imap.status(quote(folder), "(MESSAGES)")
        check(typ, data, "フォルダ件数の取得")
        m = re.search(rb"MESSAGES (\d+)", data[0] if isinstance(data[0], bytes) else str(data[0]).encode())
        if not m:
            raise ToolError("フォルダ件数を読み取れませんでした。")
        return int(m.group(1))

    def move_batch(self, address: str, uids, trash: str) -> MoveResult:
        if not uids:
            raise ToolError("移動するメールがありません。")
        check(*self.imap.select(SOURCE_FOLDER), "受信箱を開く")
        before = self.folder_count(trash)
        for uid_set in split_uid_sets(uids):
            if "MOVE" in self.caps:
                check(*self.imap.uid("MOVE", uid_set, quote(trash)), "ゴミ箱への移動")
            else:
                # コピー → 今回の分だけ削除印 → 今回の分だけ除去（UID EXPUNGE）
                check(*self.imap.uid("COPY", uid_set, quote(trash)), "ゴミ箱へのコピー")
                check(*self.imap.uid("STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)"), "削除印")
                check(*self.imap.uid("EXPUNGE", uid_set), "受信箱からの除去")
        remaining = 0
        for uid_set in split_uid_sets(uids):
            data = check(*self.imap.uid("SEARCH", None, "UID", uid_set), "移動後の確認")
            remaining += len(data[0].split()) if data and data[0] else 0
        result = MoveResult(len(uids), len(uids) - remaining, self.folder_count(trash) - before)
        write_log(address, result.requested, result.removed, result.trash_delta,
                  "OK" if result.ok else "要確認", self.log_file)
        return result
