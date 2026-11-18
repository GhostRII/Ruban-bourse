"""Ticker Bandeau — appli PC pour piloter le bandeau boursier du Raspberry Pi en SSH.

Se connecte au Pi comme PuTTY, lit et modifie ~/ticker/tickers.json par SFTP,
puis demande au serveur du bandeau de se rafraîchir. Les cours affichés à côté
de chaque symbole viennent directement du Pi (/api/quotes).
"""
import json
import queue
import sys
from pathlib import Path

import paramiko
from PySide6.QtCore import QRect, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

APP_NAME = "Ticker Bandeau"
CONF_FILE = Path.home() / ".ticker_gui.json"
KNOWN_HOSTS = Path.home() / ".ticker_gui_known_hosts"
REMOTE_FILE = "ticker/tickers.json"          # relatif au dossier perso sur le Pi
API = "http://localhost:8050"                # le serveur du bandeau, vu depuis le Pi

UP, DN, MUT, TXT = QColor("#1fd67a"), QColor("#ff4d5e"), QColor("#7d8896"), QColor("#e8edf3")
ROLE_SYM, ROLE_PX, ROLE_CH, ROLE_CUR = Qt.UserRole, Qt.UserRole + 1, Qt.UserRole + 2, Qt.UserRole + 3


class UnknownHost(Exception):
    def __init__(self, host, fingerprint):
        super().__init__(f"Hôte inconnu {host}")
        self.host, self.fingerprint = host, fingerprint


class _RejectAndReport(paramiko.MissingHostKeyPolicy):
    """Refuse une clé d'hôte inconnue mais la mémorise pour demander à l'utilisateur."""
    def __init__(self):
        self.key = None

    def missing_host_key(self, client, hostname, key):
        self.key = key
        raise UnknownHost(hostname, key.get_fingerprint().hex(":"))


class SSHWorker(QThread):
    """Toutes les opérations SSH passent par ce thread, une à la fois : l'UI ne gèle jamais."""
    done = Signal(str, object)
    failed = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.jobs = queue.Queue()
        self.client = None
        self.home = None
        self.pending_key = None

    def submit(self, name, *args):
        self.jobs.put((name, args))

    def run(self):
        while True:
            name, args = self.jobs.get()
            if name == "quit":
                break
            try:
                self.done.emit(name, getattr(self, "job_" + name)(*args))
            except Exception as e:  # noqa: BLE001 — on remonte tout à l'UI
                self.failed.emit(name, e)
        if self.client:
            self.client.close()

    # ---- jobs ----
    def job_connect(self, p, trust_new=False):
        if self.client:
            self.client.close()
            self.client = None
        c = paramiko.SSHClient()
        if KNOWN_HOSTS.exists():
            c.load_host_keys(str(KNOWN_HOSTS))
        policy = _RejectAndReport()
        if trust_new and self.pending_key is not None:
            c.get_host_keys().add(self._hk_name(p), self.pending_key.get_name(), self.pending_key)
        c.set_missing_host_key_policy(policy)
        try:
            c.connect(p["host"], port=p["port"], username=p["user"],
                      password=p["password"] or None,
                      key_filename=p["key"] or None,
                      look_for_keys=True, allow_agent=True, timeout=8)
        except UnknownHost:
            self.pending_key = policy.key
            raise
        KNOWN_HOSTS.touch(exist_ok=True)
        c.save_host_keys(str(KNOWN_HOSTS))
        self.client, self.pending_key = c, None
        sftp = c.open_sftp()
        self.home = sftp.normalize(".")
        sftp.close()
        return self._read_list()

    @staticmethod
    def _hk_name(p):
        return p["host"] if p["port"] == 22 else f"[{p['host']}]:{p['port']}"

    def _exec(self, cmd):
        _, out, err = self.client.exec_command(cmd, timeout=15)
        code = out.channel.recv_exit_status()
        return code, out.read().decode(errors="replace"), err.read().decode(errors="replace")

    def _read_list(self):
        sftp = self.client.open_sftp()
        try:
            with sftp.open(f"{self.home}/{REMOTE_FILE}") as f:
                return json.loads(f.read().decode())
        except FileNotFoundError:
            raise RuntimeError("~/ticker/tickers.json introuvable sur le Pi — le bandeau est-il installé ?")
        finally:
            sftp.close()

    def job_load(self):
        return self._read_list()

    def job_save(self, syms):
        path = f"{self.home}/{REMOTE_FILE}"
        sftp = self.client.open_sftp()
        try:
            with sftp.open(path + ".tmp", "w") as f:       # écriture atomique : .tmp puis renommage
                f.write(json.dumps(syms))
            sftp.posix_rename(path + ".tmp", path)
        finally:
            sftp.close()
        # On prévient le serveur pour qu'il recharge tout de suite (sinon il le fait seul sous 60 s)
        code, out, _ = self._exec(
            f"curl -s -m 5 -X POST -H 'Content-Type: application/json' "
            f"--data-binary @{path} {API}/api/tickers")
        return {"live": code == 0 and out.strip().startswith("["), "syms": syms}

    def job_quotes(self):
        code, out, _ = self._exec(f"curl -s -m 5 {API}/api/quotes")
        if code != 0 or not out.strip():
            return None
        return json.loads(out)

    def job_status(self):
        _, out, _ = self._exec("systemctl is-active ticker")
        return out.strip() or "inconnu"


class QuoteDelegate(QStyledItemDelegate):
    """Dessine chaque ligne : poignée · symbole · prix · variation colorée."""
    def sizeHint(self, option, index):
        return QSize(100, 46)

    def paint(self, p: QPainter, opt, idx):
        p.save()
        r = opt.rect.adjusted(4, 3, -4, -3)
        sel = opt.state & QStyle.State_Selected
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor("#1fd67a") if sel else QColor("#222b38"))
        p.setBrush(QColor("#17202c") if sel else QColor("#121822"))
        p.drawRoundedRect(r, 8, 8)

        sym, px, ch, cur = (idx.data(x) for x in (ROLE_SYM, ROLE_PX, ROLE_CH, ROLE_CUR))
        f = QFont(opt.font)
        p.setPen(MUT)
        p.drawText(QRect(r.left() + 12, r.top(), 20, r.height()), Qt.AlignVCenter, "⋮⋮")

        f.setBold(True); f.setPointSizeF(opt.font.pointSizeF() + 1.5); p.setFont(f)
        p.setPen(TXT)
        p.drawText(QRect(r.left() + 38, r.top(), 160, r.height()), Qt.AlignVCenter, sym)

        f.setBold(False); p.setFont(f)
        p.setPen(MUT)
        pxt = "—" if px is None else f"{px:,.2f}".replace(",", " ").replace(".", ",") + (f" {cur.strip()}" if cur else "")
        p.drawText(QRect(r.right() - 330, r.top(), 180, r.height()), Qt.AlignVCenter | Qt.AlignRight, pxt)

        f.setBold(True); p.setFont(f)
        if ch is None:
            p.setPen(MUT); cht = "introuvable" if px is None and idx.data(ROLE_PX + 10) else ""
        else:
            p.setPen(UP if ch >= 0 else DN)
            cht = ("▲ +" if ch >= 0 else "▼ ") + f"{ch:.2f}".replace(".", ",") + " %"
        p.drawText(QRect(r.right() - 140, r.top(), 128, r.height()), Qt.AlignVCenter | Qt.AlignRight, cht)
        p.restore()


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(720, 760)
        self.quotes = {}
        self.connected = False

        self.w = SSHWorker()
        self.w.done.connect(self.on_done)
        self.w.failed.connect(self.on_fail)
        self.w.start()

        conf = self.load_conf()
        root = QWidget(); self.setCentralWidget(root)
        lay = QVBoxLayout(root); lay.setContentsMargins(20, 18, 20, 14); lay.setSpacing(12)

        # --- Connexion ---
        title = QLabel("Connexion au Raspberry Pi"); title.setObjectName("h")
        lay.addWidget(title)
        form = QFormLayout(); form.setHorizontalSpacing(12)
        self.host = QLineEdit(conf.get("host", "192.168.1.35"))
        self.port = QSpinBox(); self.port.setRange(1, 65535); self.port.setValue(conf.get("port", 22))
        self.user = QLineEdit(conf.get("user", "hugo"))
        self.pwd = QLineEdit(); self.pwd.setEchoMode(QLineEdit.Password)
        self.pwd.setPlaceholderText("vide si tu utilises une clé SSH")
        self.key = QLineEdit(conf.get("key", "")); self.key.setPlaceholderText("optionnel : clé privée OpenSSH")
        kb = QPushButton("Parcourir"); kb.clicked.connect(self.pick_key)
        krow = QHBoxLayout(); krow.addWidget(self.key); krow.addWidget(kb)
        hrow = QHBoxLayout(); hrow.addWidget(self.host, 1); hrow.addWidget(QLabel("port")); hrow.addWidget(self.port)
        form.addRow("Hôte", hrow); form.addRow("Utilisateur", self.user)
        form.addRow("Mot de passe", self.pwd); form.addRow("Clé SSH", krow)
        self.form_box = QWidget(); self.form_box.setLayout(form); form.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.form_box)
        self.btn_conn = QPushButton("Se connecter"); self.btn_conn.setObjectName("primary")
        self.btn_conn.clicked.connect(self.connect_pi)
        self.pwd.returnPressed.connect(self.connect_pi)
        crow = QHBoxLayout(); self.conn_lbl = QLabel("● Déconnecté"); self.conn_lbl.setObjectName("mut")
        self.btn_set = QPushButton("Paramètres"); self.btn_set.hide()
        self.btn_set.clicked.connect(lambda: self.form_box.setVisible(not self.form_box.isVisible()))
        crow.addWidget(self.conn_lbl, 1); crow.addWidget(self.btn_set); crow.addWidget(self.btn_conn)
        lay.addLayout(crow)

        # --- Liste ---
        t2 = QLabel("Liste du bandeau"); t2.setObjectName("h"); lay.addWidget(t2)
        hint = QLabel("Glisse pour réordonner · Suppr pour retirer · chaque modif part directement sur le Pi")
        hint.setObjectName("mut"); lay.addWidget(hint)
        addrow = QHBoxLayout()
        self.new = QLineEdit(); self.new.setPlaceholderText("Ajouter : AAPL, MC.PA, ^FCHI, BTC-EUR…")
        self.new.returnPressed.connect(self.add_sym)
        ba = QPushButton("Ajouter"); ba.setObjectName("primary"); ba.clicked.connect(self.add_sym)
        addrow.addWidget(self.new, 1); addrow.addWidget(ba); lay.addLayout(addrow)

        self.list = QListWidget()
        self.list.setItemDelegate(QuoteDelegate(self.list))
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setSpacing(1)
        self.list.model().rowsMoved.connect(self.schedule_save)
        lay.addWidget(self.list, 1)

        brow = QHBoxLayout()
        self.btn_del = QPushButton("Retirer la sélection"); self.btn_del.clicked.connect(self.remove_sel)
        self.btn_up = QPushButton("▲"); self.btn_up.clicked.connect(lambda: self.move(-1))
        self.btn_dn = QPushButton("▼"); self.btn_dn.clicked.connect(lambda: self.move(1))
        for b in (self.btn_up, self.btn_dn): b.setFixedWidth(44)
        brow.addWidget(self.btn_del); brow.addStretch(1); brow.addWidget(self.btn_up); brow.addWidget(self.btn_dn)
        lay.addLayout(brow)
        QShortcut(QKeySequence.Delete, self.list, activated=self.remove_sel)

        self.status = QLabel(""); self.status.setObjectName("mut"); lay.addWidget(self.status)

        self.save_timer = QTimer(self, singleShot=True, interval=700, timeout=self.save_now)
        self.quote_timer = QTimer(self, interval=20000, timeout=self.refresh_quotes)
        self.set_enabled(False)

    # ---- helpers ----
    def load_conf(self):
        try:
            return json.loads(CONF_FILE.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def save_conf(self):
        CONF_FILE.write_text(json.dumps({"host": self.host.text().strip(), "port": self.port.value(),
                                         "user": self.user.text().strip(), "key": self.key.text().strip()}))

    def params(self):
        return {"host": self.host.text().strip(), "port": self.port.value(), "user": self.user.text().strip(),
                "password": self.pwd.text(), "key": self.key.text().strip()}

    def set_enabled(self, on):
        for wdg in (self.list, self.new, self.btn_del, self.btn_up, self.btn_dn):
            wdg.setEnabled(on)

    def syms(self):
        return [self.list.item(i).data(ROLE_SYM) for i in range(self.list.count())]

    def fill(self, syms):
        self.list.clear()
        for s in syms:
            it = QListWidgetItem()
            it.setData(ROLE_SYM, s)
            it.setFlags(it.flags() | Qt.ItemIsDragEnabled)
            self.list.addItem(it)
        self.apply_quotes()

    def apply_quotes(self):
        for i in range(self.list.count()):
            it = self.list.item(i); q = self.quotes.get(it.data(ROLE_SYM))
            it.setData(ROLE_PX, q and q.get("p")); it.setData(ROLE_CH, q and q.get("c"))
            it.setData(ROLE_CUR, (q or {}).get("cur", "")); it.setData(ROLE_PX + 10, q is not None)
        self.list.viewport().update()

    # ---- actions ----
    def pick_key(self):
        f, _ = QFileDialog.getOpenFileName(self, "Clé privée SSH", str(Path.home() / ".ssh"))
        if f:
            self.key.setText(f)

    def connect_pi(self, trust_new=False):
        self.btn_conn.setEnabled(False)
        self.conn_lbl.setText("● Connexion…")
        self.save_conf()
        self.w.submit("connect", self.params(), trust_new)

    def add_sym(self):
        for s in self.new.text().replace(";", ",").split(","):
            s = s.strip().upper()
            if s and s not in self.syms():
                it = QListWidgetItem(); it.setData(ROLE_SYM, s); self.list.addItem(it)
        self.new.clear(); self.apply_quotes(); self.schedule_save()

    def remove_sel(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))
        self.schedule_save()

    def move(self, d):
        r = self.list.currentRow()
        if r < 0 or not (0 <= r + d < self.list.count()):
            return
        it = self.list.takeItem(r); self.list.insertItem(r + d, it); self.list.setCurrentRow(r + d)
        self.schedule_save()

    def schedule_save(self, *_):
        if self.connected:
            self.status.setText("Modifications en attente…")
            self.save_timer.start()

    def save_now(self):
        self.w.submit("save", self.syms())

    def refresh_quotes(self):
        if self.connected:
            self.w.submit("quotes"); self.w.submit("status")

    # ---- retours du thread SSH ----
    def on_done(self, name, res):
        if name == "connect":
            self.connected = True; self.btn_conn.setEnabled(True); self.btn_conn.setText("Reconnecter")
            self.conn_lbl.setText(f"● Connecté à {self.user.text()}@{self.host.text()}")
            self.conn_lbl.setStyleSheet("color:#1fd67a")
            self.pwd.clear(); self.set_enabled(True); self.fill(res)
            self.form_box.hide(); self.btn_set.show()
            self.refresh_quotes(); self.quote_timer.start()
        elif name == "load":
            self.fill(res)
        elif name == "save":
            self.status.setText("✓ Envoyé — le bandeau est à jour" if res["live"]
                                else "✓ Fichier enregistré — le serveur du bandeau ne répond pas, il le prendra au prochain démarrage")
            QTimer.singleShot(4000, self.refresh_quotes)
        elif name == "quotes":
            if res:
                self.quotes = {q["s"]: q for q in res.get("data", [])}
                self.apply_quotes()
        elif name == "status":
            if not self.status.text().startswith(("✓", "Modif")):
                self.status.setText(f"Service bandeau : {res}")

    def on_fail(self, name, err):
        if name == "connect":
            self.btn_conn.setEnabled(True)
            if isinstance(err, UnknownHost):
                ok = QMessageBox.question(
                    self, "Nouvel appareil",
                    f"Première connexion à {err.host}.\n\nEmpreinte de la clé :\n{err.fingerprint}\n\n"
                    "Faire confiance à cet appareil ?")
                if ok == QMessageBox.Yes:
                    return self.connect_pi(trust_new=True)
                self.conn_lbl.setText("● Connexion annulée"); return
            self.connected = False; self.set_enabled(False); self.quote_timer.stop()
            self.conn_lbl.setStyleSheet("color:#ff4d5e")
            msg = "mot de passe ou clé refusé" if isinstance(err, paramiko.AuthenticationException) else str(err)
            self.conn_lbl.setText(f"● Échec : {msg}")
        elif name in ("quotes", "status"):
            pass  # silencieux : on réessaiera au prochain tour
        else:
            self.status.setText(f"⚠ {name} : {err}")
            if isinstance(err, (OSError, EOFError, paramiko.SSHException)):
                self.connected = False; self.set_enabled(False)
                self.conn_lbl.setText("● Connexion perdue — reconnecte-toi"); self.conn_lbl.setStyleSheet("color:#ff4d5e")

    def closeEvent(self, e):
        self.w.submit("quit"); self.w.wait(3000); e.accept()


STYLE = """
* { font-family: 'Segoe UI', 'DejaVu Sans', sans-serif; font-size: 10.5pt; color: #e8edf3; }
QMainWindow, QWidget { background: #0b0f15; }
QLabel#h { font-size: 13pt; font-weight: 700; margin-top: 6px; }
QLabel#mut { color: #7d8896; }
QLineEdit, QSpinBox { background: #121822; border: 1px solid #222b38; border-radius: 7px; padding: 8px 10px; }
QLineEdit:focus, QSpinBox:focus { border-color: #1fd67a; }
QPushButton { background: #18202b; border: 1px solid #2a3442; border-radius: 7px; padding: 8px 14px; }
QPushButton:hover { border-color: #3a4656; }
QPushButton:disabled { color: #4a5563; }
QPushButton#primary { background: #1fd67a; color: #04140b; border: 0; font-weight: 700; }
QPushButton#primary:disabled { background: #1b3d2c; color: #4a7a60; }
QListWidget { background: transparent; border: 0; outline: 0; }
QScrollBar:vertical { background: transparent; width: 10px; }
QScrollBar::handle:vertical { background: #222b38; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
"""

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    m = Main(); m.show()
    sys.exit(app.exec())
