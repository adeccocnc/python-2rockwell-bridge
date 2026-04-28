"""
ADECCO PLC Bridge - comunicatie Python <-> Allen-Bradley ControlLogix (1756-L7x)
prin EtherNet/IP CIP (biblioteca pylogix).

Codul foloseste ID-uri logice (CMD_START, STS_READY, ...).
JSON-ul plc_tags.json mapeaza fiecare ID la numele tag-ului din PLC (camp 'plc_tag').
Clientul editeaza doar 'plc_tag' daca are deja tag-uri definite cu alte nume.

UI: mainwindow.ui (editabil cu QtDesigner)
Config tag-uri: plc_tags.json
"""
import sys
import json
import time
from pathlib import Path
from PyQt5 import QtCore, QtGui, QtWidgets, uic

try:
    from pylogix import PLC
except ImportError:
    print("EROARE: pylogix nu e instalat. Ruleaza: pip install -r requirements.txt")
    sys.exit(1)

# Path-uri: cand rulam frozen (PyInstaller), fisierele sunt langa .exe;
# cand rulam ca sursa, langa main.py
if getattr(sys, 'frozen', False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).parent
CFG_PATH = _BASE / "plc_tags.json"
UI_PATH = _BASE / "mainwindow.ui"


def _coerce(typ: str, value):
    """Converteste valoarea Python in tipul corect pt PLC."""
    if value is None:
        return None
    if typ == "BOOL":
        return bool(value)
    if typ in ("SINT", "INT", "DINT"):
        return int(value)
    if typ == "REAL":
        return float(value)
    return value


class PlcWorker(QtCore.QObject):
    """Worker pe thread separat care face polling read + scriere outputs in PLC.
    Comunica cu UI prin ID-uri logice; mapping ID -> plc_tag e tinut intern."""
    connected = QtCore.pyqtSignal(bool, str)        # ok, mesaj
    inputs_updated = QtCore.pyqtSignal(dict)        # {logical_id: value}
    outputs_written = QtCore.pyqtSignal(dict)       # {logical_id: value}
    log = QtCore.pyqtSignal(str, str)               # mesaj, culoare

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self._comm = None
        self._stop_flag = False
        self._pending_writes = {}                   # logical_id -> value
        self._heartbeat = 0
        self._last_hb_t = 0.0
        # Mapari id <-> plc_tag (din sectiunile "inputs" si "outputs")
        ins = {k: v for k, v in cfg.get("inputs", {}).items() if not k.startswith("_")}
        outs = {k: v for k, v in cfg.get("outputs", {}).items() if not k.startswith("_")}
        all_tags = {**ins, **outs}
        self.id_to_tag = {tid: t["plc_tag"] for tid, t in all_tags.items()}
        self.tag_to_id = {t["plc_tag"]: tid for tid, t in all_tags.items()}
        self.id_to_type = {tid: t["type"] for tid, t in all_tags.items()}
        self.input_ids = list(ins.keys())
        self.input_plc_tags = [self.id_to_tag[tid] for tid in self.input_ids]

    def _try_connect(self, ip, slot):
        """Incearca o conexiune (sau reconectare). Returneaza True/False."""
        try:
            if self._comm is not None:
                try: self._comm.Close()
                except Exception: pass
            self._comm = PLC()
            self._comm.IPAddress = ip
            self._comm.ProcessorSlot = slot
            test_tag = self.input_plc_tags[0] if self.input_plc_tags else self.id_to_tag.get("HEARTBEAT")
            if test_tag:
                ret = self._comm.Read(test_tag)
                if ret.Status != "Success":
                    raise RuntimeError(f"Test read '{test_tag}' a esuat: {ret.Status}")
            return True
        except Exception as e:
            self._last_conn_err = str(e)
            return False

    @QtCore.pyqtSlot()
    def start(self):
        plc_cfg = self.cfg.get("plc", {})
        ip = plc_cfg.get("ip", "192.168.1.10")
        slot = int(plc_cfg.get("slot", 0))
        poll_ms = int(plc_cfg.get("poll_interval_ms", 100))
        hb_ms = int(plc_cfg.get("heartbeat_interval_ms", 500))
        # Backoff reconectare: 0.5s, 1s, 2s, 5s, 10s, 10s...
        backoff_seq = [0.5, 1.0, 2.0, 5.0, 10.0]
        backoff_idx = 0
        connected_now = False
        consecutive_fail = 0
        FAIL_THRESHOLD = 3   # dupa 3 erori consecutive, marcam offline si reconectam
        self._stop_flag = False
        while not self._stop_flag:
            # ---- Faza CONECTARE / RECONECTARE ----
            if not connected_now:
                self.log.emit(f"Conectare la {ip}:{slot}...", "#FFC857")
                if self._try_connect(ip, slot):
                    connected_now = True
                    consecutive_fail = 0
                    backoff_idx = 0
                    self.connected.emit(True, f"Conectat la {ip}:{slot}")
                    self.log.emit(f"Conectat la PLC {ip} slot {slot}", "#34C759")
                else:
                    wait_s = backoff_seq[min(backoff_idx, len(backoff_seq) - 1)]
                    self.connected.emit(False, getattr(self, '_last_conn_err', 'fail'))
                    self.log.emit(f"Reconectare in {wait_s:.0f}s ({self._last_conn_err})", "#FF3B30")
                    backoff_idx += 1
                    # Sleep cu posibilitate de stop
                    t_wait = time.time() + wait_s
                    while not self._stop_flag and time.time() < t_wait:
                        QtCore.QThread.msleep(100)
                    continue
            # ---- Faza CICLU NORMAL ----
            t0 = time.time()
            had_error = False
            # Heartbeat
            if "HEARTBEAT" in self.id_to_tag and (t0 - self._last_hb_t) * 1000 >= hb_ms:
                self._heartbeat = (self._heartbeat + 1) & 0x7FFFFFFF
                self._pending_writes["HEARTBEAT"] = self._heartbeat
                self._last_hb_t = t0
            # Citeste inputs
            try:
                if self.input_plc_tags:
                    rets = self._comm.Read(self.input_plc_tags)
                    vals_by_id = {}
                    rets_list = rets if isinstance(rets, list) else [rets]
                    any_ok = False
                    for r in rets_list:
                        if r.Status == "Success":
                            any_ok = True
                            tid = self.tag_to_id.get(r.TagName)
                            if tid:
                                vals_by_id[tid] = r.Value
                    if not any_ok and rets_list:
                        had_error = True
                        self.log.emit(f"Citire fail: {rets_list[0].Status}", "#FF3B30")
                    if vals_by_id:
                        self.inputs_updated.emit(vals_by_id)
            except Exception as e:
                had_error = True
                self.log.emit(f"Eroare citire: {e}", "#FF3B30")
            # Scrie outputs
            if self._pending_writes:
                writes = list(self._pending_writes.items())
                self._pending_writes.clear()
                done = {}
                for tid, val in writes:
                    plc_tag = self.id_to_tag.get(tid)
                    if not plc_tag:
                        continue
                    typ = self.id_to_type.get(tid, "DINT")
                    coerced = _coerce(typ, val)
                    try:
                        ret = self._comm.Write(plc_tag, coerced)
                        if ret.Status == "Success":
                            done[tid] = coerced
                        else:
                            had_error = True
                            self.log.emit(f"Write {plc_tag}={coerced} fail: {ret.Status}", "#FF3B30")
                    except Exception as e:
                        had_error = True
                        self.log.emit(f"Write {plc_tag} exception: {e}", "#FF3B30")
                if done:
                    self.outputs_written.emit(done)
            # Detectare deconectare
            if had_error:
                consecutive_fail += 1
                if consecutive_fail >= FAIL_THRESHOLD:
                    self.log.emit(f"Conexiune pierduta dupa {consecutive_fail} erori consecutive — reconectez", "#FF3B30")
                    connected_now = False
                    self.connected.emit(False, "Conexiune pierduta")
                    consecutive_fail = 0
                    continue
            else:
                consecutive_fail = 0
            # Sleep pana la urmatorul poll
            elapsed = (time.time() - t0) * 1000
            sleep_ms = max(10, poll_ms - int(elapsed))
            QtCore.QThread.msleep(sleep_ms)
        try:
            if self._comm: self._comm.Close()
        except Exception:
            pass
        self.connected.emit(False, "Deconectat")
        self.log.emit("Worker oprit", "#FF9500")

    @QtCore.pyqtSlot(str, object)
    def queue_write(self, logical_id, value):
        """Pune un write in coada (apel din UI thread)."""
        self._pending_writes[logical_id] = value

    @QtCore.pyqtSlot()
    def stop(self):
        self._stop_flag = True


class MainWindow(QtWidgets.QMainWindow):
    request_write = QtCore.pyqtSignal(str, object)
    request_stop = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        uic.loadUi(str(UI_PATH), self)
        self.cfg = self._load_cfg()
        self._worker = None
        self._worker_thread = None
        self._last_inputs = {}
        self._last_outputs = {}
        self._row_by_id_in = {}     # logical_id -> row in tblInputs
        self._row_by_id_out = {}    # logical_id -> row in tblOutputs
        self._init_tables()
        self._wire_buttons()
        self._apply_cfg_to_ui()
        self.statusbar.showMessage("Gata. Conecteaza-te la PLC.")
        self._log("Aplicatia pornita. Editeaza plc_tags.json pt mapare tag-uri.", "#FFC857")

    def _load_cfg(self):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Config", f"Nu pot citi {CFG_PATH}:\n{e}")
            sys.exit(1)

    def _apply_cfg_to_ui(self):
        plc = self.cfg.get("plc", {})
        self.edIp.setText(plc.get("ip", "192.168.1.10"))
        self.spinSlot.setValue(int(plc.get("slot", 0)))

    def _init_tables(self):
        # Coloane tabele: refac headerul ca sa adaug coloana plc_tag
        self.tblInputs.setColumnCount(5)
        self.tblInputs.setHorizontalHeaderLabels(["ID logic", "PLC tag", "Tip", "Valoare", "Descriere"])
        self.tblOutputs.setColumnCount(6)
        self.tblOutputs.setHorizontalHeaderLabels(["ID logic", "PLC tag", "Tip", "Valoare", "Override manual", "Descriere"])
        ins_raw = self.cfg.get("inputs", {})
        outs_raw = self.cfg.get("outputs", {})
        ins = [(tid, t) for tid, t in ins_raw.items() if not tid.startswith("_")]
        outs = [(tid, t) for tid, t in outs_raw.items() if not tid.startswith("_")]
        # Inputs
        self.tblInputs.setRowCount(len(ins))
        self._row_by_id_in = {}
        for i, (tid, t) in enumerate(ins):
            self._row_by_id_in[tid] = i
            self.tblInputs.setItem(i, 0, QtWidgets.QTableWidgetItem(tid))
            self.tblInputs.setItem(i, 1, QtWidgets.QTableWidgetItem(t.get("plc_tag", "")))
            self.tblInputs.setItem(i, 2, QtWidgets.QTableWidgetItem(t.get("type", "")))
            it_val = QtWidgets.QTableWidgetItem("?")
            it_val.setTextAlignment(QtCore.Qt.AlignCenter)
            self.tblInputs.setItem(i, 3, it_val)
            self.tblInputs.setItem(i, 4, QtWidgets.QTableWidgetItem(t.get("desc", "")))
        self.tblInputs.resizeColumnsToContents()
        # Outputs
        self.tblOutputs.setRowCount(len(outs))
        self._row_by_id_out = {}
        for i, (tid, t) in enumerate(outs):
            self._row_by_id_out[tid] = i
            self.tblOutputs.setItem(i, 0, QtWidgets.QTableWidgetItem(tid))
            self.tblOutputs.setItem(i, 1, QtWidgets.QTableWidgetItem(t.get("plc_tag", "")))
            self.tblOutputs.setItem(i, 2, QtWidgets.QTableWidgetItem(t.get("type", "")))
            it_val = QtWidgets.QTableWidgetItem("?")
            it_val.setTextAlignment(QtCore.Qt.AlignCenter)
            self.tblOutputs.setItem(i, 3, it_val)
            typ = t.get("type", "")
            if typ == "BOOL":
                cb = QtWidgets.QCheckBox()
                cb.setStyleSheet("margin-left: 16px;")
                cb.toggled.connect(lambda v, tid=tid: self._on_override(tid, bool(v)))
                self.tblOutputs.setCellWidget(i, 4, cb)
            else:
                ed = QtWidgets.QLineEdit()
                ed.setPlaceholderText("override (Enter)")
                ed.returnPressed.connect(lambda e=ed, tid=tid, typ=typ: self._on_override_edit(tid, typ, e.text()))
                self.tblOutputs.setCellWidget(i, 4, ed)
            self.tblOutputs.setItem(i, 5, QtWidgets.QTableWidgetItem(t.get("desc", "")))
        self.tblOutputs.resizeColumnsToContents()

    def _wire_buttons(self):
        self.btnConnect.clicked.connect(self._on_connect)
        self.btnDisconnect.clicked.connect(self._on_disconnect)
        self.btnReloadCfg.clicked.connect(self._on_reload_cfg)
        self.btnSimMeasure.clicked.connect(self._on_sim_measure)
        self.btnSimError.clicked.connect(self._on_sim_error)
        self.btnSimReset.clicked.connect(self._on_sim_reset)

    def _plc_tag_of(self, logical_id):
        for sect in ("inputs", "outputs"):
            t = self.cfg.get(sect, {}).get(logical_id)
            if isinstance(t, dict):
                return t.get("plc_tag", "?")
        return "?"

    def _on_override(self, logical_id, value):
        self.request_write.emit(logical_id, value)
        self._log(f"Override manual: {logical_id} ({self._plc_tag_of(logical_id)}) = {value}", "#5856D6")

    def _on_override_edit(self, logical_id, typ, text):
        try:
            if typ in ("DINT", "INT", "SINT"):
                val = int(text)
            elif typ == "REAL":
                val = float(text)
            else:
                val = text
        except ValueError:
            self._log(f"Valoare invalida pt {logical_id} ({typ}): '{text}'", "#FF3B30")
            return
        self.request_write.emit(logical_id, val)
        self._log(f"Override manual: {logical_id} ({self._plc_tag_of(logical_id)}) = {val}", "#5856D6")

    def _on_connect(self):
  
        if self._worker_thread is not None:
            return
        self.cfg["plc"]["ip"] = self.edIp.text().strip()
        self.cfg["plc"]["slot"] = self.spinSlot.value()
        self._worker = PlcWorker(self.cfg)
        self._worker_thread = QtCore.QThread()
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.start)
        self._worker.connected.connect(self._on_worker_connected)
        self._worker.inputs_updated.connect(self._on_inputs)
        self._worker.outputs_written.connect(self._on_outputs)
        self._worker.log.connect(self._log)
        self.request_write.connect(self._worker.queue_write)
        self.request_stop.connect(self._worker.stop)
        self._worker_thread.start()
        self._set_status("CONECTARE...", "#FFC857")

    def _on_disconnect(self):
        if self._worker_thread is None:
            return
        self.request_stop.emit()
        self._worker_thread.quit()
        self._worker_thread.wait(2000)
        self._worker_thread = None
        self._worker = None
        self._set_status("DECONECTAT", "#888")

    def _on_reload_cfg(self):
        self.cfg = self._load_cfg()
        self._init_tables()
        self._apply_cfg_to_ui()
        self._log("Config reincarcat din plc_tags.json", "#FFC857")

    def _on_sim_measure(self):
        scale = self.cfg["plc"].get("scale_factor", 1000)
        val_mm = self.spinSimVal.value()
        val_int = int(round(val_mm * scale))
        self._log(f"Simulare ciclu: {val_mm:.3f} mm × {scale} = {val_int}", "#FF9500")
        self.request_write.emit("STS_READY", False)
        self.request_write.emit("STS_RUNNING", True)
        self.request_write.emit("STS_DONE", False)
        self.request_write.emit("STS_ERROR", False)
        QtCore.QTimer.singleShot(2000, lambda: self._sim_finish(val_int))

    def _sim_finish(self, val_int):
        # Important: scriem RES_VALUE INAINTE de STS_DONE, ca PLC sa
        # citeasca o valoare valida cand vede rising edge pe STS_DONE
        self.request_write.emit("RES_VALUE", val_int)
        self.request_write.emit("STS_RUNNING", False)
        self.request_write.emit("STS_DONE", True)
        self.request_write.emit("STS_READY", True)
        self._log(f"Simulare DONE: RES_VALUE={val_int}", "#34C759")

    def _on_sim_error(self):
        self.request_write.emit("STS_ERROR", True)
        self.request_write.emit("ERROR_CODE", 1)
        self.request_write.emit("STS_RUNNING", False)
        self.request_write.emit("STS_DONE", False)
        self._log("Simulare EROARE -> STS_ERROR=1, ERROR_CODE=1", "#FF3B30")

    def _on_sim_reset(self):
        self.request_write.emit("STS_ERROR", False)
        self.request_write.emit("ERROR_CODE", 0)
        self.request_write.emit("STS_DONE", False)
        self.request_write.emit("STS_READY", True)
        self._log("RESET -> stari curatate", "#FFC857")

    def _on_worker_connected(self, ok, msg):
        if ok:
            self._set_status("CONECTAT", "#34C759")
            for tid, val in [
                ("STS_READY", True), ("STS_RUNNING", False), ("STS_DONE", False),
                ("STS_ERROR", False), ("STS_ROBOT_HOME", True), ("STS_ROBOT_IN_POS", False),
                ("ERROR_CODE", 0),
            ]:
                if tid in self.cfg.get("outputs", {}):
                    self.request_write.emit(tid, val)
        else:
            self._set_status("EROARE", "#FF3B30")

    def _on_inputs(self, vals_by_id):
        self._last_inputs.update(vals_by_id)
        for tid, v in vals_by_id.items():
            row = self._row_by_id_in.get(tid)
            if row is None:
                continue
            it = self.tblInputs.item(row, 3)
            old = it.text()
            new = str(v)
            it.setText(new)
            if old != new:
                it.setBackground(QtGui.QColor("#FF9500"))
                QtCore.QTimer.singleShot(500,
                    lambda r=row: self.tblInputs.item(r, 3).setBackground(QtGui.QColor(0, 0, 0, 0)))
        # Detect rising edge pe CMD_START -> declanseaza simularea
        prev_start = getattr(self, '_prev_start', False)
        new_start = bool(vals_by_id.get("CMD_START", prev_start))
        if "CMD_START" in vals_by_id and new_start and not prev_start:
            self._log("[PLC] CMD_START rising edge -> declansez ciclu simulat", "#FF9500")
            self._on_sim_measure()
        if "CMD_START" in vals_by_id:
            self._prev_start = new_start
        # Reset rising edge
        prev_reset = getattr(self, '_prev_reset', False)
        new_reset = bool(vals_by_id.get("CMD_RESET", prev_reset))
        if "CMD_RESET" in vals_by_id and new_reset and not prev_reset:
            self._log("[PLC] CMD_RESET rising edge", "#FFC857")
            self._on_sim_reset()
        if "CMD_RESET" in vals_by_id:
            self._prev_reset = new_reset

    def _on_outputs(self, vals_by_id):
        self._last_outputs.update(vals_by_id)
        for tid, v in vals_by_id.items():
            row = self._row_by_id_out.get(tid)
            if row is not None:
                self.tblOutputs.item(row, 3).setText(str(v))

    def _set_status(self, text, color):
        self.lblStatus.setText(text)
        self.lblStatus.setStyleSheet(
            f"QLabel {{ color: white; font-weight: bold; padding: 4px 10px; "
            f"border-radius: 4px; background: {color}; }}"
        )
        # LED gradient radial — culoare luminoasa in centru, mai inchis pe margini.
        # Verde aprins = conectat, rosu = eroare/cazut, galben = in conectare, gri = deconectat
        if color == "#34C759":
            led_grad = "stop:0 #80FF80, stop:0.5 #34C759, stop:1 #1E6E32"
        elif color == "#FF3B30":
            led_grad = "stop:0 #FF8080, stop:0.5 #FF3B30, stop:1 #8B1A14"
        elif color == "#FFC857":
            led_grad = "stop:0 #FFE090, stop:0.5 #FFC857, stop:1 #8B6F2F"
        else:
            led_grad = "stop:0 #888, stop:0.7 #444, stop:1 #222"
        self.ledStatus.setStyleSheet(
            f"QLabel {{ background: qradialgradient(cx:0.4, cy:0.4, radius:0.6, "
            f"fx:0.3, fy:0.3, {led_grad}); border: 1px solid #000; border-radius: 11px; }}"
        )

    def _log(self, msg, color="#0f0"):
        ts = time.strftime("%H:%M:%S")
        self.txtLog.appendHtml(f'<span style="color:{color};">[{ts}] {msg}</span>')

    def closeEvent(self, e):
        self._on_disconnect()
        e.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor(45, 45, 48))
    pal.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor(30, 30, 30))
    pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(38, 38, 38))
    pal.setColor(QtGui.QPalette.Text, QtCore.Qt.white)
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor(60, 60, 60))
    pal.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(255, 149, 0))
    pal.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
    app.setPalette(pal)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
