"""テスト用の偽IMAPサーバー（ネットにはつながない）"""


def expand(s):
    out = []
    for p in str(s).split(","):
        if ":" in p:
            a, b = p.split(":")
            out += range(int(a), int(b) + 1)
        else:
            out.append(int(p))
    return out


class FakeIMAP:
    def __init__(self, messages, caps="IMAP4rev1 MOVE UIDPLUS", password="pw", trash_drift=0):
        self.inbox = dict(messages)      # uid -> From ヘッダのバイト列
        self.trash = 0
        self.deleted = set()
        self.caps = caps
        self.password = password
        self.trash_drift = trash_drift   # ゴミ箱件数の食い違いを起こす
        self.commands = []
        self.capabilities = ("IMAP4REV1",)

    def login(self, user, pw):
        import imaplib
        if pw != self.password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b""]

    def capability(self):
        return "OK", [self.caps.encode()]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "." INBOX',
                      b'(\\HasNoChildren \\Trash) "." Trash',
                      b'(\\HasNoChildren) "." "&MLQw33ux-"']

    def select(self, f):
        return "OK", [str(len(self.inbox)).encode()]

    def status(self, f, q):
        return "OK", [f"Trash (MESSAGES {self.trash})".encode()]

    def logout(self):
        return "BYE", [b""]

    def uid(self, cmd, *a):
        self.commands.append((cmd, a))
        if cmd == "SEARCH":
            if a[1] == "FROM":
                needle = a[2].strip('"').encode()
                ids = [u for u, h in self.inbox.items() if needle in h.lower()]
            else:
                ids = [u for u in expand(a[2]) if u in self.inbox]
            return "OK", [" ".join(map(str, sorted(ids))).encode()]
        if cmd == "FETCH":
            out = []
            for u in expand(a[0]):
                if u in self.inbox:
                    hdr = self.inbox[u] + b"\r\nSubject: test\r\nDate: Mon, 1 Jan 2024 00:00:00 +0900\r\n\r\n"
                    out.append((f"{u} (UID {u} BODY[...] {{{len(hdr)}}}".encode(), hdr))
                    out.append(b")")
            return "OK", out
        if cmd == "MOVE":
            for u in expand(a[0]):
                if u in self.inbox:
                    del self.inbox[u]
                    self.trash += 1
            self.trash += self.trash_drift
            return "OK", [b""]
        if cmd == "COPY":
            self.trash += sum(1 for u in expand(a[0]) if u in self.inbox)
            return "OK", [b""]
        if cmd == "STORE":
            self.deleted.update(expand(a[0]))
            return "OK", [b""]
        if cmd == "EXPUNGE":
            for u in expand(a[0]):
                if u in self.deleted and u in self.inbox:
                    del self.inbox[u]
            return "OK", [b""]
        raise AssertionError(f"unexpected {cmd}")
