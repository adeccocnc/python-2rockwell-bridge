# ADECCO PLC Bridge

Aplicație Python care comunică cu un PLC Allen-Bradley ControlLogix
(CPU 1756-L7x) prin EtherNet/IP CIP, folosind biblioteca `pylogix`.

Rolul ei: transmite starea sistemului ADECCO (robot + camere) către PLC-ul
clientului, primește comanda START de la el și raportează valoarea măsurată
(înmulțită cu un factor de scalare ca să fie întreagă).

---

## Instalare

```cmd
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Necesită Python 3.8+ și PyQt5.

---

## Configurare

Toată configurarea se face în **`plc_tags.json`**.

### Conexiune PLC

```json
"plc": {
  "ip": "192.168.1.10",       // IP-ul CPU-ului ControlLogix
  "slot": 0,                  // Slot-ul CPU-ului in chassis (uzual 0)
  "poll_interval_ms": 100,    // Cat de des citim inputs (ms)
  "heartbeat_interval_ms": 500, // Cat de des incrementam HEARTBEAT
  "scale_factor": 1000        // Valoare_mm * scale = int trimis (1000 = micrometri)
}
```

### Mapare tag-uri (`tag_map`)

Codul Python folosește **ID-uri logice** stabile (`CMD_START`, `STS_READY`...).
JSON-ul mapează fiecare ID la **numele real al tag-ului din PLC** (`plc_tag`).

```json
"CMD_START": {
  "plc_tag": "ADECCO_CMD_START",   // <-- aici se schimba daca clientul are alt nume
  "type": "BOOL",
  "dir": "in",
  "desc": "Comanda START de la PLC"
}
```

**Clientul are două opțiuni:**

1. **Creează tag-uri în PLC cu numele implicite** (`ADECCO_CMD_START`,
   `ADECCO_STS_READY`, etc.) — atunci nu trebuie editat nimic în JSON.
2. **Are deja tag-uri cu alte denumiri** (ex: `G65_StartFromAdecco`) —
   atunci editează doar câmpul `plc_tag` în JSON. ID-urile logice (cheile)
   rămân nemodificate.

### Listă completă tag-uri (default)

| ID logic | Tip | Direcție | PLC tag default | Rol |
|---|---|---|---|---|
| `CMD_START` | BOOL | PLC→Bridge | `ADECCO_CMD_START` | START măsurare |
| `CMD_RESET` | BOOL | PLC→Bridge | `ADECCO_CMD_RESET` | Reset eroare |
| `CMD_ABORT` | BOOL | PLC→Bridge | `ADECCO_CMD_ABORT` | Abort măsurare |
| `TYRE_CODE` | DINT | PLC→Bridge | `ADECCO_TYRE_CODE` | Cod produs / rețetă |
| `HEARTBEAT` | DINT | Bridge→PLC | `ADECCO_HEARTBEAT` | Counter incremental (~500ms) |
| `STS_READY` | BOOL | Bridge→PLC | `ADECCO_STS_READY` | Sistem gata |
| `STS_RUNNING` | BOOL | Bridge→PLC | `ADECCO_STS_RUNNING` | Măsurare în curs |
| `STS_DONE` | BOOL | Bridge→PLC | `ADECCO_STS_DONE` | Măsurare terminată |
| `STS_ERROR` | BOOL | Bridge→PLC | `ADECCO_STS_ERROR` | Stare eroare |
| `STS_ROBOT_HOME` | BOOL | Bridge→PLC | `ADECCO_STS_ROBOT_HOME` | Robot în HOME |
| `STS_ROBOT_IN_POS` | BOOL | Bridge→PLC | `ADECCO_STS_ROBOT_IN_POS` | Robot în poziție măsurare |
| `RES_VALUE` | DINT | Bridge→PLC | `ADECCO_RES_VALUE` | Valoare măsurată × `scale_factor`, citită de PLC pe rising edge `STS_DONE` |
| `ERROR_CODE` | DINT | Bridge→PLC | `ADECCO_ERROR_CODE` | Cod eroare numeric |

---

## Cerințe în PLC

1. **Toate tag-urile ADECCO_* trebuie declarate în Controller Tags**
   (nu Program Tags), ca să fie accesibile de pe rețea.
2. **External Access = Read/Write** (default).
3. **Constant = unchecked**.
4. CPU-ul trebuie să aibă **EtherNet/IP scanner enabled** și să fie
   **routabil** din PC-ul pe care rulează bridge-ul (ping `192.168.1.10`
   trebuie să răspundă).

---

## Protocol de comunicare (handshake recomandat)

```
PLC                         BRIDGE
───────────────             ───────────────
                  init      STS_READY = 1
                            STS_ROBOT_HOME = 1
                            HEARTBEAT++ (continuu)

CMD_START = 1     ────►     vede rising edge
                            STS_READY = 0
                            STS_RUNNING = 1
                            (executa ciclu robot+masurare)
                            STS_ROBOT_IN_POS = 1 (cand ajunge)
                            ...
                            RES_VALUE = 12345
                            STS_RUNNING = 0
                            STS_DONE = 1   (rising edge -> PLC citeste RES_VALUE)
                            STS_ROBOT_HOME = 1
                            STS_READY = 1

CMD_START = 0     ◄────     PLC reseteaza dupa ce a citit RES_VALUE

(in caz de eroare)          STS_ERROR = 1
                            ERROR_CODE = N
CMD_RESET = 1     ────►     STS_ERROR = 0
                            ERROR_CODE = 0
CMD_RESET = 0
```

`RES_VALUE` interpretare: dacă `scale_factor = 1000` și `RES_VALUE = 12345`,
atunci valoarea măsurată reală = `12.345 mm`.

---

## UI

- **Sus**: IP / Slot / Connect / Disconnect / status conexiune.
- **Stânga**: tabelul `INPUT-uri` (citim de la PLC) cu valorile live.
- **Dreapta**: tabelul `OUTPUT-uri` (scriem în PLC) cu valorile curente
  și override manual pentru testare (checkbox la BOOL, edit la DINT).
- **Simulare**: buton care imită un ciclu START → 2s așteptare → RES_VALUE
  scrisă cu valoarea din spinbox. Util pentru testare fără robotul real.
- **Log**: jos, cu timestamp și culori.

Detectia automată: dacă PLC-ul setează `CMD_START = 1` (rising edge),
bridge-ul declanșează automat ciclul simulat. La `CMD_RESET = 1`, face reset.

---

## Editarea UI cu Qt Designer

```cmd
pyqt5-tools designer mainwindow.ui
```

(sau direct din Qt Designer File → Open). UI-ul e încărcat la runtime cu
`uic.loadUi`, deci după salvare rulezi din nou `python main.py` — fără
recompilare.

---

## Integrare în programul principal

Acest bridge e autonom acum, dar e conceput să se mute ulterior în
aplicația principală Pirelli. Logica esențială (clasa `PlcWorker` și
maparea ID logic ↔ `plc_tag`) e izolată și poate fi importată ca modul.
