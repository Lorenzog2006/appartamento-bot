from flask import Flask, request
import json, os, re, base64, urllib.request, urllib.error
from datetime import datetime

app = Flask(__name__)

TOKEN          = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
GROQ_KEY       = (os.environ.get("GROQ_API_KEY") or "").strip()
ANTHROPIC_KEY  = (os.environ.get("ANTHROPIC_KEY") or "").strip()
OWNER_ID       = (os.environ.get("OWNER_CHAT_ID") or "").strip()
GITHUB_TOKEN   = (os.environ.get("GITHUB_TOKEN") or "").strip()
DASHBOARD_KEY  = (os.environ.get("DASHBOARD_KEY") or "").strip()

WA_TOKEN        = (os.environ.get("WHATSAPP_TOKEN") or "").strip()
WA_PHONE_ID     = (os.environ.get("WHATSAPP_PHONE_ID") or "").strip()
WA_VERIFY_TOKEN = (os.environ.get("WHATSAPP_VERIFY_TOKEN") or "").strip()
WA_PULIZIE      = (os.environ.get("WA_PULIZIE") or "").strip()  # numero WhatsApp signora pulizie (es: 393201234567)
NOME_PULIZIE    = (os.environ.get("NOME_PULIZIE") or "Signora delle pulizie").strip()

REPO         = "Lorenzog2006/appartamento-bot"
GITHUB_RAW   = f"https://raw.githubusercontent.com/{REPO}/main/appartamento.txt"
GITHUB_API   = f"https://api.github.com/repos/{REPO}/contents/appartamento.txt"
STATS_API       = f"https://api.github.com/repos/{REPO}/contents/stats.json"
DAILY_STATS_API = f"https://api.github.com/repos/{REPO}/contents/daily_stats.json"
BOOKINGS_API    = f"https://api.github.com/repos/{REPO}/contents/bookings.json"
CONVERSATIONS_API = f"https://api.github.com/repos/{REPO}/contents/conversations.json"
USERS_API       = f"https://api.github.com/repos/{REPO}/contents/users.json"
ANALYTICS_API   = f"https://api.github.com/repos/{REPO}/contents/analytics.json"
PULIZIE_API     = f"https://api.github.com/repos/{REPO}/contents/pulizie_turni.json"
INFO_PATH    = os.path.join(os.path.dirname(__file__), "appartamento.txt")

# ── Stato sessioni ────────────────────────────────────────────────────────────
# chat_id → {"storia": [...], "ultimo": timestamp}
_conversazioni = {}
_conv_sha = None         # SHA del file conversations.json su GitHub
_conv_loaded = False     # True dopo primo caricamento da GitHub
MAX_MESSAGGI   = 10
SCADENZA_ORE   = 2

# Anagrafica utenti: chat_id → metadati cumulativi (totale msg, lingua, topic, ecc.)
_users = {}
_users_sha = None
_users_loaded = False

# chat_id ospite → {"nome": nome, "lingua": lingua} — aspettiamo le date
_attesa_date = {}
# OWNER_ID → guest_chat_id — Lorenzo sta per inviare date corrette
_attesa_correzione_owner = {}
# Flusso guidato upload media: OWNER_ID → {file_id, tipo, step, keywords}
_upload_media = {}
# Flusso guidato creazione prenotazione manuale: OWNER_ID → {step, nome, checkin, checkout, lingua}
_attesa_prenotazione = {}

# Aggregazione notifiche stesso ospite: chat_id_ospite (str) → {msg_id, testo, ts, bottoni}
_ultima_notif_ospite = {}
_NOTIF_AGGREGA_SEC = 120


# ── Helper sicurezza/robustezza ───────────────────────────────────────────────
def escape_md(s):
    """Escape caratteri speciali Telegram parse_mode='Markdown' (legacy).
    Markdown legacy interpreta solo: _ * ` [ — escapiamo questi."""
    if s is None:
        return ""
    return (str(s)
            .replace("\\", "\\\\")
            .replace("_", "\\_")
            .replace("*", "\\*")
            .replace("`", "\\`")
            .replace("[", "\\["))


def urlopen_retry(req, timeout=10, retries=2, backoff=0.8):
    """urlopen con retry su 5xx/timeout/network. Lascia passare 4xx (errori client)."""
    import time as _t
    last_exc = None
    for i in range(retries + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if 500 <= e.code < 600 and i < retries:
                last_exc = e
                _t.sleep(backoff * (2 ** i))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if i < retries:
                last_exc = e
                _t.sleep(backoff * (2 ** i))
                continue
            raise
    if last_exc:
        raise last_exc


def _carica_conversazioni_da_github():
    """Carica conversazioni da GitHub. Best-effort, mai bloccante."""
    global _conversazioni, _conv_sha, _conv_loaded
    if _conv_loaded or not GITHUB_TOKEN:
        _conv_loaded = True
        return
    try:
        url = f"{CONVERSATIONS_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=4)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        loaded = json.loads(contenuto) if contenuto.strip() else {}
        # Ripulisci conversazioni scadute (>SCADENZA_ORE)
        ora = datetime.now().timestamp()
        _conversazioni = {
            cid: c for cid, c in loaded.items()
            if (ora - c.get("ultimo", 0)) <= SCADENZA_ORE * 3600
        }
        _conv_sha = data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # File non esiste ancora: ok, lo creeremo al primo salvataggio
            _conversazioni = {}
            _conv_sha = None
        else:
            try:
                log_errore("conv_load", e)
            except Exception:
                pass
    except Exception as e:
        try:
            log_errore("conv_load", e)
        except Exception:
            pass
    finally:
        _conv_loaded = True

def _salva_conversazioni_su_github():
    """Salva le conversazioni su GitHub. Best-effort, mai bloccante."""
    global _conv_sha
    if not GITHUB_TOKEN:
        return
    try:
        contenuto_nuovo = json.dumps(_conversazioni, ensure_ascii=False)
        payload = {
            "message": "Aggiorna conversazioni",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
        }
        if _conv_sha:
            payload["sha"] = _conv_sha
        req = urllib.request.Request(CONVERSATIONS_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        r = urllib.request.urlopen(req, timeout=8)
        risposta = json.loads(r.read())
        _conv_sha = risposta.get("content", {}).get("sha", _conv_sha)
    except urllib.error.HTTPError as e:
        # Conflitto SHA (409): ricarica e riprova una volta
        if e.code in (409, 422):
            try:
                global _conv_loaded
                _conv_loaded = False
                _carica_conversazioni_da_github()
            except Exception:
                pass
        else:
            try:
                log_errore("conv_save", e)
            except Exception:
                pass
    except Exception as e:
        try:
            log_errore("conv_save", e)
        except Exception:
            pass

def get_storia(chat_id):
    _carica_conversazioni_da_github()
    ora = datetime.now().timestamp()
    conv = _conversazioni.get(str(chat_id)) or _conversazioni.get(chat_id)
    if conv and (ora - conv["ultimo"]) > SCADENZA_ORE * 3600:
        _conversazioni.pop(str(chat_id), None)
        _conversazioni.pop(chat_id, None)
        conv = None
    return conv["storia"] if conv else []

def aggiorna_storia(chat_id, domanda, risposta):
    _carica_conversazioni_da_github()
    ora = datetime.now().timestamp()
    cid = str(chat_id)
    if cid not in _conversazioni:
        _conversazioni[cid] = {"storia": [], "ultimo": ora}
    storia = _conversazioni[cid]["storia"]
    storia.append({"role": "user",      "content": domanda})
    storia.append({"role": "assistant", "content": risposta})
    if len(storia) > MAX_MESSAGGI * 2:
        storia = storia[-(MAX_MESSAGGI * 2):]
    _conversazioni[cid]["storia"] = storia
    _conversazioni[cid]["ultimo"] = ora
    _salva_conversazioni_su_github()


# ── Anagrafica utenti (users.json) ────────────────────────────────────────────
def _carica_users_da_github():
    global _users, _users_sha, _users_loaded
    if _users_loaded or not GITHUB_TOKEN:
        _users_loaded = True
        return
    try:
        url = f"{USERS_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=4)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        _users = json.loads(contenuto) if contenuto.strip() else {}
        _users_sha = data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _users = {}
            _users_sha = None
    except Exception:
        pass
    finally:
        _users_loaded = True

def _salva_users_su_github():
    global _users_sha
    if not GITHUB_TOKEN:
        return
    try:
        contenuto_nuovo = json.dumps(_users, ensure_ascii=False, indent=2)
        payload = {
            "message": "Aggiorna anagrafica utenti",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
        }
        if _users_sha:
            payload["sha"] = _users_sha
        req = urllib.request.Request(USERS_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        r = urllib.request.urlopen(req, timeout=8)
        risposta = json.loads(r.read())
        _users_sha = risposta.get("content", {}).get("sha", _users_sha)
    except urllib.error.HTTPError as e:
        if e.code in (409, 422):
            global _users_loaded
            _users_loaded = False
            _carica_users_da_github()
    except Exception:
        pass

# ── Stato wizard /prenotazione (file dedicato wizard_state.json su GitHub:
# solo l'owner ci scrive → zero race con altre richieste) ─────────────────────
WIZARD_API = f"https://api.github.com/repos/{REPO}/contents/wizard_state.json"

def _wizard_load_raw():
    """Ritorna (dict_stato, sha) leggendo wizard_state.json da GitHub.
    Se il file non esiste ritorna ({}, None). Non usa cache: legge sempre fresco
    per minimizzare conflitti (il wizard dura ~30s, non vale la pena cachare)."""
    if not GITHUB_TOKEN:
        return ({}, None)
    try:
        url = f"{WIZARD_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=4)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        stato = json.loads(contenuto) if contenuto.strip() else {}
        return (stato, data["sha"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ({}, None)
        return ({}, None)
    except Exception:
        return ({}, None)


def _wizard_save_raw(stato_completo, sha):
    """PUT wizard_state.json. Su conflitto refetch sha e ritenta una volta."""
    if not GITHUB_TOKEN:
        return False
    for _attempt in range(2):
        try:
            payload = {
                "message": "Bot aggiorna wizard state",
                "content": base64.b64encode(json.dumps(stato_completo, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8"),
            }
            if sha:
                payload["sha"] = sha
            req = urllib.request.Request(WIZARD_API, data=json.dumps(payload).encode(), headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "appartamento-bot"
            }, method="PUT")
            urllib.request.urlopen(req, timeout=8)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (409, 422):
                _, sha = _wizard_load_raw()
                continue
            return False
        except Exception:
            return False
    return False


def wizard_pren_get(chat_id):
    """Legge lo stato del wizard /prenotazione per chat_id. Ritorna dict o None."""
    try:
        stato, _ = _wizard_load_raw()
        st = stato.get(str(chat_id))
        return st if isinstance(st, dict) else None
    except Exception:
        return None


def wizard_pren_set(chat_id, stato_wizard):
    """Salva lo stato del wizard /prenotazione per chat_id."""
    try:
        completo, sha = _wizard_load_raw()
        completo[str(chat_id)] = stato_wizard
        return _wizard_save_raw(completo, sha)
    except Exception:
        return False


def wizard_pren_clear(chat_id):
    """Rimuove lo stato del wizard /prenotazione per chat_id."""
    try:
        completo, sha = _wizard_load_raw()
        cid = str(chat_id)
        if cid in completo:
            del completo[cid]
            return _wizard_save_raw(completo, sha)
        return True
    except Exception:
        return False


# ── Storage turni pulizie (pulizie_turni.json su GitHub) ─────────────────────
def _pulizie_load_raw():
    """Legge pulizie_turni.json. Ritorna (dict, sha). 404 → ({}, None)."""
    if not GITHUB_TOKEN:
        return ({}, None)
    try:
        url = f"{PULIZIE_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=4)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        return (json.loads(contenuto) if contenuto.strip() else {}, data["sha"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ({}, None)
        return ({}, None)
    except Exception:
        return ({}, None)


def _pulizie_save_raw(turni, sha):
    """PUT pulizie_turni.json. Su conflitto refetch sha e ritenta una volta."""
    if not GITHUB_TOKEN:
        return False
    for _attempt in range(2):
        try:
            payload = {
                "message": "Aggiorna turni pulizie",
                "content": base64.b64encode(json.dumps(turni, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8"),
            }
            if sha:
                payload["sha"] = sha
            req = urllib.request.Request(PULIZIE_API, data=json.dumps(payload).encode(), headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "appartamento-bot"
            }, method="PUT")
            urllib.request.urlopen(req, timeout=8)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (409, 422):
                _, sha = _pulizie_load_raw()
                continue
            return False
        except Exception:
            return False
    return False


def pulizie_turno_id_for_checkout(checkout_date):
    """Chiave canonica turno per data check-out (DD/MM/YYYY → YYYYMMDD)."""
    try:
        dt = datetime.strptime(checkout_date, "%d/%m/%Y")
        return f"turno_{dt.strftime('%Y%m%d')}"
    except Exception:
        return None


def pulizie_upsert_turno(checkout, ospite_uscente, num_uscenti, culla_uscente,
                          next_checkin=None, ospite_entrante=None, num_entranti=0, culla_entrante=False):
    """Crea o aggiorna un turno per il checkout dato. Ritorna (id, turno) oppure (None, None)."""
    tid = pulizie_turno_id_for_checkout(checkout)
    if not tid:
        return (None, None)
    turni, sha = _pulizie_load_raw()
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    t = turni.get(tid) or {
        "checkout": checkout,
        "next_checkin": None,
        "ospite_uscente": "",
        "num_ospiti_uscenti": 0,
        "culla_uscente": False,
        "ospite_entrante": None,
        "num_ospiti_entranti": 0,
        "culla_entrante": False,
        "inviato_subito_at": None,
        "inviato_reminder_at": None,
        "confermato_at": None,
        "confermato_msg": "",
        "created_at": now,
    }
    # Aggiorna info uscente
    if ospite_uscente:
        t["ospite_uscente"] = ospite_uscente
    if num_uscenti:
        t["num_ospiti_uscenti"] = int(num_uscenti)
    t["culla_uscente"] = bool(culla_uscente)
    # Aggiorna info entrante (se passato)
    if next_checkin:
        t["next_checkin"] = next_checkin
    if ospite_entrante:
        t["ospite_entrante"] = ospite_entrante
    if num_entranti:
        t["num_ospiti_entranti"] = int(num_entranti)
    if culla_entrante is not None:
        t["culla_entrante"] = bool(culla_entrante)
    turni[tid] = t
    _pulizie_save_raw(turni, sha)
    return (tid, t)


def pulizie_mark_inviato(tid, tipo):
    """tipo = 'subito' | 'reminder'. Marca il turno con timestamp invio."""
    turni, sha = _pulizie_load_raw()
    if tid not in turni:
        return False
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    key = "inviato_subito_at" if tipo == "subito" else "inviato_reminder_at"
    turni[tid][key] = now
    return _pulizie_save_raw(turni, sha)


def pulizie_mark_confermato(tid, msg_testo):
    """Marca un turno come confermato dalla signora."""
    turni, sha = _pulizie_load_raw()
    if tid not in turni:
        return False
    turni[tid]["confermato_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    turni[tid]["confermato_msg"] = (msg_testo or "")[:200]
    return _pulizie_save_raw(turni, sha)


def pulizie_trova_ultimo_aperto():
    """Ritorna (id, turno) dell'ultimo turno inviato e non ancora confermato. Altrimenti None."""
    turni, _ = _pulizie_load_raw()
    aperti = [(k, v) for k, v in turni.items()
              if (v.get("inviato_subito_at") or v.get("inviato_reminder_at"))
              and not v.get("confermato_at")]
    if not aperti:
        return (None, None)
    # Ordina per data check-out più recente (decrescente)
    aperti.sort(key=lambda kv: kv[1].get("checkout", ""),
                reverse=False)
    # Prendi quello con checkout più vicino a oggi (oggi o ieri tipicamente)
    oggi = datetime.now().date()
    miglior = None
    miglior_diff = None
    for k, v in aperti:
        try:
            d = datetime.strptime(v["checkout"], "%d/%m/%Y").date()
            diff = abs((d - oggi).days)
            if miglior_diff is None or diff < miglior_diff:
                miglior = (k, v)
                miglior_diff = diff
        except Exception:
            continue
    return miglior or (aperti[-1][0], aperti[-1][1])


def pulizie_format_riepilogo():
    """Riepilogo turni pulizie per /pulizie. Mostra futuri + ultimi 3 passati."""
    turni, _ = _pulizie_load_raw()
    if not turni:
        return "🧹 Nessun turno pulizie registrato.\n\nI turni vengono creati automaticamente quando completi una prenotazione."
    oggi = datetime.now().date()
    items = []
    for tid, t in turni.items():
        try:
            d = datetime.strptime(t["checkout"], "%d/%m/%Y").date()
        except Exception:
            continue
        items.append((d, tid, t))
    items.sort(key=lambda x: x[0])
    futuri = [i for i in items if i[0] >= oggi]
    passati = [i for i in items if i[0] < oggi][-3:]
    out = ["🧹 *Turni pulizie*\n"]
    def render(d, tid, t):
        if t.get("confermato_at"):
            icon = "✅"
            stato = f"_confermato_"
        elif t.get("inviato_subito_at") or t.get("inviato_reminder_at"):
            icon = "📤"
            stato = "_inviato, in attesa OK_"
        else:
            icon = "⏳"
            stato = "_da inviare_"
        uscente = t.get("ospite_uscente") or "—"
        n_u = t.get("num_ospiti_uscenti", 0)
        entrante = t.get("ospite_entrante") or "vuoto"
        n_e = t.get("num_ospiti_entranti", 0)
        culla_e = " 🛏️" if t.get("culla_entrante") else ""
        riga_entra = f"{entrante} ({n_e}){culla_e}" if entrante and entrante != "vuoto" else "vuoto"
        return f"{icon} *{t['checkout']}* — {uscente} ({n_u}) → {riga_entra}   {stato}"
    if passati:
        out.append("\n_Ultimi turni:_")
        for d, tid, t in passati:
            out.append(render(d, tid, t))
    if futuri:
        out.append("\n_Prossimi turni:_")
        for d, tid, t in futuri:
            out.append(render(d, tid, t))
    if not futuri and not passati:
        out.append("_Nessun turno._")
    return "\n".join(out)


def _topic_di(testo):
    """Determina la categoria di una domanda dalle TOPIC_KEYWORDS."""
    t = (testo or "").lower()
    for topic, kws in TOPIC_KEYWORDS.items():
        if any(k in t for k in kws):
            return topic
    return "altro"

def is_ospite_tornato(chat_id):
    """True se questo cliente ha gia' scritto al bot in passato (>= 2 messaggi totali
    e ultimo contatto piu' di 30 giorni fa)."""
    try:
        _carica_users_da_github()
        u = _users.get(str(chat_id))
        if not u:
            return False
        if int(u.get("totale_msg", 0)) < 2:
            return False
        try:
            ultimo = datetime.strptime((u.get("ultimo_msg", "") or "")[:19], "%Y-%m-%dT%H:%M:%S")
            giorni_passati = (datetime.now() - ultimo).days
            # Almeno 30 giorni dall'ultimo contatto e flag bentornato non gia' inviato
            return giorni_passati >= 30 and not u.get("bentornato_inviato")
        except Exception:
            return False
    except Exception:
        return False

def marca_bentornato(chat_id):
    """Segna che il messaggio di bentornato e' stato inviato."""
    try:
        _carica_users_da_github()
        cid = str(chat_id)
        if cid in _users:
            _users[cid]["bentornato_inviato"] = True
            _salva_users_su_github()
    except Exception:
        pass

BENTORNATO = {
    "italian":  "Bentornato {nome}! 😊 Felice di rivederti. Come posso aiutarti questa volta?",
    "english":  "Welcome back {nome}! 😊 Happy to see you again. How can I help you this time?",
    "french":   "Re-bonjour {nome}! 😊 Heureux de te revoir. Comment puis-je t'aider cette fois?",
    "spanish":  "¡Bienvenido de nuevo {nome}! 😊 Encantado de verte otra vez. ¿En qué puedo ayudarte?",
    "german":   "Willkommen zurück {nome}! 😊 Schön, dich wiederzusehen. Wie kann ich dir helfen?",
    "portuguese":"Bem-vindo de volta {nome}! 😊 Feliz em te ver de novo. Como posso ajudar?",
}


def rileva_sentiment_negativo(testo):
    """Rileva se il testo dell'ospite mostra frustrazione/rabbia/insoddisfazione.
    Approccio keyword multilingua, conservativo per evitare falsi positivi."""
    if not testo:
        return False
    t = " " + testo.lower() + " "
    # Frasi/parole che indicano chiaramente frustrazione o rabbia
    indicatori = [
        # Italiano
        "schifo", "vergogna", "scandalo", "inaccettabile", "rimborso",
        "non funziona niente", "tutto rotto", "tutto sporco", "disgustoso",
        "deluso", "delusa", "delusione", "arrabbiato", "arrabbiata", "incazzat",
        "pessimo", "pessima", "orribile", "terribile", "disastro",
        "voglio andare via", "voglio andarmene", "me ne vado", "rivoglio i soldi",
        "denuncio", "denuncia", "avvocato", "tribunale", "polizia",
        "ridicolo", "vergognoso", "che cavolo", "ma cosa",
        # English
        " awful ", " terrible ", " disgusting ", " horrible ", " disaster ",
        " refund ", " unacceptable ", " ridiculous ",
        " disappointed ", " disappointing ", " angry ",
        "want to leave", "want my money back", "this is a joke",
        "i'll sue", "calling the police", "small claims",
        # French
        "honteux", "scandaleux", "inacceptable", "remboursement",
        "déçu", "déçue", "dégoûtant", "horrible", "catastrophe",
        "je porte plainte", "avocat", "tribunal",
        # Spanish
        "vergüenza", "escándalo", "inaceptable", "reembolso", "asco",
        "decepcionad", "horrible", "pésimo",
        # German
        "katastrophe", "skandal", "unzumutbar", "erstattung",
        "enttäuscht", "ekelhaft", "schrecklich",
    ]
    for ind in indicatori:
        if ind in t:
            return True
    # Controllo anche pattern "molto/troppo + parola negativa"
    if re.search(r'\b(molto|troppo|veramente|davvero)\s+(brutto|brutta|sporco|sporca|rotto|rotta|caldo|freddo|rumoros)', t):
        return True
    if re.search(r'\b(very|too|really)\s+(bad|dirty|broken|noisy|hot|cold)', t):
        return True
    return False

def is_in_takeover(chat_id):
    """True se la conversazione è in 'takeover' (Lorenzo l'ha presa in carico esplicitamente)."""
    try:
        _carica_users_da_github()
        u = _users.get(str(chat_id), {})
        return bool(u.get("in_takeover"))
    except Exception:
        return False

def set_takeover(chat_id, attivo):
    """Attiva/disattiva il takeover per un cliente."""
    try:
        _carica_users_da_github()
        cid = str(chat_id)
        if cid not in _users:
            _users[cid] = {"canale": "telegram" if not cid.startswith("wa_") else "whatsapp"}
        _users[cid]["in_takeover"] = bool(attivo)
        _users[cid]["pausa_ai"] = bool(attivo)  # takeover implica anche pausa AI
        _salva_users_su_github()
        return True
    except Exception:
        return False

# Messaggi takeover per lingua (mandati all'ospite quando Lorenzo prende la chat)
TAKEOVER_MSG = {
    "italian":  "Un attimo, ti rispondo io personalmente! 👋",
    "english":  "One moment, I'm taking over to reply personally! 👋",
    "french":   "Un instant, je te réponds personnellement! 👋",
    "spanish":  "¡Un momento, te respondo personalmente! 👋",
    "german":   "Einen Moment, ich antworte dir persönlich! 👋",
    "portuguese": "Um momento, vou responder pessoalmente! 👋",
}

def is_paused(chat_id):
    """True se la AI e' in pausa per questo cliente."""
    try:
        _carica_users_da_github()
        u = _users.get(str(chat_id), {})
        return bool(u.get("pausa_ai"))
    except Exception:
        return False

def set_pause(chat_id, paused):
    """Attiva/disattiva pausa AI per un cliente specifico."""
    try:
        _carica_users_da_github()
        cid = str(chat_id)
        if cid not in _users:
            _users[cid] = {"canale": "telegram" if not cid.startswith("wa_") else "whatsapp"}
        _users[cid]["pausa_ai"] = bool(paused)
        _salva_users_su_github()
        return True
    except Exception:
        return False

def aggiorna_user(chat_id, canale, nome, testo, lingua=None, username=None):
    """Aggiorna i metadati cumulativi del cliente. Best-effort, non blocca il bot."""
    try:
        _carica_users_da_github()
        cid = str(chat_id)
        ora_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        u = _users.get(cid) or {}
        u.setdefault("canale", canale)
        u.setdefault("primo_msg", ora_iso)
        u.setdefault("totale_msg", 0)
        u.setdefault("topic_count", {})
        if nome:
            u["nome"] = nome
        if username:
            u["username"] = username
        if lingua:
            u["lingua"] = lingua
        u["ultimo_msg"] = ora_iso
        u["totale_msg"] = int(u.get("totale_msg", 0)) + 1
        topic = _topic_di(testo)
        u["topic_count"][topic] = int(u["topic_count"].get(topic, 0)) + 1
        _users[cid] = u
        _salva_users_su_github()
    except Exception:
        pass


# ── Analytics: traccia eventi per heatmap, tempi risposta, trending ────────────
_analytics = []
_analytics_sha = None
_analytics_loaded = False
ANALYTICS_MAX_GIORNI = 30   # ritengo solo ultimi 30gg
ANALYTICS_MAX_EVENTI = 5000 # safety cap

def _carica_analytics_da_github():
    global _analytics, _analytics_sha, _analytics_loaded
    if _analytics_loaded or not GITHUB_TOKEN:
        _analytics_loaded = True
        return
    try:
        url = f"{ANALYTICS_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=4)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        _analytics = json.loads(contenuto) if contenuto.strip() else []
        _analytics_sha = data["sha"]
        # Cleanup: rimuovi eventi più vecchi di ANALYTICS_MAX_GIORNI giorni
        from datetime import timedelta as _td
        cutoff = (datetime.now() - _td(days=ANALYTICS_MAX_GIORNI)).strftime("%Y-%m-%dT%H:%M:%S")
        _analytics = [e for e in _analytics if e.get("ts", "") >= cutoff]
        # Cap totale eventi
        if len(_analytics) > ANALYTICS_MAX_EVENTI:
            _analytics = _analytics[-ANALYTICS_MAX_EVENTI:]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            _analytics = []
            _analytics_sha = None
    except Exception:
        pass
    finally:
        _analytics_loaded = True

def _salva_analytics_su_github():
    global _analytics_sha
    if not GITHUB_TOKEN:
        return
    try:
        contenuto_nuovo = json.dumps(_analytics, ensure_ascii=False)
        payload = {
            "message": "Aggiorna analytics events",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
        }
        if _analytics_sha:
            payload["sha"] = _analytics_sha
        req = urllib.request.Request(ANALYTICS_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        r = urllib.request.urlopen(req, timeout=8)
        risposta = json.loads(r.read())
        _analytics_sha = risposta.get("content", {}).get("sha", _analytics_sha)
    except urllib.error.HTTPError as e:
        if e.code in (409, 422):
            global _analytics_loaded
            _analytics_loaded = False
            _carica_analytics_da_github()
    except Exception:
        pass

def log_evento_analytics(canale, topic, durata_sec, takeover=False, non_risolto=False, era_vocale=False):
    """Aggiunge un evento al log analytics. Best-effort, non blocca."""
    try:
        _carica_analytics_da_github()
        evento = {
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "ch": canale[:2],          # "te" o "wh"
            "tp": (topic or "altro")[:20],
            "ds": round(float(durata_sec), 2) if durata_sec else 0,
            "to": bool(takeover),
            "nr": bool(non_risolto),
            "vo": bool(era_vocale),
        }
        _analytics.append(evento)
        # Auto-cleanup se troppi
        if len(_analytics) > ANALYTICS_MAX_EVENTI + 100:
            del _analytics[:100]
        _salva_analytics_su_github()
    except Exception:
        pass

def log_msg_non_risolto(testo, chat_id, lingua):
    """Salva la domanda non risolta in users.json sotto un campo dedicato."""
    try:
        _carica_users_da_github()
        cid = str(chat_id)
        u = _users.get(cid) or {}
        u.setdefault("non_risolti", [])
        if len(u["non_risolti"]) >= 20:
            u["non_risolti"] = u["non_risolti"][-19:]  # max 20 per utente
        u["non_risolti"].append({
            "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "domanda": (testo or "")[:200],
            "lingua": lingua
        })
        _users[cid] = u
        _salva_users_su_github()
    except Exception:
        pass


# ── Frasi "non so rispondere" ─────────────────────────────────────────────────
FRASI_NON_SO = [
    "contatterò il proprietario", "contatterai il proprietario",
    "contatterà il proprietario", "il proprietario sarà contattato",
    "non ho questa informazione", "non dispongo di",
    "i'll contact", "i will contact", "contact the owner", "i'll let the owner",
    "don't have that information", "don't have this information",
    "je vais contacter", "je contacterai", "le propriétaire sera contacté",
    "je n'ai pas cette information",
    "contactaré al propietario", "el propietario será contactado",
    "no tengo esa información",
    "ich werde den eigentümer", "werde den eigentümer kontaktieren",
]


# ── Parsing date ──────────────────────────────────────────────────────────────
MESI = {
    "january":1,"jan":1,"gennaio":1,"janvier":1,"enero":1,"januar":1,
    "february":2,"feb":2,"febbraio":2,"février":2,"fevrier":2,"febrero":2,"februar":2,
    "march":3,"mar":3,"marzo":3,"mars":3,"märz":3,"marz":3,
    "april":4,"apr":4,"aprile":4,"avril":4,"abril":4,
    "may":5,"maggio":5,"mai":5,"mayo":5,
    "june":6,"jun":6,"giugno":6,"juin":6,"junio":6,"juni":6,
    "july":7,"jul":7,"luglio":7,"juillet":7,"julio":7,"juli":7,
    "august":8,"aug":8,"agosto":8,"août":8,"aout":8,"august":8,
    "september":9,"sep":9,"sept":9,"settembre":9,"septembre":9,"septiembre":9,
    "october":10,"oct":10,"ottobre":10,"octobre":10,"octubre":10,"oktober":10,
    "november":11,"nov":11,"novembre":11,"noviembre":11,
    "december":12,"dec":12,"dicembre":12,"décembre":12,"decembre":12,"diciembre":12,"dezember":12,
}

def estrai_date(testo):
    """Estrae (checkin, checkout) da testo libero. Restituisce (None, None) se non trovate."""
    t = testo.lower()
    anno_corrente = datetime.now().year
    date_trovate = []

    # Pattern numerico: dd/mm, dd-mm, dd.mm con anno opzionale
    for m in re.finditer(r'(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?', t):
        g, me = int(m.group(1)), int(m.group(2))
        a = int(m.group(3)) if m.group(3) else anno_corrente
        if a < 100: a += 2000
        if 1 <= g <= 31 and 1 <= me <= 12:
            date_trovate.append(f"{g:02d}/{me:02d}/{a}")

    # Pattern testuale: "25 april", "25 avril 2026", ecc.
    nomi_mesi = "|".join(MESI.keys())
    for m in re.finditer(rf'(\d{{1,2}})\s+({nomi_mesi})(?:\s+(\d{{2,4}}))?', t):
        g = int(m.group(1))
        me = MESI[m.group(2)]
        a = int(m.group(3)) if m.group(3) else anno_corrente
        if a < 100: a += 2000
        if 1 <= g <= 31:
            candidato = f"{g:02d}/{me:02d}/{a}"
            if candidato not in date_trovate:
                date_trovate.append(candidato)

    if len(date_trovate) >= 2:
        return date_trovate[0], date_trovate[1]
    return None, None


# ── Messaggi date multilingua ─────────────────────────────────────────────────
DOMANDA_DATE = {
    "italian":  "📅 Per aiutarti al meglio, potresti indicarmi le date del tuo soggiorno?\n(Arrivo e partenza — anche in formato libero, es. \"25 aprile - 28 aprile\")",
    "english":  "📅 To assist you better, could you share your stay dates?\n(Arrival and departure — even in free format, e.g. \"April 25 - April 28\")",
    "french":   "📅 Pour mieux vous aider, pourriez-vous m'indiquer les dates de votre séjour?\n(Arrivée et départ — même en format libre, ex. \"25 avril - 28 avril\")",
    "spanish":  "📅 Para ayudarte mejor, ¿podrías indicarme las fechas de tu estancia?\n(Llegada y salida — incluso en formato libre, ej. \"25 abril - 28 abril\")",
    "german":   "📅 Um Ihnen besser helfen zu können, könnten Sie mir Ihre Aufenthaltsdaten mitteilen?\n(An- und Abreise — auch frei, z.B. \"25. April - 28. April\")",
}
CONFERMA_DATE = {
    "italian":  "✅ Perfetto! Ho registrato il tuo soggiorno:\n📆 Arrivo: {checkin}\n🏁 Partenza: {checkout}\n\nSe le date non sono corrette scrivimi e le sistemo subito!",
    "english":  "✅ Perfect! I've noted your stay:\n📆 Arrival: {checkin}\n🏁 Departure: {checkout}\n\nIf the dates are wrong, just let me know!",
    "french":   "✅ Parfait! J'ai enregistré votre séjour:\n📆 Arrivée: {checkin}\n🏁 Départ: {checkout}\n\nSi les dates ne sont pas correctes, dites-le moi!",
    "spanish":  "✅ ¡Perfecto! He registrado tu estancia:\n📆 Llegada: {checkin}\n🏁 Salida: {checkout}\n\n¡Si las fechas no son correctas, dímelo!",
    "german":   "✅ Perfekt! Ich habe Ihren Aufenthalt notiert:\n📆 Ankunft: {checkin}\n🏁 Abreise: {checkout}\n\nFalls die Daten falsch sind, lassen Sie es mich wissen!",
}
ERRORE_DATE = {
    "italian":  "Non ho capito le date 😊 Puoi scrivermele così?\n\nArrivo: 25/04/2026\nPartenza: 28/04/2026",
    "english":  "I didn't quite catch the dates 😊 Could you write them like this?\n\nArrival: 25/04/2026\nDeparture: 28/04/2026",
    "french":   "Je n'ai pas bien compris les dates 😊 Pourriez-vous les écrire ainsi?\n\nArrivée: 25/04/2026\nDépart: 28/04/2026",
    "spanish":  "No entendí las fechas 😊 ¿Puedes escribirlas así?\n\nLlegada: 25/04/2026\nSalida: 28/04/2026",
    "german":   "Ich habe die Daten nicht verstanden 😊 Könnten Sie sie so schreiben?\n\nAnkunft: 25/04/2026\nAbreise: 28/04/2026",
}


# ── Lettura appartamento.txt ──────────────────────────────────────────────────
_cache = {"testo": "", "ts": 0}
CACHE_TTL = 300

def leggi_testo():
    ora = datetime.now().timestamp()
    if _cache["testo"] and (ora - _cache["ts"]) < CACHE_TTL:
        return _cache["testo"]
    try:
        url = f"{GITHUB_RAW}?t={int(ora)}"
        req = urllib.request.Request(url, headers={
            "Cache-Control": "no-cache",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=4)
        testo = r.read().decode("utf-8")
        if testo.strip():
            _cache["testo"] = testo
            _cache["ts"] = ora
            return testo
    except Exception:
        pass
    try:
        with open(INFO_PATH, "r", encoding="utf-8") as f:
            testo = f.read()
            _cache["testo"] = testo
            _cache["ts"] = ora
            return testo
    except Exception:
        return ""

def invalida_cache():
    _cache["ts"] = 0

import sys as _sys


def _log_stderr(livello, contesto, msg):
    """Stampa su stderr (Vercel cattura nei logs). Best-effort, mai bloccante."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [{livello}] [{contesto}] {msg}", file=_sys.stderr, flush=True)
    except Exception:
        pass


def log_info(contesto, msg):
    _log_stderr("INFO", contesto, msg)


def log_warn(contesto, msg):
    _log_stderr("WARN", contesto, msg)


def log_errore(contesto, errore):
    """Notifica Lorenzo via Telegram di un errore + stampa su stderr per i log Vercel."""
    err_tipo = type(errore).__name__ if not isinstance(errore, str) else "Errore"
    err_msg = str(errore)[:500]
    _log_stderr("ERROR", contesto, f"{err_tipo}: {err_msg}")
    if not OWNER_ID or not TOKEN:
        return
    try:
        msg = f"⚠️ Bot errore [{contesto}]: {err_tipo}: {err_msg[:300]}"
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        data = json.dumps({"chat_id": int(OWNER_ID), "text": msg}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

def leggi_info():
    testo = leggi_testo()
    info = re.sub(r'\[MEDIA\].*', '', testo, flags=re.DOTALL).strip()
    return info if info else "Informazioni non disponibili."

def leggi_media():
    media = []
    testo = leggi_testo()
    match = re.search(r'\[MEDIA\](.*)', testo, re.DOTALL)
    if not match:
        return media
    for riga in match.group(1).strip().splitlines():
        riga = riga.strip()
        if not riga or riga.startswith("#") or "=" not in riga:
            continue
        sinistra, destra = riga.split("=", 1)
        keywords = [k.strip().lower() for k in sinistra.split(",")]
        parti = destra.strip().split("|", 1)
        tipo_id = parti[0].strip()
        caption = parti[1].strip() if len(parti) > 1 else ""
        tipo, file_id = tipo_id.split(":", 1) if ":" in tipo_id else ("photo", tipo_id)
        media.append({"keywords": keywords, "tipo": tipo.strip(), "file_id": file_id.strip(), "caption": caption})
    return media

def trova_media(domanda):
    for m in leggi_media():
        if any(k in domanda.lower() for k in m["keywords"]):
            return m
    return None


# ── GitHub: Media ─────────────────────────────────────────────────────────────
def cancella_media_su_github(indice_1based):
    """Cancella la riga N (1-based) dalla sezione [MEDIA] di appartamento.txt."""
    if not GITHUB_TOKEN:
        return None
    try:
        req = urllib.request.Request(GITHUB_API, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=10)
        data = json.loads(r.read())
        sha = data["sha"]
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")

        if "[MEDIA]" not in contenuto:
            return None

        testo_parte, media_parte = contenuto.split("[MEDIA]", 1)
        # Estrai righe valide (con "=") e ricostruisci
        righe_orig = media_parte.split("\n")
        righe_media = []      # [(idx_originale, riga)]
        for i, riga in enumerate(righe_orig):
            r_strip = riga.strip()
            if r_strip and not r_strip.startswith("#") and "=" in r_strip:
                righe_media.append((i, riga))

        if indice_1based < 1 or indice_1based > len(righe_media):
            return None

        # Rimuovi la riga dall'array originale
        idx_da_rimuovere = righe_media[indice_1based - 1][0]
        riga_rimossa = righe_orig[idx_da_rimuovere].strip()
        del righe_orig[idx_da_rimuovere]
        nuovo_media_parte = "[MEDIA]" + "\n".join(righe_orig)
        contenuto_nuovo = testo_parte.rstrip() + "\n\n" + nuovo_media_parte

        payload = {
            "message": f"Bot cancella media #{indice_1based}",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
            "sha": sha
        }
        req = urllib.request.Request(GITHUB_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        urllib.request.urlopen(req, timeout=15)
        invalida_cache()
        return riga_rimossa
    except Exception as e:
        try:
            log_errore("cancella_media", e)
        except Exception:
            pass
        return None


def salva_media_su_github(keywords, tipo, file_id, caption):
    if not GITHUB_TOKEN:
        return False
    try:
        req = urllib.request.Request(GITHUB_API, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=10)
        data = json.loads(r.read())
        sha = data["sha"]
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")

        # Separa testo e [MEDIA]
        if "[MEDIA]" in contenuto:
            testo_parte, media_parte = contenuto.split("[MEDIA]", 1)
            media_parte = "[MEDIA]" + media_parte
        else:
            testo_parte = contenuto
            media_parte = "[MEDIA]\n"

        # Aggiunge la nuova riga media nella sezione [MEDIA]
        nuova_riga_media = f"{keywords} = {tipo}:{file_id} | {caption}\n"
        if "[MEDIA]" in media_parte:
            media_parte = media_parte.replace("[MEDIA]\n", "[MEDIA]\n" + nuova_riga_media)
        else:
            media_parte = "[MEDIA]\n" + nuova_riga_media

        # Riorganizza il testo con Claude aggiungendo nota sul media
        tipo_label = "foto" if tipo == "photo" else "video"
        nota_media = f"Disponibile {tipo_label} con parole chiave: {keywords} — descrizione: {caption}"
        testo_riorganizzato = riorganizza_con_claude(testo_parte.strip(), nota_media)
        contenuto_nuovo = testo_riorganizzato.strip() + "\n\n" + media_parte

        payload = {
            "message": f"Bot salva media e riorganizza: {keywords[:40]}",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
            "sha": sha
        }
        req = urllib.request.Request(GITHUB_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        urllib.request.urlopen(req, timeout=15)
        invalida_cache()
        return True
    except Exception:
        return False


# ── Riorganizza il testo con Claude ──────────────────────────────────────────
def riorganizza_con_claude(testo_attuale, nuova_info):
    """Integra la nuova info nel file e riorganizza tutto con Claude."""
    try:
        prompt = (
            f"Gestisci questo file di informazioni su un appartamento vacanze.\n\n"
            f"FILE ATTUALE:\n{testo_attuale}\n\n"
            f"NUOVA INFORMAZIONE DA INTEGRARE:\n{nuova_info}\n\n"
            f"Istruzioni:\n"
            f"1. Inserisci la nuova info nella sezione più appropriata (se esiste) oppure crea una nuova sezione con titolo # NOME SEZIONE\n"
            f"2. Non duplicare informazioni già presenti\n"
            f"3. Mantieni il formato con # per i titoli delle sezioni\n"
            f"4. NON eliminare nessuna informazione esistente\n"
            f"5. Cita tutti i numeri (codici, indirizzi, piani) ESATTAMENTE come appaiono — non confonderli mai\n"
            f"6. Rispondi SOLO con il file completo riorganizzato, senza spiegazioni o commenti"
        )
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": "claude-haiku-4-5",
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}]
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01"
        })
        r = urllib.request.urlopen(req, timeout=40)
        return json.loads(r.read())["content"][0]["text"]
    except Exception:
        # Fallback: append semplice
        data_oggi = datetime.now().strftime("%d/%m/%Y")
        return testo_attuale + f"\n# Aggiunto il {data_oggi}\n{nuova_info}\n"


# ── GitHub: Q&A ──────────────────────────────────────────────────────────────
def _autosalva_qa(owner_chat_id, testo_originale, risposta_lorenzo):
    """Estrae la domanda originale dalla notifica, salva auto la Q&A in memoria
    e notifica Lorenzo del risultato. Per correggere errori: edita appartamento.txt su GitHub."""
    match_domanda = re.search(r'❓ "(.+?)"', testo_originale, re.DOTALL)
    if not match_domanda:
        match_domanda = re.search(r'❓ (.+?)(?:\n|$)', testo_originale)
    if not match_domanda:
        return
    domanda = match_domanda.group(1).strip()
    try:
        msg = invia_messaggio_get(owner_chat_id, f"💾 Salvataggio in memoria...\n\nD: {domanda}\nR: {risposta_lorenzo}")
        msg_id = (msg or {}).get("result", {}).get("message_id")
        salvato = salva_su_github(domanda, risposta_lorenzo)
        if msg_id:
            if salvato:
                modifica_messaggio(owner_chat_id, msg_id,
                    f"✅ *Salvato in memoria!*\n\nD: {domanda}\nR: {risposta_lorenzo}\n\n"
                    f"_Per correggere/rimuovere: edita appartamento.txt su GitHub._",
                    parse_mode="Markdown")
            else:
                modifica_messaggio(owner_chat_id, msg_id,
                    f"❌ Errore nel salvataggio in memoria.\n\nD: {domanda}\nR: {risposta_lorenzo}")
    except Exception:
        pass


def invia_messaggio_get(chat_id, testo, parse_mode=None):
    """Come invia_messaggio ma ritorna la risposta Telegram (per ottenere il message_id)."""
    payload = {"chat_id": chat_id, "text": testo}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        return telegram("sendMessage", payload)
    except Exception:
        return None


def salva_su_github(domanda, risposta):
    if not GITHUB_TOKEN:
        return False
    try:
        req = urllib.request.Request(GITHUB_API, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=10)
        data = json.loads(r.read())
        sha = data["sha"]
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")

        # Separa la sezione [MEDIA] (non va toccata)
        if "[MEDIA]" in contenuto:
            testo_parte, media_parte = contenuto.split("[MEDIA]", 1)
            media_parte = "[MEDIA]" + media_parte
        else:
            testo_parte = contenuto
            media_parte = ""

        testo_riga = domanda if not risposta else f"{domanda}: {risposta}"
        testo_riorganizzato = riorganizza_con_claude(testo_parte.strip(), testo_riga)
        contenuto_nuovo = testo_riorganizzato.strip() + ("\n\n" + media_parte if media_parte else "")

        payload = {
            "message": f"Bot apprende e riorganizza: {domanda[:60]}",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
            "sha": sha
        }
        req = urllib.request.Request(GITHUB_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        urllib.request.urlopen(req, timeout=15)
        invalida_cache()
        return True
    except Exception:
        return False


# ── GitHub: Prenotazioni ──────────────────────────────────────────────────────
def carica_prenotazioni():
    try:
        url = f"{BOOKINGS_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=5)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        return json.loads(contenuto), data["sha"]
    except Exception:
        return {}, None

def salva_prenotazione(chat_id, nome, checkin, checkout, lingua, num_ospiti=0, culla=False):
    if not GITHUB_TOKEN:
        return False
    try:
        prenotazioni, sha = carica_prenotazioni()
        prenotazioni[str(chat_id)] = {
            "nome": nome,
            "checkin": checkin,
            "checkout": checkout,
            "lingua": lingua,
            "num_ospiti": int(num_ospiti or 0),
            "culla": bool(culla),
            "salvata": datetime.now().strftime("%d/%m/%Y %H:%M")
        }
        contenuto_nuovo = json.dumps(prenotazioni, ensure_ascii=False, indent=2)
        payload = {
            "message": f"Prenotazione: {nome} {checkin}-{checkout}",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
        }
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(BOOKINGS_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception:
        return False


def salva_tutte_prenotazioni(prenotazioni):
    """Sovrascrive bookings.json con il dict completo. Usato dal cron per aggiornare flag reminder."""
    if not GITHUB_TOKEN:
        return False
    try:
        _, sha = carica_prenotazioni()
        contenuto_nuovo = json.dumps(prenotazioni, ensure_ascii=False, indent=2)
        payload = {
            "message": "Aggiorna flag reminder prenotazioni",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
        }
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(BOOKINGS_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        try:
            log_errore("salva_prenotazioni", e)
        except Exception:
            pass
        return False


# ── Template promemoria automatici (5 lingue) ─────────────────────────────────
PROMEMORIA_PRE_ARRIVO = {
    "italian":  "Ciao {nome}! 😊 Domani ti aspettiamo a Juan les Pins.\n\n📅 Check-in: dalle 16:00\n🔑 Codice KeyBox: 8492\n🚗 Ingresso garage: 67 Chemin des Liserons, Antibes\n📍 https://maps.google.com/?q=67+Chemin+des+Liserons+Antibes\n\n🏠 Appartamento: 93 Bd Raymond Poincaré, piano 2°, porta 23\n📍 https://maps.google.com/?q=93+Bd+Raymond+Poincare+Antibes\n\nPer qualsiasi cosa scrivimi pure qui!",
    "english":  "Hi {nome}! 😊 We're expecting you tomorrow in Juan les Pins.\n\n📅 Check-in: from 16:00\n🔑 KeyBox code: 8492\n🚗 Garage entrance: 67 Chemin des Liserons, Antibes\n📍 https://maps.google.com/?q=67+Chemin+des+Liserons+Antibes\n\n🏠 Apartment: 93 Bd Raymond Poincaré, 2nd floor, door 23\n📍 https://maps.google.com/?q=93+Bd+Raymond+Poincare+Antibes\n\nWrite me anytime if you need anything!",
    "french":   "Salut {nome}! 😊 On t'attend demain à Juan les Pins.\n\n📅 Check-in: à partir de 16h\n🔑 Code KeyBox: 8492\n🚗 Entrée garage: 67 Chemin des Liserons, Antibes\n📍 https://maps.google.com/?q=67+Chemin+des+Liserons+Antibes\n\n🏠 Appartement: 93 Bd Raymond Poincaré, 2ème étage, porte 23\n📍 https://maps.google.com/?q=93+Bd+Raymond+Poincare+Antibes\n\nÉcris-moi pour toute question!",
    "spanish":  "¡Hola {nome}! 😊 Mañana te esperamos en Juan les Pins.\n\n📅 Check-in: desde las 16:00\n🔑 Código KeyBox: 8492\n🚗 Entrada garaje: 67 Chemin des Liserons, Antibes\n📍 https://maps.google.com/?q=67+Chemin+des+Liserons+Antibes\n\n🏠 Apartamento: 93 Bd Raymond Poincaré, 2° piso, puerta 23\n📍 https://maps.google.com/?q=93+Bd+Raymond+Poincare+Antibes\n\n¡Escríbeme para cualquier cosa!",
    "german":   "Hallo {nome}! 😊 Wir erwarten dich morgen in Juan les Pins.\n\n📅 Check-in: ab 16:00 Uhr\n🔑 KeyBox-Code: 8492\n🚗 Garage-Eingang: 67 Chemin des Liserons, Antibes\n📍 https://maps.google.com/?q=67+Chemin+des+Liserons+Antibes\n\n🏠 Wohnung: 93 Bd Raymond Poincaré, 2. Stock, Tür 23\n📍 https://maps.google.com/?q=93+Bd+Raymond+Poincare+Antibes\n\nSchreib mir bei Fragen!",
}

PROMEMORIA_ARRIVO = {
    "italian":  "Buongiorno {nome}! 🌞 Oggi è il giorno!\n\nTutto pronto per il tuo arrivo dalle 16:00. Riepilogo veloce:\n🔑 KeyBox: codice 8492 (a 67 Chemin des Liserons)\n🚗 Posto auto: numero 53\n🏠 Appartamento 23 al 2° piano\n\nBuon viaggio! 🛣️",
    "english":  "Good morning {nome}! 🌞 Today's the day!\n\nReady for your arrival from 16:00. Quick recap:\n🔑 KeyBox: code 8492 (at 67 Chemin des Liserons)\n🚗 Parking spot: #53\n🏠 Apartment 23 on 2nd floor\n\nSafe travels! 🛣️",
    "french":   "Bonjour {nome}! 🌞 C'est le grand jour!\n\nTout est prêt pour ton arrivée à partir de 16h. Récap:\n🔑 KeyBox: code 8492 (à 67 Chemin des Liserons)\n🚗 Place de parking: n°53\n🏠 Appartement 23 au 2ème étage\n\nBon voyage! 🛣️",
    "spanish":  "¡Buenos días {nome}! 🌞 ¡Hoy es el día!\n\nTodo listo para tu llegada desde las 16:00. Resumen:\n🔑 KeyBox: código 8492 (en 67 Chemin des Liserons)\n🚗 Plaza de parking: #53\n🏠 Apartamento 23 en el 2° piso\n\n¡Buen viaje! 🛣️",
    "german":   "Guten Morgen {nome}! 🌞 Heute ist der Tag!\n\nAlles bereit für deine Ankunft ab 16:00. Zusammenfassung:\n🔑 KeyBox: Code 8492 (in 67 Chemin des Liserons)\n🚗 Parkplatz: Nr. 53\n🏠 Wohnung 23 im 2. Stock\n\nGute Reise! 🛣️",
}

PROMEMORIA_CHECKOUT = {
    "italian":  "Buongiorno {nome}, ricorda: check-out entro le 10:00.\n\n📝 Procedura:\n1. Prendi le chiavi con te\n2. Chiudi finestre, spegni AC e luci\n3. Porta fuori i rifiuti (locale al piano terra, lato cortile)\n4. Scendi in garage, prendi l'auto\n5. Lascia chiavi + telecomando nella KeyBox e richiudila\n\nGrazie e buon viaggio! 👋",
    "english":  "Good morning {nome}, reminder: check-out by 10:00.\n\n📝 Procedure:\n1. Take the keys with you\n2. Close windows, turn off AC and lights\n3. Take out the trash (ground floor, courtyard side)\n4. Go to garage, take your car\n5. Leave keys + remote in KeyBox and lock it\n\nThanks and safe travels! 👋",
    "french":   "Bonjour {nome}, rappel: check-out avant 10h.\n\n📝 Procédure:\n1. Prends les clés avec toi\n2. Ferme fenêtres, éteins clim et lumières\n3. Sors les poubelles (RDC, côté cour)\n4. Descends au garage, prends ta voiture\n5. Laisse clés + télécommande dans la KeyBox et referme-la\n\nMerci et bon voyage! 👋",
    "spanish":  "Buenos días {nome}, recordatorio: check-out antes de las 10:00.\n\n📝 Procedimiento:\n1. Llévate las llaves\n2. Cierra ventanas, apaga AC y luces\n3. Saca la basura (planta baja, lado patio)\n4. Baja al garaje, coge el coche\n5. Deja llaves + mando en la KeyBox y ciérrala\n\n¡Gracias y buen viaje! 👋",
    "german":   "Guten Morgen {nome}, Erinnerung: Check-out bis 10:00 Uhr.\n\n📝 Ablauf:\n1. Nimm die Schlüssel mit\n2. Fenster schließen, Klima und Licht aus\n3. Müll rausbringen (EG, Hofseite)\n4. Garage runter, ins Auto\n5. Schlüssel + Fernbedienung in die KeyBox legen und abschließen\n\nDanke und gute Reise! 👋",
}

PROMEMORIA_RECENSIONE = {
    "italian":  "Ciao {nome}! 😊 Speriamo che il tuo soggiorno a Juan les Pins sia stato fantastico.\n\nSe hai 30 secondi, una recensione su Booking o Airbnb ci aiuterebbe tantissimo. ⭐\n\nGrazie di cuore e a presto per la prossima vacanza! 🌊",
    "english":  "Hi {nome}! 😊 We hope your stay in Juan les Pins was amazing.\n\nIf you have 30 seconds, a review on Booking or Airbnb would mean the world to us. ⭐\n\nThanks so much and see you for the next holiday! 🌊",
    "french":   "Salut {nome}! 😊 On espère que ton séjour à Juan les Pins a été génial.\n\nSi tu as 30 secondes, un avis sur Booking ou Airbnb nous aiderait énormément. ⭐\n\nMerci infiniment et à bientôt pour les prochaines vacances! 🌊",
    "spanish":  "¡Hola {nome}! 😊 Esperamos que tu estancia en Juan les Pins haya sido genial.\n\nSi tienes 30 segundos, una reseña en Booking o Airbnb nos ayudaría muchísimo. ⭐\n\n¡Gracias de corazón y hasta las próximas vacaciones! 🌊",
    "german":   "Hallo {nome}! 😊 Wir hoffen dein Aufenthalt in Juan les Pins war fantastisch.\n\nWenn du 30 Sekunden hast, eine Bewertung auf Booking oder Airbnb würde uns sehr helfen. ⭐\n\nHerzlichen Dank und bis zum nächsten Urlaub! 🌊",
}

# Mapping tipo promemoria → nome template Meta approvato
TIPO_A_TEMPLATE = {
    "pre_arrivo": "pre_arrivo_v2",
    "arrivo":     "arrivo_oggi",
    "check_out":  "check_out_oggi",
    "recensione": "richiesta_recensione",
}


def _dentro_finestra_24h(chat_id_wa):
    """True se l'ospite WhatsApp ha scritto al bot nelle ultime 24h
    (finestra di servizio Meta in cui i messaggi free-form sono gratuiti)."""
    try:
        _carica_users_da_github()
        u = _users.get(str(chat_id_wa))
        if not u:
            return False
        ultimo = (u.get("ultimo_msg", "") or "")[:19]
        if not ultimo:
            return False
        dt_ultimo = datetime.strptime(ultimo, "%Y-%m-%dT%H:%M:%S")
        return (datetime.now() - dt_ultimo).total_seconds() < 86400
    except Exception:
        return False


def _invia_a_cliente(chat_id_str, testo, nome_ospite="ospite", tipo_promemoria=""):
    """Invio promemoria a un cliente. Logica:
    - Telegram (chat_id numerico): invio diretto, automatico.
    - WhatsApp DENTRO finestra 24h: invio diretto via wa_invia() (gratis).
    - WhatsApp FUORI finestra 24h: invio template Meta approvato (apre la
      conversazione di servizio). Ricaduta: notifica Lorenzo per copia/incolla.
    - Canale assente o ramo manuale: notifica Lorenzo come prima."""
    try:
        cid = str(chat_id_str)
        # Telegram: invio automatico
        if cid.isdigit():
            invia_messaggio(int(cid), testo)
            return True
        # WhatsApp: prova invio automatico (free-form o template)
        if cid.startswith("wa_"):
            numero_wa = cid.replace("wa_", "")
            # 1) Dentro finestra 24h → testo completo gratuito
            if _dentro_finestra_24h(cid):
                try:
                    wa_invia(numero_wa, testo)
                    if OWNER_ID:
                        try:
                            invia_messaggio(
                                int(OWNER_ID),
                                f"✅ Promemoria auto-inviato a *{nome_ospite}* via WhatsApp (finestra 24h)",
                                parse_mode="Markdown"
                            )
                        except Exception:
                            pass
                    return True
                except Exception:
                    pass  # cade su template/manuale
            # 2) Fuori finestra → template Meta
            template_name = TIPO_A_TEMPLATE.get(tipo_promemoria)
            if template_name and wa_invia_template(numero_wa, template_name, nome_ospite):
                if OWNER_ID:
                    try:
                        invia_messaggio(
                            int(OWNER_ID),
                            f"✅ Template `{template_name}` inviato a *{nome_ospite}* (+{numero_wa})",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                return True
        # 3) Fallback: notifica Lorenzo per invio manuale (copia/incolla)
        if not OWNER_ID:
            return False
        import urllib.parse as _urlp
        if cid.startswith("wa_"):
            numero_wa = cid.replace("wa_", "")
            link_wa = f"https://wa.me/{numero_wa}?text={_urlp.quote(testo)}"
            etichetta_canale = f"📱 WhatsApp: +{numero_wa} _(invio auto fallito)_"
            bottoni = [[
                {"text": "📲 Apri WhatsApp con messaggio precompilato", "url": link_wa}
            ]]
        else:
            etichetta_canale = "📝 _Nessun canale impostato — copia manualmente_"
            bottoni = None
        tipo_label = {
            "pre_arrivo": "📬 PROMEMORIA PRE-ARRIVO",
            "arrivo": "🌞 PROMEMORIA GIORNO DI ARRIVO",
            "check_out": "🏁 PROMEMORIA CHECK-OUT",
            "recensione": "⭐ RICHIESTA RECENSIONE",
        }.get(tipo_promemoria, "📤 PROMEMORIA")
        msg_a_lorenzo = (
            f"{tipo_label}\n\n"
            f"👤 Ospite: *{nome_ospite}*\n"
            f"{etichetta_canale}\n\n"
            f"💬 *Testo da inviare:*\n\n"
            f"```\n{testo}\n```"
        )
        if bottoni:
            invia_bottoni(int(OWNER_ID), msg_a_lorenzo, bottoni, parse_mode="Markdown")
        else:
            invia_messaggio(int(OWNER_ID), msg_a_lorenzo, parse_mode="Markdown")
        return True
    except Exception as e:
        try:
            log_errore("invia_a_cliente", e)
        except Exception:
            pass
        return False


# ── Invio turno pulizie via WhatsApp ─────────────────────────────────────────
def _fmt_data_pulizie(data_str):
    """22/06/2026 → 'lunedì 22/06'"""
    try:
        dt = datetime.strptime(data_str, "%d/%m/%Y")
        giorni = ["lunedì","martedì","mercoledì","giovedì","venerdì","sabato","domenica"]
        return f"{giorni[dt.weekday()]} {dt.strftime('%d/%m')}"
    except Exception:
        return data_str or "?"


def _testo_turno_pulizie(turno):
    """Compone il messaggio markdown del turno per la signora."""
    co_str = _fmt_data_pulizie(turno.get("checkout", ""))
    uscente = turno.get("ospite_uscente") or "—"
    n_uscenti = int(turno.get("num_ospiti_uscenti") or 0)
    culla_u = turno.get("culla_uscente")
    parts = [f"🧹 *Turno pulizie*\n"]
    parts.append(f"📅 *Check-out:* {co_str} (ore 10:00)")
    parts.append(f"👤 Esce: *{uscente}* — {n_uscenti} ospit{'e' if n_uscenti==1 else 'i'}")
    if culla_u:
        parts.append(f"🛏️ Culla da smontare")
    nc = turno.get("next_checkin")
    if nc:
        nc_str = _fmt_data_pulizie(nc)
        entrante = turno.get("ospite_entrante") or "—"
        n_entranti = int(turno.get("num_ospiti_entranti") or 0)
        culla_e = turno.get("culla_entrante")
        parts.append(f"\n📅 *Check-in:* {nc_str} (ore 16:00)")
        parts.append(f"👤 Entra: *{entrante}* — {n_entranti} ospit{'e' if n_entranti==1 else 'i'}")
        if culla_e:
            parts.append(f"🛏️ *Culla da MONTARE* ⚠️")
    else:
        parts.append(f"\n_(nessun check-in lo stesso giorno)_")
    parts.append(f"\nQuando hai visto rispondi *ok* così so che hai letto. Grazie! 🙏")
    return "\n".join(parts)


def invia_turno_pulizie(turno, tipo):
    """tipo = 'subito' | 'reminder'. Invia il messaggio alla signora via WA.
    Ritorna True se inviato (auto o template), False se fallback manuale."""
    if not WA_PULIZIE:
        # Niente numero configurato: notifica Lorenzo per copia/incolla
        if OWNER_ID:
            try:
                testo = _testo_turno_pulizie(turno)
                invia_messaggio(int(OWNER_ID),
                    f"⚠️ *WA_PULIZIE non configurato*\n\n"
                    f"Copia/incolla questo messaggio alla signora:\n\n"
                    f"```\n{testo}\n```",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        return False
    testo = _testo_turno_pulizie(turno)
    prefisso_tipo = "🔔 *Reminder turno di oggi:*\n\n" if tipo == "reminder" else ""
    testo_finale = prefisso_tipo + testo
    # Tentativo 1: invio diretto (gratis se in finestra 24h)
    try:
        wa_invia(WA_PULIZIE, testo_finale)
        if OWNER_ID:
            try:
                invia_messaggio(int(OWNER_ID),
                    f"✅ Turno pulizie {tipo} inviato a {NOME_PULIZIE} (+{WA_PULIZIE})",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        return True
    except Exception:
        pass
    # Tentativo 2: template Meta dedicato (se approvato — fallisce silenzioso se no)
    try:
        if wa_invia_template(WA_PULIZIE, "turno_pulizie", NOME_PULIZIE):
            # Dopo template (apre conv) prova a mandare il dettaglio in free-form
            try:
                wa_invia(WA_PULIZIE, testo_finale)
            except Exception:
                pass
            if OWNER_ID:
                try:
                    invia_messaggio(int(OWNER_ID),
                        f"✅ Template `turno_pulizie` inviato a {NOME_PULIZIE}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            return True
    except Exception:
        pass
    # Fallback: notifica Lorenzo per copia/incolla manuale
    if OWNER_ID:
        try:
            import urllib.parse as _urlp
            link_wa = f"https://wa.me/{WA_PULIZIE}?text={_urlp.quote(testo_finale)}"
            invia_bottoni(int(OWNER_ID),
                f"📤 *Turno pulizie da inviare manualmente*\n\n"
                f"_Auto-invio fallito (fuori finestra 24h / template non approvato)_\n\n"
                f"```\n{testo_finale}\n```",
                [[{"text": "📲 Apri WhatsApp precompilato", "url": link_wa}]],
                parse_mode="Markdown"
            )
        except Exception:
            pass
    return False


def pulizie_trigger_da_prenotazione(nome, checkin, checkout, num_ospiti, culla):
    """Hook chiamato quando una prenotazione viene completata o creata manualmente.
    Aggiorna due turni:
      - Turno del CHECKOUT di questa prenotazione (l'ospite ESCE)
      - Turno del CHECKIN di questa prenotazione (l'ospite ENTRA — pulizia di chi è uscito)
    Se sono il primo invio per il turno, parte la notifica 'subito'."""
    if not (checkin and checkout):
        return
    # 1) Turno del CHECKOUT (questo ospite esce)
    tid_out, _t = pulizie_upsert_turno(
        checkout=checkout,
        ospite_uscente=nome, num_uscenti=num_ospiti, culla_uscente=culla,
    )
    # Se c'è un'altra prenotazione che fa check-in il giorno del check-out di questa,
    # popoliamo anche la sezione "entrante". Cerchiamo sia in calendar_events.json
    # sia in bookings.json.
    if tid_out:
        try:
            entrante = _pulizie_trova_entrante(checkout)
            if entrante:
                pulizie_upsert_turno(
                    checkout=checkout,
                    ospite_uscente=nome, num_uscenti=num_ospiti, culla_uscente=culla,
                    next_checkin=entrante["checkin"],
                    ospite_entrante=entrante.get("nome",""),
                    num_entranti=entrante.get("num_ospiti",0),
                    culla_entrante=entrante.get("culla", False),
                )
        except Exception:
            pass
    # 2) Turno del CHECKIN di questa prenotazione (qualcun altro esce, questo entra)
    tid_in, _t2 = pulizie_upsert_turno(
        checkout=checkin,  # il "checkout" di QUEL turno = il checkin di questa
        ospite_uscente="",  # non noto da qui, viene popolato dall'altra parte se esiste
        num_uscenti=0, culla_uscente=False,
        next_checkin=checkin,
        ospite_entrante=nome, num_entranti=num_ospiti, culla_entrante=culla,
    )
    # Se c'è una prenotazione che fa check-out lo stesso giorno, popola l'uscente
    if tid_in:
        try:
            uscente = _pulizie_trova_uscente(checkin)
            if uscente:
                pulizie_upsert_turno(
                    checkout=checkin,
                    ospite_uscente=uscente.get("nome",""),
                    num_uscenti=uscente.get("num_ospiti",0),
                    culla_uscente=uscente.get("culla", False),
                    next_checkin=checkin,
                    ospite_entrante=nome, num_entranti=num_ospiti, culla_entrante=culla,
                )
        except Exception:
            pass
    # Manda notifica "subito" per ogni turno che non è ancora stato inviato
    turni, _ = _pulizie_load_raw()
    for tid in (tid_out, tid_in):
        if not tid or tid not in turni:
            continue
        t = turni[tid]
        if not t.get("inviato_subito_at"):
            try:
                if invia_turno_pulizie(t, "subito"):
                    pulizie_mark_inviato(tid, "subito")
            except Exception:
                pass


def _pulizie_trova_entrante(data_str):
    """Trova una prenotazione (manuale o canale) che ha checkin == data_str. Ritorna dict o None."""
    # Prenotazioni manuali
    try:
        prenotazioni, _ = carica_prenotazioni()
        for cid, p in prenotazioni.items():
            if p.get("checkin") == data_str:
                return {
                    "nome": p.get("nome",""),
                    "checkin": data_str,
                    "num_ospiti": p.get("num_ospiti", 0),
                    "culla": p.get("culla", False),
                }
    except Exception:
        pass
    # Eventi canale
    try:
        eventi, _ = _cal_load_events()
        for k, ev in eventi.items():
            if ev.get("checkin") == data_str and ev.get("stato") == "complete":
                return {
                    "nome": ev.get("nome",""),
                    "checkin": data_str,
                    "num_ospiti": ev.get("num_ospiti", 0),
                    "culla": ev.get("culla", False),
                }
    except Exception:
        pass
    return None


def _pulizie_trova_uscente(data_str):
    """Trova prenotazione con checkout == data_str. Ritorna dict o None."""
    try:
        prenotazioni, _ = carica_prenotazioni()
        for cid, p in prenotazioni.items():
            if p.get("checkout") == data_str:
                return {
                    "nome": p.get("nome",""),
                    "checkout": data_str,
                    "num_ospiti": p.get("num_ospiti", 0),
                    "culla": p.get("culla", False),
                }
    except Exception:
        pass
    try:
        eventi, _ = _cal_load_events()
        for k, ev in eventi.items():
            if ev.get("checkout") == data_str and ev.get("stato") == "complete":
                return {
                    "nome": ev.get("nome",""),
                    "checkout": data_str,
                    "num_ospiti": ev.get("num_ospiti", 0),
                    "culla": ev.get("culla", False),
                }
    except Exception:
        pass
    return None


def esegui_reminder_pulizie():
    """Cron mattina: per ogni turno con checkout == oggi e reminder non ancora inviato, manda reminder."""
    inviati = 0
    try:
        oggi = datetime.now().date()
        turni, _ = _pulizie_load_raw()
        for tid, t in turni.items():
            try:
                co_d = datetime.strptime(t["checkout"], "%d/%m/%Y").date()
            except Exception:
                continue
            if co_d != oggi:
                continue
            if t.get("inviato_reminder_at"):
                continue
            if t.get("confermato_at"):
                continue
            if invia_turno_pulizie(t, "reminder"):
                pulizie_mark_inviato(tid, "reminder")
                inviati += 1
    except Exception as e:
        try:
            log_errore("esegui_reminder_pulizie", e)
        except Exception:
            pass
    return inviati


def esegui_promemoria():
    """Loop sulle prenotazioni e manda i promemoria del giorno (pre-arrivo, arrivo, check-out, recensione).
    Idempotente: usa flag reminder_inviati per non inviare due volte."""
    from datetime import timedelta
    inviati = {"pre_arrivo": 0, "arrivo": 0, "check_out": 0, "recensione": 0, "errori": 0}
    try:
        prenotazioni, _ = carica_prenotazioni()
        if not prenotazioni:
            return inviati
        oggi = datetime.now().date()
        domani = oggi + timedelta(days=1)
        ieri = oggi - timedelta(days=1)

        def _prenota_e_invia(chat_id, p, r, tipo, msg, nome):
            # Idempotenza: marca PRIMA dell'invio e persisti, poi invia.
            # Se l'invio fallisce, resetta il flag e persisti di nuovo per ritentare al prossimo cron.
            r[tipo] = datetime.now().isoformat()
            try:
                salva_tutte_prenotazioni(prenotazioni)
            except Exception:
                # Se non riesco a persistere il "lock", evito di inviare per non rischiare doppi invii al prossimo cron
                r[tipo] = None
                return False
            ok = _invia_a_cliente(chat_id, msg, nome_ospite=nome, tipo_promemoria=tipo)
            if not ok:
                r[tipo] = None
                try:
                    salva_tutte_prenotazioni(prenotazioni)
                except Exception:
                    pass
            return ok

        for chat_id, p in prenotazioni.items():
            try:
                ci_d = datetime.strptime(p.get("checkin", ""), "%d/%m/%Y").date()
                co_d = datetime.strptime(p.get("checkout", ""), "%d/%m/%Y").date()
            except Exception:
                continue
            lingua = p.get("lingua", "italian")
            nome = p.get("nome", "ospite")
            r = p.setdefault("reminder_inviati", {})
            # PRE-ARRIVO (giorno prima)
            if ci_d == domani and not r.get("pre_arrivo"):
                msg = PROMEMORIA_PRE_ARRIVO.get(lingua, PROMEMORIA_PRE_ARRIVO["english"]).format(nome=nome)
                if _prenota_e_invia(chat_id, p, r, "pre_arrivo", msg, nome):
                    inviati["pre_arrivo"] += 1
                else:
                    inviati["errori"] += 1
            # ARRIVO (giorno stesso)
            if ci_d == oggi and not r.get("arrivo"):
                msg = PROMEMORIA_ARRIVO.get(lingua, PROMEMORIA_ARRIVO["english"]).format(nome=nome)
                if _prenota_e_invia(chat_id, p, r, "arrivo", msg, nome):
                    inviati["arrivo"] += 1
                else:
                    inviati["errori"] += 1
            # CHECK-OUT (giorno stesso)
            if co_d == oggi and not r.get("check_out"):
                msg = PROMEMORIA_CHECKOUT.get(lingua, PROMEMORIA_CHECKOUT["english"]).format(nome=nome)
                if _prenota_e_invia(chat_id, p, r, "check_out", msg, nome):
                    inviati["check_out"] += 1
                else:
                    inviati["errori"] += 1
            # RECENSIONE (giorno dopo check-out)
            if co_d == ieri and not r.get("recensione"):
                msg = PROMEMORIA_RECENSIONE.get(lingua, PROMEMORIA_RECENSIONE["english"]).format(nome=nome)
                if _prenota_e_invia(chat_id, p, r, "recensione", msg, nome):
                    inviati["recensione"] += 1
                else:
                    inviati["errori"] += 1
    except Exception as e:
        try:
            log_errore("cron_promemoria", e)
        except Exception:
            pass
        inviati["errori"] += 1
    return inviati


def esegui_report_mensile():
    """Genera e manda a Lorenzo il report del mese appena trascorso."""
    try:
        ora = datetime.now()
        # Mese precedente
        if ora.month == 1:
            mese_p = 12
            anno_p = ora.year - 1
        else:
            mese_p = ora.month - 1
            anno_p = ora.year
        nomi_mesi = ["", "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                     "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
        mese_nome = nomi_mesi[mese_p]

        # Stats lifetime
        s, _ = carica_stats()
        totale = s.get("totale", 0)
        argomenti = s.get("argomenti", {})
        lingue = s.get("lingue", {})

        # Users
        _carica_users_da_github()
        n_clienti = len(_users)
        ora_iso = datetime.now()
        attivi_7gg = 0
        nuovi_mese = 0
        for u in _users.values():
            try:
                primo = datetime.strptime((u.get("primo_msg", "") or "")[:7], "%Y-%m")
                if primo.year == anno_p and primo.month == mese_p:
                    nuovi_mese += 1
            except Exception:
                pass
            try:
                ult = datetime.strptime((u.get("ultimo_msg", "") or "")[:19], "%Y-%m-%dT%H:%M:%S")
                if (ora_iso - ult).total_seconds() < 7 * 86400:
                    attivi_7gg += 1
            except Exception:
                pass

        # Prenotazioni
        prenotazioni, _ = carica_prenotazioni()
        n_pren = len(prenotazioni)

        # Argomenti top 5
        top_arg = sorted(argomenti.items(), key=lambda x: -x[1])[:5]
        arg_str = "\n".join([f"{i+1}. {k.capitalize()}: {v}" for i, (k, v) in enumerate(top_arg)]) or "—"

        # Lingue top 5
        top_ling = sorted(lingue.items(), key=lambda x: -x[1])[:5]
        ling_str = "\n".join([f"• {k.capitalize()}: {v}" for k, v in top_ling]) or "—"

        dash_url = f"https://appartamento-bot.vercel.app/dashboard?key={DASHBOARD_KEY}" if DASHBOARD_KEY else "(dashboard non configurata)"

        report = (
            f"📊 *REPORT {mese_nome.upper()} {anno_p}*\n\n"
            f"💬 *Totale messaggi lifetime*: {totale}\n\n"
            f"🌍 *Lingue (lifetime)*:\n{ling_str}\n\n"
            f"🏷️ *Top 5 argomenti (lifetime)*:\n{arg_str}\n\n"
            f"👥 *Clienti*:\n• Totali registrati: {n_clienti}\n• Nuovi questo mese: {nuovi_mese}\n• Attivi negli ultimi 7gg: {attivi_7gg}\n\n"
            f"📅 *Prenotazioni totali*: {n_pren}\n\n"
            f"🔗 [Apri dashboard completa]({dash_url})"
        )
        if OWNER_ID:
            invia_messaggio(int(OWNER_ID), report, parse_mode="Markdown")
        return True
    except Exception as e:
        try:
            log_errore("report_mensile", e)
        except Exception:
            pass
        return False


# ── Stats ─────────────────────────────────────────────────────────────────────
TOPIC_KEYWORDS = {
    "wifi":         ["wifi","password","internet","connessione","rete","wlan","réseau","mot de passe","contraseña"],
    "check-in":     ["check-in","checkin","arrivo","arrivée","arrival","llegada","ankunft","chiavi","clé","key","keybox","codice"],
    "check-out":    ["check-out","checkout","partenza","départ","departure","salida","abreise","orario uscita"],
    "parcheggio":   ["parcheggio","garage","box","parking","voiture","auto","macchina","car","coche","wagen"],
    "spiaggia":     ["spiaggia","mare","beach","plage","playa","strand","oceano","bagno"],
    "supermercato": ["supermercato","spesa","negozio","supermarché","supermarket","supermercado","supermarkt","alimentari"],
    "ristorante":   ["ristorante","mangiare","cena","pranzo","restaurant","dinner","lunch","restaurante"],
    "lavatrice":    ["lavatrice","bucato","washing","machine à laver","lavadora","waschmaschine"],
    "aria condizionata": ["aria","condizionata","climatisation","air conditioning","aire acondicionado","klimaanlage"],
    "emergenza":    ["emergenza","problema","aiuto","urgente","emergency","urgence","emergencia","notfall"],
    "trasporti":    ["bus","treno","taxi","transfer","trasporto","transport","train","tren","zug","nizza","nice","cannes","antibes"],
}

def rileva_topic(domanda):
    t = domanda.lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(k in t for k in keywords):
            return topic
    return "altro"

def carica_stats():
    try:
        url = f"{STATS_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=5)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n","")).decode("utf-8")
        return json.loads(contenuto), data["sha"]
    except Exception:
        return {"totale": 0, "lingue": {}, "argomenti": {}}, None

def aggiorna_stats(domanda, lingua):
    if not GITHUB_TOKEN:
        return
    try:
        stats, sha = carica_stats()
        stats["totale"] = stats.get("totale", 0) + 1
        stats["lingue"][lingua] = stats["lingue"].get(lingua, 0) + 1
        topic = rileva_topic(domanda)
        stats["argomenti"][topic] = stats["argomenti"].get(topic, 0) + 1
        contenuto_nuovo = json.dumps(stats, ensure_ascii=False, indent=2)
        payload = {"message": "Bot aggiorna stats", "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8")}
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(STATS_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass

def carica_daily_stats():
    try:
        url = f"{DAILY_STATS_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=5)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        return json.loads(contenuto), data["sha"]
    except Exception:
        return {"data": "", "totale": 0, "lingue": {}, "argomenti": {}, "ospiti": []}, None

def aggiorna_daily_stats(domanda, lingua, chat_id):
    if not GITHUB_TOKEN:
        return
    try:
        oggi = datetime.now().strftime("%d/%m/%Y")
        stats, sha = carica_daily_stats()
        # Reset se è un nuovo giorno
        if stats.get("data") != oggi:
            stats = {"data": oggi, "totale": 0, "lingue": {}, "argomenti": {}, "ospiti": []}
            sha = None  # forza ricreazione file
        stats["totale"] = stats.get("totale", 0) + 1
        stats["lingue"][lingua] = stats["lingue"].get(lingua, 0) + 1
        topic = rileva_topic(domanda)
        stats["argomenti"][topic] = stats["argomenti"].get(topic, 0) + 1
        # Traccia ospiti unici
        ospiti = stats.get("ospiti", [])
        if str(chat_id) not in ospiti:
            ospiti.append(str(chat_id))
        stats["ospiti"] = ospiti
        contenuto_nuovo = json.dumps(stats, ensure_ascii=False, indent=2)
        payload = {
            "message": f"Daily stats {oggi}",
            "content": base64.b64encode(contenuto_nuovo.encode("utf-8")).decode("utf-8"),
        }
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(DAILY_STATS_API, data=json.dumps(payload).encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        }, method="PUT")
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass

def formatta_daily_stats():
    stats, _ = carica_daily_stats()
    oggi = datetime.now().strftime("%d/%m/%Y")
    data_stats = stats.get("data", "")
    totale = stats.get("totale", 0)

    if data_stats != oggi or totale == 0:
        return "📊 *Riepilogo di oggi*\n\nNessun messaggio ricevuto oggi. 😴"

    lingue = stats.get("lingue", {})
    bandiere = {"italian":"🇮🇹","french":"🇫🇷","english":"🇬🇧","spanish":"🇪🇸","german":"🇩🇪"}
    righe_lingue = " · ".join(
        f"{bandiere.get(l,'🌍')} {n}"
        for l, n in sorted(lingue.items(), key=lambda x: -x[1])
    )
    argomenti = stats.get("argomenti", {})
    top_arg = sorted(argomenti.items(), key=lambda x: -x[1])[:5]
    righe_arg = "\n".join(f"  • {a.capitalize()}: {n}" for a, n in top_arg)
    ospiti_unici = len(stats.get("ospiti", []))

    return (
        f"📊 *Riepilogo di oggi — {oggi}*\n\n"
        f"💬 Messaggi ricevuti: *{totale}*\n"
        f"👥 Ospiti attivi: *{ospiti_unici}*\n\n"
        f"🌍 Lingue: {righe_lingue}\n\n"
        f"🔥 *Argomenti del giorno:*\n{righe_arg}"
    )

def formatta_stats():
    stats, _ = carica_stats()
    totale = stats.get("totale", 0)
    if totale == 0:
        return "📊 Nessuna statistica disponibile ancora."
    lingue = stats.get("lingue", {})
    bandiere = {"italian":"🇮🇹","french":"🇫🇷","english":"🇬🇧","spanish":"🇪🇸","german":"🇩🇪","portuguese":"🇵🇹","dutch":"🇳🇱"}
    righe_lingue = "\n".join(
        f"  {bandiere.get(l,'🌍')} {l.capitalize()}: {n} ({round(n/totale*100)}%)"
        for l, n in sorted(lingue.items(), key=lambda x: -x[1])
    )
    argomenti = stats.get("argomenti", {})
    righe_arg = "\n".join(
        f"  {i+1}. {a.capitalize()}: {n}"
        for i, (a, n) in enumerate(sorted(argomenti.items(), key=lambda x: -x[1])[:8])
    )
    return (
        f"📊 *Statistiche bot*\n\n"
        f"💬 Domande totali: *{totale}*\n\n"
        f"🌍 *Lingue ospiti:*\n{righe_lingue}\n\n"
        f"🔥 *Argomenti più richiesti:*\n{righe_arg}"
    )


# ── API Telegram ──────────────────────────────────────────────────────────────
def telegram(metodo, payload):
    url = f"https://api.telegram.org/bot{TOKEN}/{metodo}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        r = urlopen_retry(req, timeout=10, retries=2)
        return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # Telegram 400 spesso = Markdown rotto. Ritenta senza parse_mode come fallback.
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if e.code == 400 and payload.get("parse_mode") and "can't parse entities" in body.lower():
            try:
                payload2 = {k: v for k, v in payload.items() if k != "parse_mode"}
                req2 = urllib.request.Request(url, data=json.dumps(payload2).encode(),
                                              headers={"Content-Type": "application/json"})
                r2 = urlopen_retry(req2, timeout=10, retries=1)
                return json.loads(r2.read())
            except Exception:
                pass
        raise

def invia_messaggio(chat_id, testo, parse_mode=None, remove_kb=True):
    payload = {"chat_id": chat_id, "text": testo}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if remove_kb:
        payload["reply_markup"] = {"remove_keyboard": True}
    telegram("sendMessage", payload)

def invia_bottoni(chat_id, testo, bottoni, parse_mode=None):
    payload = {
        "chat_id": chat_id,
        "text": testo,
        "reply_markup": {"inline_keyboard": bottoni}
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    telegram("sendMessage", payload)


def notifica_owner_aggregata(chat_id_ospite, testo, bottoni, parse_mode="Markdown"):
    """Manda una notifica all'OWNER ma se ce n'è una recente (<NOTIF_AGGREGA_SEC) per
    lo stesso ospite, modifica quella appendendo il nuovo Q&A. Riduce lo spam quando
    un ospite manda più messaggi di seguito. Best-effort, mai bloccante."""
    if not OWNER_ID:
        return
    try:
        now = datetime.now().timestamp()
        key = str(chat_id_ospite)
        rec = _ultima_notif_ospite.get(key)
        if rec and (now - rec.get("ts", 0)) < _NOTIF_AGGREGA_SEC:
            # Modifica notifica precedente appendendo
            sep = "\n\n— — —\n\n"
            nuovo_testo = (rec.get("testo", "") + sep + testo)[-3900:]
            try:
                modifica_messaggio(int(OWNER_ID), rec["msg_id"], nuovo_testo,
                                   parse_mode=parse_mode, bottoni=bottoni)
                _ultima_notif_ospite[key] = {"msg_id": rec["msg_id"], "testo": nuovo_testo, "ts": now}
                return
            except Exception:
                pass  # fallback a notifica nuova
        # Notifica nuova
        payload = {
            "chat_id": int(OWNER_ID),
            "text": testo,
            "reply_markup": {"inline_keyboard": bottoni} if bottoni else None,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = telegram("sendMessage", payload)
        msg_id = (r or {}).get("result", {}).get("message_id")
        if msg_id:
            _ultima_notif_ospite[key] = {"msg_id": msg_id, "testo": testo, "ts": now}
    except Exception:
        pass

def modifica_messaggio(chat_id, message_id, testo, parse_mode=None, bottoni=None):
    try:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": testo
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if bottoni is not None:
            payload["reply_markup"] = {"inline_keyboard": bottoni}
        telegram("editMessageText", payload)
    except Exception:
        pass

def invia_foto(chat_id, file_id, caption=""):
    telegram("sendPhoto", {"chat_id": chat_id, "photo": file_id, "caption": caption})

def invia_video(chat_id, file_id, caption=""):
    telegram("sendVideo", {"chat_id": chat_id, "video": file_id, "caption": caption})



# ── AI ────────────────────────────────────────────────────────────────────────
def rileva_lingua(testo):
    t = " " + testo.lower() + " "
    punteggi = {"french": 0, "english": 0, "spanish": 0, "german": 0}
    parole_fr = ["bonjour","bonsoir","merci","comment","quelle","quel","est-ce","il y a",
                 "puis-je","y a-t-il","c'est","avez","pouvez","voulez","heure","arrivée",
                 "départ","clé","clés","plage","boite","code","wifi","linge","machine",
                 "lave","laver","où","voiture","parking","piscine","serviette","draps",
                 "cuisine","salle","chambre","fenêtre","porte","balcon","ascenseur",
                 "poubelle","horaire","horaires","quelle heure","à quelle"]
    parole_en = ["hello","hi ","good morning","good evening","thanks","thank you","please",
                 "where is","what is","how do","is there","are there","can i","do you",
                 "there is","there are","washing","wifi","check-in","check in","check out",
                 "checkout","password","address","parking","beach","pool","towel","sheets",
                 "kitchen","room","bathroom","window","door","balcony","elevator","lift",
                 "garbage","trash","schedule","what time","how many","how much","any "]
    parole_es = ["hola","buenos","gracias","dónde","cómo","cuál","hay una","hay un",
                 "puede","puedo","tiene","tengo","llegada","salida","lavadora","wifi",
                 "playa","piscina","toalla","habitación","cocina","baño","parking",
                 "cuánto","cuándo","a qué hora"]
    parole_de = ["hallo","guten","danke","bitte","gibt es","wie ","wann ","wo ist",
                 "können","haben","waschmaschine","wifi","strand","pool","handtuch",
                 "zimmer","küche","bad ","fenster","parkplatz","wie viel","wieviel",
                 "um wie viel"]
    for w in parole_fr:
        if w in t: punteggi["french"] += 1
    for w in parole_en:
        if w in t: punteggi["english"] += 1
    for w in parole_es:
        if w in t: punteggi["spanish"] += 1
    for w in parole_de:
        if w in t: punteggi["german"] += 1
    migliore = max(punteggi, key=punteggi.get)
    return migliore if punteggi[migliore] > 0 else "italian"

# ── Meteo (OpenMeteo, gratis senza API key) ────────────────────────────────────
# Coordinate Juan-les-Pins (per multi-tenant in futuro: ogni tenant ha le sue)
METEO_LAT = 43.5640
METEO_LON = 7.1241
_meteo_cache = {"data": None, "ts": 0}
METEO_CACHE_TTL = 1800  # 30 minuti

WEATHER_CODES = {
    0: ("☀️", "Sereno"),
    1: ("🌤️", "Prevalentemente sereno"),
    2: ("⛅", "Parzialmente nuvoloso"),
    3: ("☁️", "Nuvoloso"),
    45: ("🌫️", "Nebbia"),
    48: ("🌫️", "Nebbia gelata"),
    51: ("🌦️", "Pioggerella leggera"),
    53: ("🌦️", "Pioggerella moderata"),
    55: ("🌧️", "Pioggerella intensa"),
    61: ("🌧️", "Pioggia leggera"),
    63: ("🌧️", "Pioggia moderata"),
    65: ("🌧️", "Pioggia intensa"),
    71: ("🌨️", "Neve leggera"),
    73: ("🌨️", "Neve moderata"),
    75: ("❄️", "Neve intensa"),
    80: ("🌧️", "Rovesci leggeri"),
    81: ("🌧️", "Rovesci moderati"),
    82: ("⛈️", "Rovesci violenti"),
    95: ("⛈️", "Temporale"),
    96: ("⛈️", "Temporale con grandine leggera"),
    99: ("⛈️", "Temporale con grandine forte"),
}

KEYWORDS_METEO = {
    "italian":  ["meteo", "tempo che fa", "che tempo", "pioggia", "piove", "sole", "soleggiato", "nuvoloso", "temperatura", "fa caldo", "fa freddo", "previsioni"],
    "english":  ["weather", "rain", "raining", "sunny", "cloudy", "temperature", "hot", "cold", "forecast", "will it rain"],
    "french":   ["météo", "temps", "pluie", "pleut", "soleil", "ensoleillé", "nuageux", "température", "prévisions"],
    "spanish":  ["tiempo", "lluvia", "llueve", "sol", "soleado", "nublado", "temperatura", "calor", "frío", "pronóstico"],
    "german":   ["wetter", "regen", "regnet", "sonne", "sonnig", "bewölkt", "temperatur", "heiß", "kalt", "vorhersage"],
}

def e_domanda_meteo(testo):
    """True se la domanda riguarda il meteo (qualunque lingua)."""
    if not testo:
        return False
    t = " " + testo.lower() + " "
    for parole in KEYWORDS_METEO.values():
        for p in parole:
            if p in t:
                return True
    return False

def recupera_meteo():
    """Scarica previsioni 3 giorni da OpenMeteo. Cache 30 min in-memory."""
    ora = datetime.now().timestamp()
    if _meteo_cache["data"] and (ora - _meteo_cache["ts"]) < METEO_CACHE_TTL:
        return _meteo_cache["data"]
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={METEO_LAT}&longitude={METEO_LON}"
            f"&current_weather=true"
            f"&daily=temperature_2m_max,temperature_2m_min,weathercode,precipitation_sum,precipitation_probability_max"
            f"&timezone=Europe%2FParis&forecast_days=3"
        )
        r = urllib.request.urlopen(url, timeout=6)
        data = json.loads(r.read())
        daily = data.get("daily", {})
        current = data.get("current_weather", {})
        giorni_label = ["Oggi", "Domani", "Dopodomani"]
        righe = []
        # Attuale
        if current:
            code = current.get("weathercode", 0)
            emoji, _ = WEATHER_CODES.get(code, ("🌡️", "?"))
            t = current.get("temperature")
            righe.append(f"• Ora: {emoji} {t}°C")
        # Prossimi 3 giorni
        codes = daily.get("weathercode", [])
        max_t = daily.get("temperature_2m_max", [])
        min_t = daily.get("temperature_2m_min", [])
        prob = daily.get("precipitation_probability_max", [])
        for i, code in enumerate(codes[:3]):
            emoji, descr = WEATHER_CODES.get(code, ("🌡️", "?"))
            mx = max_t[i] if i < len(max_t) else "?"
            mn = min_t[i] if i < len(min_t) else "?"
            pr = prob[i] if i < len(prob) else 0
            righe.append(f"• {giorni_label[i]}: {emoji} {descr}, min {mn}°C / max {mx}°C, pioggia {pr}%")
        testo = "Meteo Juan-les-Pins (fonte: Open-Meteo, ufficiale):\n" + "\n".join(righe)
        _meteo_cache["data"] = testo
        _meteo_cache["ts"] = ora
        return testo
    except Exception as e:
        try:
            log_errore("meteo", e)
        except Exception:
            pass
        return None


SYSTEM_PROMPT = {
    "italian": (
        "Sei un assistente virtuale per un appartamento in affitto su Booking e Airbnb. "
        "Rispondi SOLO con le informazioni presenti nel testo qui sotto — non aggiungere nulla che non sia scritto. "
        "ATTENZIONE AI NUMERI: cita ogni numero ESATTAMENTE come appare nel testo. Non confondere mai numeri diversi tra loro (es. numero civico, numero appartamento, codice, piano sono cose diverse). "
        "Se la domanda riguarda un argomento specifico, rispondi SOLO su quell'argomento senza aggiungere altre informazioni non richieste. "
        "Se non hai l'informazione richiesta, di' che lo chiederai a Lorenzo e risponderai al più presto. "
        "NUMERO DI TELEFONO: il numero di Lorenzo è disponibile nelle informazioni qui sotto. Forniscilo SOLO E SOLTANTO se l'ospite lo richiede esplicitamente con frasi come 'voglio chiamare', 'qual è il numero', 'puoi darmi il telefono', 'come ti chiamo', 'vorrei parlare al telefono'. Per qualsiasi altro tipo di domanda NON menzionarlo mai. Quando lo dai, presentalo chiaramente: '📞 Puoi chiamare Lorenzo al [numero]'. "
        "Riferisciti sempre al proprietario come 'Lorenzo'. "
        "Sii cordiale e conciso. "
        "Aggiungi 1-2 emoji coerenti con l'argomento (es. 🚗 parcheggio, 🏖️ spiaggia, 🚆 treno, 📶 wifi, 🔑 check-in, 🛒 supermercato, 🍽️ ristorante). "
        "INDIRIZZI E MAPS: ogni volta che citi un indirizzo specifico (appartamento, ristorante, supermercato, spiaggia, ecc.), aggiungi sempre subito dopo un link Google Maps cliccabile in questo formato esatto: 📍 https://maps.google.com/?q=INDIRIZZO+COMPLETO+URL+ENCODED (spazi sostituiti con +, virgole rimosse). Esempio: per '93 Bd Raymond Poincaré, 06160 Antibes' scrivi 📍 https://maps.google.com/?q=93+Bd+Raymond+Poincare+06160+Antibes\n\nINFORMAZIONI APPARTAMENTO:\n{info}"
    ),
    "french": (
        "Tu es un assistant virtuel pour un appartement de location sur Booking et Airbnb. "
        "Réponds UNIQUEMENT avec les informations du texte ci-dessous — n'ajoute rien qui n'y soit pas écrit. "
        "ATTENTION AUX CHIFFRES: cite chaque numéro EXACTEMENT comme il apparaît dans le texte. Ne confonds jamais des numéros différents (numéro de rue, numéro d'appartement, code, étage sont des choses distinctes). "
        "Si la question porte sur un sujet précis, réponds UNIQUEMENT sur ce sujet sans ajouter d'autres informations non demandées. "
        "Si tu n'as pas l'information, dis que tu vas demander à Lorenzo. "
        "NUMÉRO DE TÉLÉPHONE: le numéro de Lorenzo se trouve dans les informations ci-dessous. Donne-le UNIQUEMENT si le client le demande explicitement avec des phrases comme 'je veux appeler', 'quel est le numéro', 'peux-tu me donner le téléphone', 'comment je peux t'appeler'. Sinon, ne le mentionne JAMAIS. Quand tu le donnes, présente-le clairement: '📞 Tu peux appeler Lorenzo au [numéro]'. "
        "Réfère-toi toujours au propriétaire comme 'Lorenzo'. "
        "Sois cordial et concis. "
        "Ajoute 1-2 emojis cohérents avec le sujet (ex. 🚗 parking, 🏖️ plage, 🚆 train, 📶 wifi, 🔑 check-in). "
        "ADRESSES ET MAPS: chaque fois que tu cites une adresse spécifique (appartement, restaurant, supermarché, plage, etc.), ajoute toujours juste après un lien Google Maps cliquable dans ce format exact: 📍 https://maps.google.com/?q=ADRESSE+COMPLETE+URL+ENCODE (espaces remplacés par +, virgules supprimées).\n\nINFORMATIONS APPARTEMENT:\n{info}"
    ),
    "english": (
        "You are a virtual assistant for a vacation rental apartment on Booking and Airbnb. "
        "Answer ONLY using the information in the text below — do not add anything not written there. "
        "WARNING ABOUT NUMBERS: quote every number EXACTLY as it appears in the text. Never confuse different numbers (street number, apartment number, access code, floor are all different things). "
        "If the question is about a specific topic, answer ONLY about that topic without adding unrequested information. "
        "If you don't have the information, say you will ask Lorenzo. "
        "PHONE NUMBER: Lorenzo's phone number is available in the information below. Share it ONLY if the guest explicitly asks with phrases like 'I want to call', 'what's the phone number', 'can you give me the phone', 'how can I reach you'. Otherwise NEVER mention it. When you give it, present it clearly: '📞 You can call Lorenzo at [number]'. "
        "Always refer to the owner as 'Lorenzo'. "
        "Be friendly and concise. "
        "Add 1-2 relevant emojis (e.g. 🚗 parking, 🏖️ beach, 🚆 train, 📶 wifi, 🔑 check-in, 🛒 supermarket, 🍽️ restaurant). "
        "ADDRESSES AND MAPS: whenever you mention a specific address (apartment, restaurant, supermarket, beach, etc.), always add right after a clickable Google Maps link in this exact format: 📍 https://maps.google.com/?q=FULL+ADDRESS+URL+ENCODED (spaces replaced by +, commas removed).\n\nAPARTMENT INFORMATION:\n{info}"
    ),
    "spanish": (
        "Eres un asistente virtual para un apartamento de alquiler en Booking y Airbnb. "
        "Responde SOLO con la información del texto de abajo — no añadas nada que no esté escrito. "
        "ATENCIÓN A LOS NÚMEROS: cita cada número EXACTAMENTE como aparece en el texto. No confundas nunca números distintos (número de calle, número de apartamento, código, piso son cosas diferentes). "
        "Si la pregunta es sobre un tema específico, responde SOLO sobre ese tema. "
        "Si no tienes la información, di que se lo preguntarás a Lorenzo. "
        "TELÉFONO: el número de Lorenzo está en las informaciones de abajo. Compártelo SOLO si el huésped lo pide explícitamente con frases como 'quiero llamar', 'cuál es el número', 'puedes darme el teléfono', 'cómo te llamo'. En cualquier otro caso NUNCA lo menciones. Cuando lo des, preséntalo claramente: '📞 Puedes llamar a Lorenzo al [número]'. "
        "Llama siempre al propietario 'Lorenzo'. "
        "Sé cordial y conciso. "
        "Añade 1-2 emojis coherentes con el tema (ej. 🚗 aparcamiento, 🏖️ playa, 🚆 tren, 📶 wifi, 🔑 check-in). "
        "DIRECCIONES Y MAPS: cada vez que cites una dirección específica (apartamento, restaurante, supermercado, playa, etc.), añade siempre justo después un enlace Google Maps clicable en este formato exacto: 📍 https://maps.google.com/?q=DIRECCION+COMPLETA+URL+ENCODED (espacios reemplazados por +, comas eliminadas).\n\nINFORMACIÓN DEL APARTAMENTO:\n{info}"
    ),
    "german": (
        "Du bist ein virtueller Assistent für eine Ferienwohnung auf Booking und Airbnb. "
        "Antworte NUR mit den Informationen aus dem Text unten — füge nichts hinzu, was nicht dort steht. "
        "ACHTUNG BEI ZAHLEN: Zitiere jede Zahl GENAU so wie sie im Text erscheint. Verwechsle niemals verschiedene Zahlen (Hausnummer, Wohnungsnummer, Code, Etage sind verschiedene Dinge). "
        "Wenn die Frage ein bestimmtes Thema betrifft, antworte NUR zu diesem Thema. "
        "Wenn du die Information nicht hast, sage dass du Lorenzo fragen wirst. "
        "TELEFONNUMMER: Lorenzos Nummer steht in den Informationen unten. Gib sie NUR weiter, wenn der Gast sie ausdrücklich verlangt mit Sätzen wie 'ich möchte anrufen', 'wie ist die Nummer', 'kannst du mir das Telefon geben', 'wie erreiche ich dich'. Sonst erwähne sie NIEMALS. Wenn du sie gibst, präsentiere sie klar: '📞 Du kannst Lorenzo unter [Nummer] anrufen'. "
        "Nenne den Eigentümer immer 'Lorenzo'. "
        "Sei freundlich und prägnant. "
        "Füge 1-2 passende Emojis hinzu (z.B. 🚗 Parkplatz, 🏖️ Strand, 🚆 Zug, 📶 WLAN, 🔑 Check-in). "
        "ADRESSEN UND MAPS: jedes Mal wenn du eine konkrete Adresse erwähnst (Wohnung, Restaurant, Supermarkt, Strand usw.), füge immer direkt danach einen klickbaren Google Maps Link in genau diesem Format hinzu: 📍 https://maps.google.com/?q=VOLLSTAENDIGE+ADRESSE+URL+ENCODED (Leerzeichen durch + ersetzt, Kommas entfernt).\n\nWOHNUNGSINFORMATIONEN:\n{info}"
    ),
}

_LINGUA_LABEL = {
    "italian": "italiano", "english": "inglese", "french": "francese",
    "spanish": "spagnolo", "german": "tedesco", "portuguese": "portoghese",
}

def traduci_testo(testo, lingua_dest):
    """Traduce un testo in qualsiasi lingua target. Best-effort.
    Ritorna il testo tradotto, o l'originale in caso di errore.
    Il chiamante è responsabile di non invocarla se source == dest."""
    if not testo or not lingua_dest:
        return testo
    label = _LINGUA_LABEL.get(lingua_dest, lingua_dest)
    prompt = (
        f"Traduci il seguente messaggio in {label} mantenendo tono, emoji e formattazione.\n"
        f"Rispondi SOLO con la traduzione, senza prefissi, virgolette o spiegazioni.\n\n"
        f"Messaggio:\n{testo}"
    )
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_KEY}",
            "User-Agent": "groq-python/0.9.0"
        })
        r = urllib.request.urlopen(req, timeout=10)
        out = json.loads(r.read())["choices"][0]["message"]["content"].strip()
        # Rimuovi eventuali virgolette esterne
        if len(out) >= 2 and out[0] in "\"'«" and out[-1] in "\"'»":
            out = out[1:-1].strip()
        return out or testo
    except Exception:
        return testo


def _domanda_per_owner(voce_pre, testo, italic=False):
    """Formatta il testo di un ospite per la notifica all'owner.
    Se la lingua non è italiano, appende una riga con la traduzione IT."""
    body = f"_{testo}_" if italic else testo
    out = f"{voce_pre}{body}"
    try:
        lingua = rileva_lingua(testo)
        if lingua and lingua != "italian":
            it = traduci_testo(testo, "italian")
            if it and it.strip() and it != testo:
                out += f"\n🇮🇹 _IT:_ {it}"
    except Exception:
        pass
    return out


def _lingua_ospite(chat_id):
    """Recupera la lingua salvata di un ospite (da users.json). Fallback: italian."""
    try:
        _carica_users_da_github()
        u = _users.get(str(chat_id)) or {}
        return u.get("lingua") or "italian"
    except Exception:
        return "italian"


def traduci_keywords(keywords_it):
    """Traduce le parole chiave italiane in EN, FR, ES, DE e restituisce tutte le varianti."""
    prompt = (
        f"Traduci queste parole chiave italiane in inglese, francese, spagnolo e tedesco.\n"
        f"Parole chiave: {keywords_it}\n\n"
        f"Rispondi SOLO con una riga CSV con tutte le parole chiave (originali + traduzioni), "
        f"separate da virgola, senza spiegazioni, senza duplicati, tutto in minuscolo.\n"
        f"Esempio input: box, garage, parcheggio\n"
        f"Esempio output: box, garage, parcheggio, parking, parkplatz, stationnement, estacionamiento"
    )
    try:
        url = "https://api.groq.com/openai/v1/chat/completions"
        payload = {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}]
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_KEY}",
            "User-Agent": "groq-python/0.9.0"
        })
        r = urllib.request.urlopen(req, timeout=10)
        risultato = json.loads(r.read())["choices"][0]["message"]["content"].strip()
        # Pulisce e deduplicca
        tutte = [k.strip().lower() for k in risultato.split(",") if k.strip()]
        # Assicura che le originali ci siano sempre
        originali = [k.strip().lower() for k in keywords_it.split(",") if k.strip()]
        for o in originali:
            if o not in tutte:
                tutte.insert(0, o)
        return ", ".join(tutte)
    except Exception:
        return keywords_it  # fallback: usa solo le originali

def trascrivi_audio_groq(audio_data, mime_hint="audio/ogg"):
    """Trascrive audio binario via Groq Whisper. Restituisce testo trascritto o None."""
    if not GROQ_KEY:
        return None
    if not audio_data:
        return None
    try:
        # Determina estensione/filename in base al mime
        if "ogg" in mime_hint or "opus" in mime_hint:
            filename, mime = "audio.ogg", "audio/ogg"
        elif "mp3" in mime_hint or "mpeg" in mime_hint:
            filename, mime = "audio.mp3", "audio/mpeg"
        elif "m4a" in mime_hint or "mp4" in mime_hint or "aac" in mime_hint:
            filename, mime = "audio.m4a", "audio/m4a"
        elif "wav" in mime_hint:
            filename, mime = "audio.wav", "audio/wav"
        elif "webm" in mime_hint:
            filename, mime = "audio.webm", "audio/webm"
        else:
            filename, mime = "audio.ogg", "audio/ogg"
        # Multipart upload a Groq Whisper
        boundary = "----WhisperBoundary" + str(int(datetime.now().timestamp()))
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"whisper-large-v3-turbo\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            f"json\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + audio_data + f"\r\n--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "User-Agent": "appartamento-bot"
            }
        )
        r = urllib.request.urlopen(req, timeout=25)
        result = json.loads(r.read())
        return (result.get("text") or "").strip() or None
    except Exception as e:
        try:
            log_errore("trascrivi_audio", e)
        except Exception:
            pass
        return None


def scarica_telegram_voice(file_id):
    """Scarica un voice/audio da Telegram come bytes. Ritorna (data, mime) o (None, None)."""
    try:
        r = telegram("getFile", {"file_id": file_id})
        file_path = r.get("result", {}).get("file_path")
        if not file_path:
            return None, None
        url = f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"
        data = urllib.request.urlopen(url, timeout=15).read()
        # Telegram voice notes sono OGG Opus
        ext = file_path.lower().rsplit(".", 1)[-1] if "." in file_path else "ogg"
        mime = {"ogg":"audio/ogg","oga":"audio/ogg","mp3":"audio/mpeg","m4a":"audio/m4a","wav":"audio/wav","webm":"audio/webm"}.get(ext, "audio/ogg")
        return data, mime
    except Exception as e:
        try:
            log_errore("scarica_tg_voice", e)
        except Exception:
            pass
        return None, None


def scarica_wa_media(media_id):
    """Scarica un media da WhatsApp Cloud API come bytes. Ritorna (data, mime) o (None, None)."""
    try:
        # 1. Ottieni URL del media
        url = f"https://graph.facebook.com/v22.0/{media_id}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {WA_TOKEN}"})
        r = urllib.request.urlopen(req, timeout=10)
        info = json.loads(r.read())
        media_url = info.get("url")
        mime = info.get("mime_type", "audio/ogg")
        if not media_url:
            return None, None
        # 2. Scarica binario
        req2 = urllib.request.Request(media_url, headers={"Authorization": f"Bearer {WA_TOKEN}"})
        data = urllib.request.urlopen(req2, timeout=20).read()
        return data, mime
    except Exception as e:
        try:
            log_errore("scarica_wa_media", e)
        except Exception:
            pass
        return None, None


def _chiama_groq(model, messages, timeout):
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {"model": model, "messages": messages}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_KEY}",
        "User-Agent": "groq-python/0.9.0"
    })
    r = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(r.read())["choices"][0]["message"]["content"]

def _chiama_claude(system_text, messages_claude, timeout=35):
    url = "https://api.anthropic.com/v1/messages"
    # Prompt caching: il system prompt (16KB di info appartamento) viene cachato
    # da Anthropic per 5 minuti. Token "cached" costano 1/10 dei token normali.
    # Risparmio tipico: 60-80% sui costi totali.
    payload = {
        "model": "claude-haiku-4-5",
        "max_tokens": 1024,
        "system": [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"}
            }
        ],
        "messages": messages_claude
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01"
    })
    r = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(r.read())["content"][0]["text"]

def chiedi_ai(domanda, info, chat_id=None):
    lingua = rileva_lingua(domanda)
    # Se domanda meteo, inietta info meteo aggiornate dal servizio OpenMeteo
    if e_domanda_meteo(domanda):
        meteo_str = recupera_meteo()
        if meteo_str:
            info = f"{info}\n\n[INFORMAZIONI METEO AGGIORNATE — usa questi dati per rispondere a domande sul tempo]\n{meteo_str}"
    system_text = SYSTEM_PROMPT.get(lingua, SYSTEM_PROMPT["english"]).format(info=info[:24000])
    storia = get_storia(chat_id) if chat_id else []
    # Converti storia in formato Anthropic (no "system" nei messages)
    messages_claude = []
    for m in storia:
        messages_claude.append({"role": m["role"], "content": m["content"]})
    messages_claude.append({"role": "user", "content": domanda})
    # 1° tentativo Claude (35s)
    try:
        return _chiama_claude(system_text, messages_claude, timeout=35)
    except Exception as e1:
        try:
            log_errore("claude_1", e1)
        except Exception:
            pass
    # 2° tentativo Claude (retry, 35s)
    try:
        return _chiama_claude(system_text, messages_claude, timeout=35)
    except Exception as e2:
        try:
            log_errore("claude_2", e2)
        except Exception:
            pass
    # Fallback Groq con modello più potente (70b invece di 8b)
    try:
        messages_groq = [{"role": "system", "content": system_text}, *storia, {"role": "user", "content": domanda}]
        return _chiama_groq("llama-3.3-70b-versatile", messages_groq, timeout=20)
    except Exception as e3:
        try:
            log_errore("groq", e3)
        except Exception:
            pass
        return "Mi dispiace, sto avendo un problema tecnico in questo momento 🙏 Riprova tra qualche minuto, intanto avviso Lorenzo!"

def bot_non_sa(risposta):
    return any(f in risposta.lower() for f in FRASI_NON_SO)

SALUTI = ["ciao","salve","buongiorno","buonasera","hello","hi","hey","good morning",
          "good evening","good afternoon","bonjour","bonsoir","salut","hola","buenos días",
          "buenas","hallo","guten morgen","guten tag","guten abend","olá","oi"]

def e_saluto(testo):
    t = testo.lower().strip()
    return any(t == s or t.startswith(s + " ") or t.startswith(s + ",") for s in SALUTI)

BENVENUTO_IT = """Benvenuto! 😊 Sono l'assistente virtuale dell'appartamento, sono qui per aiutarti durante tutto il tuo soggiorno.

Ecco le informazioni per il tuo arrivo:

🕐 Check-in: dalle 16:00

🔑 KeyBox — codice: 8492
All'interno troverai il telecomando del garage.

🚗 Ingresso garage: 67 Chemin des Liserons, Antibes
Una volta entrati dal cancello elettrico, tieniti subito sulla sinistra e scendi la rampa aprendo anche il secondo cancello elettrico. Prosegui dritto fino in fondo: il posto auto è quello a sinistra a ridosso del muro, numero 53.

🚶 Una volta parcheggiato:
Sali al secondo piano. Uscendo dall'ascensore gira a sinistra — la prima porta a destra è l'appartamento 23.
Inserisci il codice sul tastierino per entrare.
Troverai le chiavi dell'appartamento sul tavolo in sala: le userai durante tutto il tuo soggiorno.

Buon soggiorno in Costa Azzurra! 🌊☀️

Per qualsiasi domanda o necessità sono qui, non esitare a scrivermi."""

def genera_benvenuto(lingua, info):
    if lingua == "italian":
        return BENVENUTO_IT
    nomi_lingua = {
        "french": "French", "english": "English",
        "spanish": "Spanish", "german": "German",
        "portuguese": "Portuguese", "dutch": "Dutch",
    }
    target = nomi_lingua.get(lingua, "English")
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": f"Translate the following message to {target}. Return ONLY the translation, keep the emojis, preserve the exact structure and all details."},
            {"role": "user",   "content": BENVENUTO_IT}
        ]
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GROQ_KEY}",
        "User-Agent": "groq-python/0.9.0"
    })
    r = urllib.request.urlopen(req, timeout=25)
    return json.loads(r.read())["choices"][0]["message"]["content"]


# ── Webhook ───────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        body = request.get_json(force=True)

        # ── Pulsanti (callback_query) ───────────────────────────────────────
        callback = body.get("callback_query")
        if callback:
            cb_id      = callback["id"]
            cb_data    = callback.get("data", "")
            cb_chat_id = callback["message"]["chat"]["id"]
            cb_msg_id  = callback["message"]["message_id"]
            cb_testo   = callback["message"].get("text", "")

            telegram("answerCallbackQuery", {"callback_query_id": cb_id})

            # ── Salva media ──
            if cb_data == "SALVA_MEDIA":
                # Idempotenza
                if "Salvataggio in corso" in cb_testo or "Media salvato!" in cb_testo:
                    return "ok"
                m_fid  = re.search(r'FILE_ID: (.+)', cb_testo)
                m_tipo = re.search(r'TIPO: (.+)', cb_testo)
                m_kw   = re.search(r'PAROLE_CHIAVE: (.+)', cb_testo)
                m_desc = re.search(r'DESCRIZIONE: (.+)', cb_testo)
                if m_fid and m_tipo and m_kw and m_desc:
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"💾 Salvataggio in corso...\n\nParole chiave: {m_kw.group(1).strip()}"
                    )
                    salvato = salva_media_su_github(
                        m_kw.group(1).strip(), m_tipo.group(1).strip(),
                        m_fid.group(1).strip(), m_desc.group(1).strip()
                    )
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"✅ Media salvato!\n\nParole chiave: {m_kw.group(1).strip()}\nDa ora rispondo automaticamente con questa foto/video."
                        if salvato else "❌ Errore nel salvataggio.")

            # ── Salva Q&A o info ──
            elif cb_data == "SALVA":
                # Idempotenza: se l'utente clicca più volte, ignora click successivi
                if "Salvataggio in corso" in cb_testo or "Salvato!" in cb_testo or "Info aggiunta" in cb_testo:
                    return "ok"
                match_dq = re.search(r'D: (.+?)\nR: (.+)', cb_testo, re.DOTALL)
                match_r  = re.search(r'R: (.+)', cb_testo, re.DOTALL)
                if match_dq:
                    domanda  = match_dq.group(1).strip()
                    risposta = match_dq.group(2).strip()
                    # Feedback immediato (rimuove i bottoni cosi non si puo riclickare)
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"💾 Salvataggio in corso...\n\nD: {domanda}\nR: {risposta}"
                    )
                    salvato  = salva_su_github(f"{domanda}: {risposta}", "")
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"🧠 Salvato!\n\nD: {domanda}\nR: {risposta}\n\nLa prossima volta rispondo in autonomia."
                        if salvato else "❌ Errore nel salvataggio.")
                elif match_r:
                    risposta = match_r.group(1).strip()
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"💾 Salvataggio in corso...\n\n{risposta[:200]}{'...' if len(risposta) > 200 else ''}"
                    )
                    salvato = salva_su_github(risposta, "")
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"✅ Info aggiunta:\n\n{risposta}"
                        if salvato else "❌ Errore nel salvataggio.")

            # ── Modifica date prenotazione ──
            elif cb_data.startswith("MODIFICA_DATE:"):
                guest_chat_id = cb_data.split(":")[1]
                # Recupera info prenotazione dal testo del messaggio
                m_nome = re.search(r'Ospite: (.+?) \[', cb_testo)
                nome_ospite = m_nome.group(1) if m_nome else "ospite"
                # Mette Lorenzo in stato attesa-correzione
                _attesa_correzione_owner[str(cb_chat_id)] = guest_chat_id
                modifica_messaggio(cb_chat_id, cb_msg_id,
                    f"✏️ Inviami le date corrette per {nome_ospite} nel formato:\n\n"
                    f"25/04/2026 - 28/04/2026\n\noppure\n\n25 aprile - 28 aprile"
                )

            # ── Conferma date ok ──
            elif cb_data == "DATE_OK":
                modifica_messaggio(cb_chat_id, cb_msg_id,
                    cb_testo.replace("  ✏️ Modifica date      ✅ Ok", "\n\n✅ Date confermate!")
                )

            elif cb_data == "RICOMINCIA_MEDIA":
                # Recupera file_id e tipo dal testo del messaggio
                m_fid  = re.search(r'FILE_ID: (.+)', cb_testo)
                m_tipo = re.search(r'TIPO: (.+)', cb_testo)
                if m_fid and m_tipo:
                    _upload_media[str(cb_chat_id)] = {
                        "file_id": m_fid.group(1).strip(),
                        "tipo": m_tipo.group(1).strip(),
                        "step": "keywords"
                    }
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        "🔄 Ok, ricominciamo!\n\n"
                        "1️⃣ Scrivi le *parole chiave* che attiveranno questo media.\n"
                        "Separale con virgola in tutte le lingue.\n\n"
                        "Esempio:\n`box, garage, parcheggio, parking`"
                    )

            elif cb_data == "NO":
                _upload_media.pop(str(cb_chat_id), None)
                modifica_messaggio(cb_chat_id, cb_msg_id, "✅ Ok, non salvato.")

            # ── Cancellazione media: bottone 🗑️ N → conferma ──
            elif cb_data.startswith("DEL_MEDIA:"):
                indice = cb_data.split(":")[1]
                # Trova il media #indice per mostrare anteprima
                anteprima = ""
                try:
                    media_list = leggi_media()
                    idx = int(indice) - 1
                    if 0 <= idx < len(media_list):
                        m = media_list[idx]
                        icona = "🎬" if m["tipo"] == "video" else "📸"
                        kw = ", ".join(m["keywords"][:5])
                        anteprima = f"\n\n{icona} _{kw}_\n📝 {m.get('caption','')[:100]}"
                except Exception:
                    pass
                invia_bottoni(cb_chat_id,
                    f"⚠️ Sicuro di voler cancellare il media *#{indice}*?{anteprima}",
                    [[
                        {"text": f"✅ Sì, cancella #{indice}", "callback_data": f"DEL_MEDIA_OK:{indice}"},
                        {"text": "❌ Annulla", "callback_data": "DEL_MEDIA_CANCEL"}
                    ]],
                    parse_mode="Markdown"
                )

            # ── Cancellazione media: conferma → esegui ──
            elif cb_data.startswith("DEL_MEDIA_OK:"):
                indice = int(cb_data.split(":")[1])
                modifica_messaggio(cb_chat_id, cb_msg_id, f"⏳ Cancellazione media #{indice}...")
                riga_rimossa = cancella_media_su_github(indice)
                if riga_rimossa:
                    anteprima = riga_rimossa[:120] + ("..." if len(riga_rimossa) > 120 else "")
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"✅ Media *#{indice}* cancellato!\n\n`{anteprima}`\n\n"
                        f"Usa /listamedia per vedere la lista aggiornata.",
                        parse_mode="Markdown"
                    )
                else:
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"❌ Errore nella cancellazione del media #{indice}."
                    )

            # ── Cancellazione media: annulla ──
            elif cb_data == "DEL_MEDIA_CANCEL":
                modifica_messaggio(cb_chat_id, cb_msg_id, "✅ Annullato. Nessun media è stato cancellato.")

            # ── Prenotazione: scelta lingua ──
            elif cb_data.startswith("PREN_LANG:"):
                lingua_scelta = cb_data.split(":", 1)[1]
                stato = wizard_pren_get(cb_chat_id)
                if stato:
                    stato["lingua"] = lingua_scelta
                    stato["step"] = "ospiti"
                    wizard_pren_set(cb_chat_id, stato)
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        cb_testo + f"\n\n✅ Lingua: *{lingua_scelta}*", parse_mode="Markdown", bottoni=[])
                    invia_messaggio(cb_chat_id,
                        "📅 *Passo 4/6* — Quanti ospiti?\n\n"
                        "Scrivi solo il numero (es: `3`)",
                        parse_mode="Markdown"
                    )
                else:
                    modifica_messaggio(cb_chat_id, cb_msg_id, "⚠️ Sessione prenotazione scaduta. Usa /prenotazione per ricominciare.")

            # ── Calendario canale: culla sì/no per stub appena completato ──
            elif cb_data.startswith("CAL_CULLA:"):
                # formato: CAL_CULLA:<event_key>:yes|no
                _, ev_key, yn = cb_data.split(":", 2)
                culla = (yn == "yes")
                cal_set_culla(ev_key, culla)
                modifica_messaggio(cb_chat_id, cb_msg_id,
                    cb_testo + f"\n\n✅ Culla: *{'SÌ 🛏️' if culla else 'no'}*",
                    parse_mode="Markdown", bottoni=[])
                # Trigger pulizie partendo da questo evento
                try:
                    eventi, _ = _cal_load_events()
                    ev = eventi.get(ev_key) or {}
                    pulizie_trigger_da_prenotazione(
                        ev.get("nome") or "",
                        ev.get("checkin") or "",
                        ev.get("checkout") or "",
                        int(ev.get("num_ospiti") or 0),
                        culla,
                    )
                except Exception:
                    pass

            # ── Prenotazione: culla sì/no ──
            elif cb_data.startswith("PREN_CULLA:"):
                culla_scelta = cb_data.split(":", 1)[1] == "yes"
                stato = wizard_pren_get(cb_chat_id)
                if stato:
                    stato["culla"] = culla_scelta
                    stato["step"] = "contatto"
                    wizard_pren_set(cb_chat_id, stato)
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        cb_testo + f"\n\n✅ Culla: *{'SÌ 🛏️' if culla_scelta else 'no'}*",
                        parse_mode="Markdown", bottoni=[])
                    invia_messaggio(cb_chat_id,
                        "📅 *Passo 6/6* — Come contatti il cliente?\n\n"
                        "Scrivi una di queste opzioni:\n"
                        "• `wa 393201234567` (numero WhatsApp con prefisso paese, senza +)\n"
                        "• `tg 8668813727` (chat ID Telegram dell'ospite)\n"
                        "• `no` (salta — niente promemoria automatici)",
                        parse_mode="Markdown"
                    )
                else:
                    modifica_messaggio(cb_chat_id, cb_msg_id, "⚠️ Sessione prenotazione scaduta. Usa /prenotazione per ricominciare.")

            # ── Pausa AI da bottone notifica ──
            elif cb_data.startswith("PAUSA:"):
                target = cb_data.split(":", 1)[1]
                ok = set_pause(target, True)
                if ok:
                    suffisso = f"\n\n⏸️ *Pausa AI attivata* per `{target}`"
                    nuovi_bottoni = [[{"text": "▶️ Riattiva AI", "callback_data": f"RIPRENDI:{target}"}]]
                    modifica_messaggio(cb_chat_id, cb_msg_id, cb_testo + suffisso, parse_mode="Markdown", bottoni=nuovi_bottoni)
                else:
                    modifica_messaggio(cb_chat_id, cb_msg_id, cb_testo + "\n\n❌ Errore", parse_mode="Markdown")

            # ── Takeover: Lorenzo prende la chat ──
            elif cb_data.startswith("TAKEOVER:"):
                target = cb_data.split(":", 1)[1]
                ok = set_takeover(target, True)
                if ok:
                    # Avvisa l'ospite nella sua lingua (best effort: usa lingua salvata in users.json)
                    try:
                        u = _users.get(str(target), {})
                        lingua = u.get("lingua", "italian")
                        msg = TAKEOVER_MSG.get(lingua, TAKEOVER_MSG["english"])
                        if str(target).startswith("wa_"):
                            wa_invia(str(target).replace("wa_", ""), msg)
                        else:
                            invia_messaggio(int(target), msg)
                    except Exception:
                        pass
                    suffisso = f"\n\n💬 *Hai preso tu la chat.* L'ospite è stato avvisato. I tuoi prossimi reply arriveranno senza prefisso '💬'.\n\n⏹️ Quando hai finito, premi il bottone qui sotto per ridare il controllo al bot."
                    nuovi_bottoni = [[{"text": "▶️ Riattiva AI (fine takeover)", "callback_data": f"RIPRENDI:{target}"}]]
                    modifica_messaggio(cb_chat_id, cb_msg_id, cb_testo + suffisso, parse_mode="Markdown", bottoni=nuovi_bottoni)
                else:
                    modifica_messaggio(cb_chat_id, cb_msg_id, cb_testo + "\n\n❌ Errore", parse_mode="Markdown")

            # ── Riattiva AI (esce sia da pausa che da takeover) ──
            elif cb_data.startswith("RIPRENDI:"):
                target = cb_data.split(":", 1)[1]
                set_pause(target, False)
                set_takeover(target, False)
                suffisso = f"\n\n▶️ *AI riattivata* per `{target}`. Il bot risponderà di nuovo automaticamente."
                # Rimuove i bottoni (nessuno più necessario)
                modifica_messaggio(cb_chat_id, cb_msg_id, cb_testo + suffisso, parse_mode="Markdown", bottoni=[])

            elif cb_data.startswith("REPLY:"):
                # Bottone "Rispondi qui": apre una compose con force_reply.
                # target_tag esempi: "ID:12345" o "WA:393201234567"
                target_tag = cb_data.split(":", 1)[1]
                # Estrai nome ospite dal testo originale per personalizzare il prompt
                m_nome = re.search(r'Ospite:\s*([^\n\[]+)', cb_testo) or re.search(r'\*([^*\n]+)\*', cb_testo)
                nome_ospite = (m_nome.group(1).strip() if m_nome else "ospite").strip()[:40]
                telegram("sendMessage", {
                    "chat_id": cb_chat_id,
                    "text": f"✏️ Scrivi qui la risposta per {nome_ospite} [{target_tag}]",
                    "reply_markup": {"force_reply": True, "input_field_placeholder": f"Risposta per {nome_ospite}..."}
                })

            return "ok"

        # ── Messaggi normali ────────────────────────────────────────────────
        message  = body.get("message", {})
        chat_id  = message.get("chat", {}).get("id")
        testo    = message.get("text", "")
        nome     = message.get("from", {}).get("first_name", "Ospite")
        username = message.get("from", {}).get("username", "")
        is_owner = str(chat_id) == OWNER_ID

        if not chat_id:
            return "ok"

        # ── Audio/voice in arrivo → trascrivi e tratta come testo ──
        era_vocale = False
        voice_obj = message.get("voice") or message.get("audio")
        if voice_obj and not testo:
            file_id_audio = voice_obj.get("file_id")
            mime_hint = voice_obj.get("mime_type", "audio/ogg")
            if file_id_audio:
                # Feedback istantaneo all'utente
                if not is_owner:
                    invia_messaggio(chat_id, "🎙️ Sto ascoltando il messaggio vocale...")
                audio_data, mime = scarica_telegram_voice(file_id_audio)
                trascritto = trascrivi_audio_groq(audio_data, mime or mime_hint) if audio_data else None
                if trascritto:
                    testo = trascritto  # tratta come testo normale
                    era_vocale = True
                else:
                    invia_messaggio(chat_id, "Mi dispiace, non sono riuscito a capire l'audio 🙏 Puoi scrivermi a testo?")
                    return "ok"

        # ── Proprietario invia foto/video → caption=upload one-shot, senza caption=wizard ──
        if is_owner and not testo:
            try:
                foto  = message.get("photo")
                video = message.get("video")
                doc   = message.get("document")
                if foto:
                    file_id, tipo = foto[-1]["file_id"], "photo"
                elif video:
                    file_id, tipo = video["file_id"], "video"
                elif doc:
                    file_id, tipo = doc["file_id"], "photo"
                else:
                    return "ok"

                # ── 1-step: caption presente con formato "keywords | descrizione" ──
                caption_in = (message.get("caption") or "").strip()
                if caption_in:
                    if "|" in caption_in:
                        kw_it, descrizione = [p.strip() for p in caption_in.split("|", 1)]
                    else:
                        kw_it, descrizione = caption_in, ""
                    if not kw_it:
                        invia_messaggio(chat_id,
                            "❌ Caption vuota. Riprova col formato:\n`spiaggia, mare | Yolo Plage a 5 min`",
                            parse_mode="Markdown")
                        return "ok"
                    invia_messaggio(chat_id, "🌍 Traduco le parole chiave...")
                    keywords_complete = traduci_keywords(kw_it) or kw_it
                    salvato = salva_media_su_github(keywords_complete, tipo, file_id, descrizione)
                    if salvato:
                        invia_messaggio(chat_id,
                            f"✅ *Media salvato!*\n\n"
                            f"🔑 Parole chiave: `{keywords_complete}`\n"
                            f"💬 Descrizione: {descrizione or '_(nessuna)_'}\n\n"
                            f"Da ora rispondo automaticamente con questo media.",
                            parse_mode="Markdown")
                    else:
                        invia_messaggio(chat_id, "❌ Errore nel salvataggio. Riprova.")
                    return "ok"

                # ── Wizard 3-step: nessuna caption, comportamento classico ──
                _upload_media[str(chat_id)] = {"file_id": file_id, "tipo": tipo, "step": "keywords"}
                invia_messaggio(chat_id,
                    f"📸 {'Foto' if tipo == 'photo' else 'Video'} ricevuto!\n\n"
                    f"💡 *Suggerimento:* la prossima volta puoi mandare la foto direttamente con caption:\n"
                    f"`spiaggia, mare | Yolo Plage a 5 min`\n\n"
                    f"Per ora procediamo passo passo.\n\n"
                    f"1️⃣ Scrivi le *parole chiave* che attiveranno questo media (in italiano, le altre lingue le aggiungo io).\n"
                    f"Separale con virgola.\n\n"
                    f"Esempio:\n`spiaggia, mare, lettini`",
                    parse_mode="Markdown"
                )
            except Exception as e:
                invia_messaggio(chat_id, f"Errore: {e}")
            return "ok"

        if not testo:
            return "ok"

        # ── /start o saluto ospite → benvenuto + chiedi date ──
        if testo == "/start" or (not is_owner and e_saluto(testo)):
            try:
                lingua    = rileva_lingua(testo) if testo != "/start" else "italian"
                info      = leggi_info()
                benvenuto = genera_benvenuto(lingua, info)
                # Invia benvenuto rimuovendo eventuale tastiera precedente
                telegram("sendMessage", {
                    "chat_id": chat_id,
                    "text": benvenuto,
                    "reply_markup": {"remove_keyboard": True}
                })
                # Chiedi le date solo se non le abbiamo già
                prenotazioni, _ = carica_prenotazioni()
                if str(chat_id) not in prenotazioni:
                    invia_messaggio(chat_id, DOMANDA_DATE.get(lingua, DOMANDA_DATE["english"]), remove_kb=True)
                    _attesa_date[chat_id] = {"nome": nome, "lingua": lingua}
            except Exception:
                invia_messaggio(chat_id, "Benvenuto! 😊 Sono l'assistente virtuale dell'appartamento. Come posso aiutarti?")
            return "ok"

        # ── Proprietario risponde a notifica → inoltra all'ospite ──
        if is_owner and message.get("reply_to_message"):
            testo_originale = message["reply_to_message"].get("text", "")
            # Regex tollerante: matcha sia [WA:393], WA:393, WA-393, 🆔WA:393 ecc.
            match_wa = re.search(r'WA[:\-]\s*(\d{8,})', testo_originale)
            match_id = re.search(r'\bID[:\-]\s*(\d{4,})', testo_originale)
            if match_wa:
                wa_numero = match_wa.group(1)
                wa_session_key = f"wa_{wa_numero}"
                # In takeover → niente prefix "💬" (sembra messaggio diretto)
                in_takeover = is_in_takeover(wa_session_key)
                prefix = "" if in_takeover else "💬 "
                # Auto-traduzione: se l'ospite ha scritto in altra lingua, traduci
                lingua_ospite = _lingua_ospite(wa_session_key)
                lingua_owner_msg = rileva_lingua(testo)
                testo_finale = testo
                tradotto = False
                if lingua_ospite and lingua_ospite != "italian" and lingua_owner_msg != lingua_ospite:
                    t = traduci_testo(testo, lingua_ospite)
                    if t and t != testo:
                        testo_finale = t
                        tradotto = True
                wa_invia(wa_numero, f"{prefix}{testo_finale}")
                # Aggiorna anche la storia conversazione lato bot
                try:
                    aggiorna_storia(wa_session_key, "[Risposta diretta di Lorenzo]", testo_finale)
                except Exception:
                    pass
                # Conferma a Lorenzo (mostra il testo tradotto se applicabile)
                nota_trad = ""
                if tradotto:
                    label = _LINGUA_LABEL.get(lingua_ospite, lingua_ospite)
                    nota_trad = f"\n\n🌍 _Tradotto in {label}:_\n`{testo_finale}`"
                if in_takeover:
                    invia_bottoni(chat_id,
                        f"✅ Risposta inviata su WhatsApp a +{wa_numero}{nota_trad}\n\n💬 _Sei in takeover._ Continua pure a rispondere oppure ridai il controllo al bot 👇",
                        [[{"text": "▶️ Riattiva AI", "callback_data": f"RIPRENDI:{wa_session_key}"}]],
                        parse_mode="Markdown"
                    )
                else:
                    invia_messaggio(chat_id, f"✅ Risposta inviata su WhatsApp a +{wa_numero}!{nota_trad}", parse_mode="Markdown")
                # Salva automaticamente in memoria (no bottone)
                _autosalva_qa(chat_id, testo_originale, testo)
                return "ok"
            if match_id:
                id_ospite = int(match_id.group(1))
                # In takeover → niente prefix "💬"
                in_takeover = is_in_takeover(id_ospite)
                prefix = "" if in_takeover else "💬 "
                # Auto-traduzione: se l'ospite ha scritto in altra lingua, traduci
                lingua_ospite = _lingua_ospite(id_ospite)
                lingua_owner_msg = rileva_lingua(testo)
                testo_finale = testo
                tradotto = False
                if lingua_ospite and lingua_ospite != "italian" and lingua_owner_msg != lingua_ospite:
                    t = traduci_testo(testo, lingua_ospite)
                    if t and t != testo:
                        testo_finale = t
                        tradotto = True
                invia_messaggio(id_ospite, f"{prefix}{testo_finale}")
                nota_trad = ""
                if tradotto:
                    label = _LINGUA_LABEL.get(lingua_ospite, lingua_ospite)
                    nota_trad = f"\n\n🌍 _Tradotto in {label}:_\n`{testo_finale}`"
                if in_takeover:
                    invia_bottoni(chat_id,
                        f"✅ Risposta inviata{nota_trad}\n\n💬 _Sei in takeover._ Continua pure a rispondere oppure ridai il controllo al bot 👇",
                        [[{"text": "▶️ Riattiva AI", "callback_data": f"RIPRENDI:{id_ospite}"}]],
                        parse_mode="Markdown"
                    )
                else:
                    invia_messaggio(chat_id, f"✅ Risposta inviata all'ospite!{nota_trad}", parse_mode="Markdown")
                # Salva automaticamente in memoria (no bottone)
                _autosalva_qa(chat_id, testo_originale, testo)
                return "ok"
            # Reply ma né WA né ID trovati nel messaggio originale → avvisa Lorenzo
            # (così non cade nella AI sull'owner)
            invia_messaggio(chat_id,
                "⚠️ Non riesco a identificare il destinatario di questa risposta.\n\n"
                "Il messaggio originale a cui hai risposto non contiene un tag `WA:numero` o `ID:numero`.\n\n"
                "Soluzioni:\n"
                "• Usa `/rispondi <chat_id> <testo>` per inoltrare manualmente\n"
                "• Oppure usa il bottone *💬 Prendi chat* sulla notifica fresca dell'ospite",
                parse_mode="Markdown"
            )
            return "ok"

        # ── Flusso guidato upload media ──
        if is_owner and str(chat_id) in _upload_media and not testo.startswith("/"):
            stato = _upload_media[str(chat_id)]
            if stato["step"] == "keywords":
                invia_messaggio(chat_id, "⏳ Sto traducendo le parole chiave in tutte le lingue...")
                keywords_complete = traduci_keywords(testo.strip())
                stato["keywords"] = keywords_complete
                stato["step"] = "description"
                invia_messaggio(chat_id,
                    f"✅ Parole chiave salvate in tutte le lingue:\n"
                    f"`{keywords_complete}`\n\n"
                    f"2️⃣ Ora scrivi la *descrizione* che l'ospite vedrà insieme alla foto/video.\n\n"
                    f"Esempio:\n`Ecco come raggiungere il box! 🚗`"
                )
                return "ok"
            elif stato["step"] == "description":
                descrizione = testo.strip()
                keywords    = stato["keywords"]
                file_id     = stato["file_id"]
                tipo        = stato["tipo"]
                del _upload_media[str(chat_id)]
                invia_bottoni(chat_id,
                    f"💾 Riepilogo — vuoi salvare?\n\n"
                    f"🔑 Parole chiave: {keywords}\n"
                    f"📝 Descrizione: {descrizione}\n"
                    f"📎 Tipo: {'Foto 📸' if tipo == 'photo' else 'Video 🎬'}\n\n"
                    f"FILE_ID: {file_id}\n"
                    f"TIPO: {tipo}\n"
                    f"PAROLE_CHIAVE: {keywords}\n"
                    f"DESCRIZIONE: {descrizione}",
                    [[
                        {"text": "✅ Sì, salva", "callback_data": "SALVA_MEDIA"},
                        {"text": "✏️ Ricomincia",  "callback_data": "RICOMINCIA_MEDIA"},
                        {"text": "❌ Annulla",      "callback_data": "NO"}
                    ]]
                )
                return "ok"

        # ── Proprietario sta correggendo date di un ospite ──
        if is_owner and str(chat_id) in _attesa_correzione_owner and not testo.startswith("/"):
            guest_chat_id = _attesa_correzione_owner.pop(str(chat_id))
            checkin, checkout = estrai_date(testo)
            if checkin and checkout:
                prenotazioni, _ = carica_prenotazioni()
                info_ospite = prenotazioni.get(str(guest_chat_id), {})
                nome_ospite = info_ospite.get("nome", "Ospite")
                lingua_ospite = info_ospite.get("lingua", "italian")
                salva_prenotazione(int(guest_chat_id), nome_ospite, checkin, checkout, lingua_ospite)
                invia_messaggio(chat_id,
                    f"✅ Date aggiornate per {nome_ospite}!\n📆 Check-in: {checkin}\n🏁 Check-out: {checkout}"
                )
            else:
                invia_messaggio(chat_id,
                    "❌ Non ho capito le date. Prova con il formato:\n25/04/2026 - 28/04/2026"
                )
            return "ok"

        # ── Channel manager: proprietario sta completando una prenotazione iCal ──
        # Precedenza: prima di interpretare il testo come "info da salvare in appartamento.txt".
        # I controlli `not wizard_pren_get(...)` e `_attesa_correzione_owner` garantiscono
        # che i wizard ospiti esistenti hanno la priorità.
        if (is_owner
                and not message.get("reply_to_message")
                and not testo.startswith("/")
                and not wizard_pren_get(chat_id)
                and str(chat_id) not in _upload_media
                and str(chat_id) not in _attesa_correzione_owner
                and cal_has_pending()):
            risposta_cal, key_completato = cal_complete_oldest_stub(testo)
            invia_messaggio(chat_id, risposta_cal, parse_mode="Markdown")
            if key_completato:
                invia_bottoni(chat_id,
                    "🛏️ Serve montare la *culla* per un neonato in questa prenotazione?",
                    [[
                        {"text": "🛏️ Sì, serve culla", "callback_data": f"CAL_CULLA:{key_completato}:yes"},
                        {"text": "❌ No",              "callback_data": f"CAL_CULLA:{key_completato}:no"},
                    ]],
                    parse_mode="Markdown"
                )
            return "ok"

        # ── Proprietario scrive info direttamente → chiede se salvare ──
        # Skippa se il proprietario è in mezzo a un wizard (prenotazione/upload/correzione date)
        if (is_owner
                and not message.get("reply_to_message")
                and not testo.startswith("/")
                and not wizard_pren_get(chat_id)
                and str(chat_id) not in _upload_media
                and str(chat_id) not in _attesa_correzione_owner):
            invia_bottoni(chat_id,
                f"💾 Vuoi aggiungere questa info ad appartamento.txt?\n\nR: {testo}",
                [[
                    {"text": "✅ Sì, aggiungi", "callback_data": "SALVA"},
                    {"text": "❌ No",            "callback_data": "NO"}
                ]]
            )
            return "ok"

        # ── /menu — Lista comandi owner ──
        if (testo == "/menu" or testo == "/help") and is_owner:
            invia_messaggio(chat_id,
                "🎛️ *Menu comandi*\n\n"
                "*Gestione ospiti*\n"
                "• `/pausa <id>` — disattiva AI per un cliente\n"
                "• `/riprendi <id>` — riattiva AI\n"
                "• `/rispondi <id> <testo>` — invia msg diretto a un ospite\n\n"
                "*Prenotazioni*\n"
                "• `/prenotazione` — nuova prenotazione (one-shot o wizard)\n"
                "• `/prenotazioni` — lista prenotazioni attive\n"
                "• `/annulla` — annulla wizard in corso\n\n"
                "*Calendario & sync*\n"
                "• `/cal` — stub iCal da completare\n"
                "• `/calendario` — link al feed iCal unificato\n"
                "• `/calsync` — sync manuale Airbnb+Booking\n\n"
                "*Pulizie*\n"
                "• `/pulizie` — gestione turni signora pulizie\n\n"
                "*Stats & dashboard*\n"
                "• `/stats` — statistiche oggi\n"
                "• `/dashboard` — link dashboard web\n"
                "• `/listamedia` — lista foto/video caricati\n\n"
                "*Auto-apprendimento*\n"
                "• Rispondi (reply) a una notifica ⚠️ → bot salva auto in memoria\n"
                "• Manda foto/video con caption italiana → salvataggio auto multilingua\n"
                "• [✏️ Modifica memoria bot su GitHub](https://github.com/Lorenzog2006/appartamento-bot/edit/main/appartamento.txt) (per correggere/rimuovere)",
                parse_mode="Markdown"
            )
            return "ok"

        # ── /pausa <chat_id> ── disattiva AI per un cliente ──
        if testo.startswith("/pausa") and is_owner:
            parti = testo.split(" ", 1)
            if len(parti) < 2:
                invia_messaggio(chat_id,
                    "⏸️ *Pausa AI*\n\nUso: `/pausa <chat_id>`\n\n"
                    "Esempi:\n"
                    "• `/pausa 8668813727` (Telegram)\n"
                    "• `/pausa wa_393201234567` (WhatsApp)\n\n"
                    "Trovi il chat_id nelle notifiche dei messaggi: `[ID:...]` o `[WA:...]`",
                    parse_mode="Markdown"
                )
                return "ok"
            target = parti[1].strip()
            if set_pause(target, True):
                invia_messaggio(chat_id,
                    f"⏸️ Pausa AI attivata per `{target}`\n\n"
                    f"Il bot non risponderà più automaticamente. I messaggi continueranno ad arrivarti come notifica.\n\n"
                    f"Per riattivare: `/riprendi {target}`",
                    parse_mode="Markdown"
                )
            else:
                invia_messaggio(chat_id, f"❌ Errore nell'attivazione pausa per {target}")
            return "ok"

        # ── /riprendi <chat_id> ── riattiva AI per un cliente ──
        if testo.startswith("/riprendi") and is_owner:
            parti = testo.split(" ", 1)
            if len(parti) < 2:
                invia_messaggio(chat_id, "▶️ Uso: `/riprendi <chat_id>`", parse_mode="Markdown")
                return "ok"
            target = parti[1].strip()
            if set_pause(target, False):
                invia_messaggio(chat_id, f"▶️ AI riattivata per `{target}`", parse_mode="Markdown")
            else:
                invia_messaggio(chat_id, f"❌ Errore")
            return "ok"

        # ── /calendario ── lista completa prenotazioni canali (dal giorno corrente) ──
        if testo == "/calendario" and is_owner:
            invia_messaggio(chat_id, cal_format_full_list(), parse_mode="Markdown")
            return "ok"

        # ── /pulizie ── riepilogo turni pulizie ──
        if testo == "/pulizie" and is_owner:
            invia_messaggio(chat_id, pulizie_format_riepilogo(), parse_mode="Markdown")
            return "ok"

        # ── /cal ── channel manager: lista stub in attesa di completamento ──
        if testo.startswith("/cal") and is_owner:
            parti = testo.split(maxsplit=1)
            sub = parti[0]
            if sub == "/cal":
                invia_messaggio(chat_id, cal_format_pending_list(), parse_mode="Markdown")
                return "ok"
            if sub == "/calsync":
                # Trigger manuale del sync iCal (riusa la stessa logica del cron).
                try:
                    resp = cron_sync_ical()
                    # cron_sync_ical ritorna json.dumps(dict) o tuple (body, code, headers)
                    body = resp[0] if isinstance(resp, tuple) else resp
                    risultato = json.loads(body) if isinstance(body, str) else body
                    if "error" in risultato:
                        invia_messaggio(chat_id, f"❌ Errore sync: {risultato['error']}")
                    else:
                        a = risultato.get("airbnb", {})
                        b = risultato.get("booking", {})
                        invia_messaggio(chat_id,
                            f"🔄 Sync iCal completato:\n"
                            f"• Airbnb: {a.get('fetched',0)} eventi, {a.get('new',0)} nuovi, {a.get('seeded',0)} seed\n"
                            f"• Booking: {b.get('fetched',0)} eventi, {b.get('new',0)} nuovi, {b.get('seeded',0)} seed"
                        )
                except Exception as e:
                    invia_messaggio(chat_id, f"❌ Errore sync: {e}")
                return "ok"

        # ── /prenotazione ── wizard per aggiungere prenotazione manualmente ──
        if testo.startswith("/prenotazione") and is_owner:
            parti = testo.split(" ", 1)
            arg = parti[1].strip() if len(parti) > 1 else ""
            # ── /prenotazione one-shot: parse libero ──
            # Esempi:
            #   /prenotazione Mario 12/06-19/06 italian
            #   /prenotazione Anna Bianchi 15/06/2026 - 22/06/2026 french wa 393201234567
            if arg:
                ci_oneshot, co_oneshot = estrai_date(arg)
                lingue_map = {
                    "italian":"italian","italiano":"italian","it":"italian",
                    "english":"english","inglese":"english","en":"english",
                    "french":"french","francese":"french","fr":"french",
                    "spanish":"spanish","spagnolo":"spanish","es":"spanish",
                    "german":"german","tedesco":"german","de":"german",
                }
                lingua_match = None
                token_lingua = ""
                for tok in re.findall(r"[A-Za-z]+", arg.lower()):
                    if tok in lingue_map:
                        lingua_match = lingue_map[tok]
                        token_lingua = tok
                        break
                # contatto opzionale: "wa <num>" o "tg <id>"
                contatto_match = re.search(r"\b(wa|tg)\s+(\d{4,})", arg.lower())
                chat_id_finale = None
                canale = None
                if contatto_match:
                    if contatto_match.group(1) == "wa":
                        chat_id_finale = f"wa_{contatto_match.group(2)}"
                        canale = "whatsapp"
                    else:
                        chat_id_finale = contatto_match.group(2)
                        canale = "telegram"
                # nome: tutto prima della prima data
                nome = ""
                if ci_oneshot:
                    m_first_date = re.search(r"\d{1,2}/\d{1,2}", arg)
                    if m_first_date:
                        nome = arg[:m_first_date.start()].strip(" -,:")
                if ci_oneshot and co_oneshot and nome:
                    lingua = lingua_match or "italian"
                    if not chat_id_finale:
                        chat_id_finale = f"manual_{int(datetime.now().timestamp())}"
                    ok = salva_prenotazione(chat_id_finale, nome, ci_oneshot, co_oneshot, lingua, num_ospiti=2, culla=False)
                    if ok:
                        riga_canale = (f"📱 *{canale.title()}*: `{chat_id_finale}`\n✅ Promemoria automatici attivi."
                                       if canale else "ℹ️ _Nessun canale: niente promemoria automatici._")
                        invia_messaggio(chat_id,
                            f"✅ *Prenotazione salvata!*\n\n"
                            f"👤 {nome}\n"
                            f"📅 {ci_oneshot} → {co_oneshot}\n"
                            f"🌍 Lingua: {lingua}\n"
                            f"👥 Ospiti: 2 (default) — culla: no (default)\n\n"
                            f"{riga_canale}\n\n"
                            f"_Per modificare numero ospiti/culla usa /prenotazione (wizard)._",
                            parse_mode="Markdown"
                        )
                        try:
                            pulizie_trigger_da_prenotazione(nome, ci_oneshot, co_oneshot, 2, False)
                        except Exception:
                            pass
                    else:
                        invia_messaggio(chat_id, "❌ Errore salvataggio. Riprova col wizard: `/prenotazione`", parse_mode="Markdown")
                    return "ok"
                # Parsing incompleto → mostra formato e avvia wizard
                invia_messaggio(chat_id,
                    "⚠️ Non riesco a leggere tutti i dati. Avvio il wizard guidato.\n\n"
                    "💡 *Formato one-shot:* `/prenotazione Mario 12/06-19/06 italian [wa 393...]`",
                    parse_mode="Markdown"
                )
            # ── Wizard step-by-step ──
            wizard_pren_set(chat_id, {"step": "nome"})
            invia_messaggio(chat_id,
                "📅 *Nuova prenotazione* — passo 1/6\n\n"
                "Come si chiama l'ospite?\n\n"
                "_(scrivi /annulla per annullare in qualsiasi momento)_",
                parse_mode="Markdown"
            )
            return "ok"

        if testo == "/annulla" and is_owner and wizard_pren_get(chat_id):
            wizard_pren_clear(chat_id)
            invia_messaggio(chat_id, "❌ Prenotazione annullata.")
            return "ok"

        # ── Flusso prenotazione manuale ──
        stato_pren = wizard_pren_get(chat_id) if is_owner else None
        if stato_pren and not testo.startswith("/"):
            stato = stato_pren
            if stato["step"] == "nome":
                stato["nome"] = testo.strip()
                stato["step"] = "date"
                wizard_pren_set(chat_id, stato)
                invia_messaggio(chat_id,
                    f"✅ Ospite: *{stato['nome']}*\n\n"
                    f"📅 *Passo 2/6* — Date di check-in e check-out\n\n"
                    f"Scrivi in uno di questi formati:\n"
                    f"• `15/06/2026 - 22/06/2026`\n"
                    f"• `15 giugno - 22 giugno`\n"
                    f"• `dal 15/06 al 22/06`",
                    parse_mode="Markdown"
                )
                return "ok"
            elif stato["step"] == "date":
                ci, co = estrai_date(testo)
                if not ci or not co:
                    invia_messaggio(chat_id,
                        "❌ Non ho capito le date. Riprova nel formato `15/06/2026 - 22/06/2026`",
                        parse_mode="Markdown"
                    )
                    return "ok"
                stato["checkin"] = ci
                stato["checkout"] = co
                stato["step"] = "lingua"
                wizard_pren_set(chat_id, stato)
                invia_bottoni(chat_id,
                    f"✅ Check-in: {ci}\n✅ Check-out: {co}\n\n"
                    f"📅 *Passo 3/6* — Lingua dell'ospite",
                    [[
                        {"text": "🇮🇹 Italiano", "callback_data": "PREN_LANG:italian"},
                        {"text": "🇬🇧 English", "callback_data": "PREN_LANG:english"}
                    ], [
                        {"text": "🇫🇷 Français", "callback_data": "PREN_LANG:french"},
                        {"text": "🇪🇸 Español", "callback_data": "PREN_LANG:spanish"}
                    ], [
                        {"text": "🇩🇪 Deutsch", "callback_data": "PREN_LANG:german"}
                    ]],
                    parse_mode="Markdown"
                )
                return "ok"
            elif stato["step"] == "ospiti":
                # Step "ospiti": numero di persone (cifra)
                try:
                    n = int(re.sub(r"\D", "", testo.strip()) or "0")
                except Exception:
                    n = 0
                if n <= 0 or n > 20:
                    invia_messaggio(chat_id, "❌ Numero non valido. Scrivi solo il numero di ospiti (es: `3`).", parse_mode="Markdown")
                    return "ok"
                stato["num_ospiti"] = n
                stato["step"] = "culla"
                wizard_pren_set(chat_id, stato)
                invia_bottoni(chat_id,
                    f"✅ Ospiti: *{n}*\n\n"
                    f"📅 *Passo 5/6* — 🛏️ Serve montare la culla per un neonato?",
                    [[
                        {"text": "🛏️ Sì, serve culla", "callback_data": "PREN_CULLA:yes"},
                        {"text": "❌ No",              "callback_data": "PREN_CULLA:no"},
                    ]],
                    parse_mode="Markdown"
                )
                return "ok"
            elif stato["step"] == "contatto":
                # Step finale: ricevuto contatto
                contatto = testo.strip().lower()
                chat_id_finale = None
                canale = None
                if contatto in ("no", "skip", "salta"):
                    # Salva senza canale → niente promemoria automatici
                    chat_id_finale = f"manual_{int(datetime.now().timestamp())}"
                elif contatto.startswith("wa "):
                    numero = re.sub(r'\D', '', contatto[3:])  # solo cifre
                    if len(numero) < 8:
                        invia_messaggio(chat_id, "❌ Numero WhatsApp non valido. Riprova (es: `wa 393201234567`).", parse_mode="Markdown")
                        return "ok"
                    chat_id_finale = f"wa_{numero}"
                    canale = "whatsapp"
                elif contatto.startswith("tg "):
                    cid_str = re.sub(r'\D', '', contatto[3:])
                    if len(cid_str) < 4:
                        invia_messaggio(chat_id, "❌ Chat ID Telegram non valido.", parse_mode="Markdown")
                        return "ok"
                    chat_id_finale = cid_str
                    canale = "telegram"
                else:
                    invia_messaggio(chat_id,
                        "❌ Formato non riconosciuto. Usa:\n• `wa 393201234567` per WhatsApp\n• `tg 12345678` per Telegram chat ID\n• `no` per saltare (niente promemoria automatici)",
                        parse_mode="Markdown"
                    )
                    return "ok"
                num_ospiti = int(stato.get("num_ospiti") or 0)
                culla = bool(stato.get("culla"))
                ok = salva_prenotazione(chat_id_finale, stato["nome"], stato["checkin"], stato["checkout"], stato["lingua"], num_ospiti=num_ospiti, culla=culla)
                wizard_pren_clear(chat_id)
                if ok:
                    if canale:
                        riga_canale = f"📱 *{canale.title()}*: `{chat_id_finale}`\n\n✅ Da domani il bot manderà automaticamente i promemoria a questo ospite."
                    else:
                        riga_canale = "ℹ️ _Nessun canale impostato → niente promemoria automatici per questa prenotazione._"
                    invia_messaggio(chat_id,
                        f"✅ *Prenotazione salvata!*\n\n"
                        f"👤 {stato['nome']} ({num_ospiti} ospit{'e' if num_ospiti==1 else 'i'})\n"
                        f"📅 Check-in: {stato['checkin']}\n"
                        f"🏁 Check-out: {stato['checkout']}\n"
                        f"🌍 Lingua: {stato['lingua']}\n"
                        f"🛏️ Culla: {'SÌ' if culla else 'no'}\n\n"
                        f"{riga_canale}",
                        parse_mode="Markdown"
                    )
                    # Trigger pulizie: aggiorna turno per il check-out e turno per il check-in
                    try:
                        pulizie_trigger_da_prenotazione(stato["nome"], stato["checkin"], stato["checkout"], num_ospiti, culla)
                    except Exception:
                        pass
                else:
                    invia_messaggio(chat_id, "❌ Errore nel salvataggio. Riprova.")
                return "ok"

        # ── /prenotazioni ── lista prenotazioni esistenti ──
        if testo == "/prenotazioni" and is_owner:
            try:
                prenotazioni, _ = carica_prenotazioni()
                if not prenotazioni:
                    invia_messaggio(chat_id, "📭 Nessuna prenotazione registrata.\n\nUsa /prenotazione per aggiungerne una.")
                    return "ok"
                from datetime import timedelta as _td
                oggi = datetime.now().date()
                correnti, prossime, passate = [], [], []
                for cid, p in prenotazioni.items():
                    try:
                        ci = datetime.strptime(p["checkin"], "%d/%m/%Y").date()
                        co = datetime.strptime(p["checkout"], "%d/%m/%Y").date()
                        row = (cid, p, ci, co)
                        if co < oggi:
                            passate.append(row)
                        elif ci <= oggi <= co:
                            correnti.append(row)
                        else:
                            prossime.append(row)
                    except Exception:
                        pass
                prossime.sort(key=lambda r: r[2])
                passate.sort(key=lambda r: r[3], reverse=True)
                lines = ["📅 *PRENOTAZIONI*\n"]
                if correnti:
                    lines.append("🟢 *In corso:*")
                    for cid, p, ci, co in correnti:
                        canale = "📱" if str(cid).startswith("wa_") else ("💬" if str(cid).isdigit() else "📝")
                        lines.append(f"• {canale} {p.get('nome','?')} ({p.get('lingua','?')}) — {p['checkin']} → {p['checkout']}")
                    lines.append("")
                if prossime:
                    lines.append("🔵 *Prossime:*")
                    for cid, p, ci, co in prossime[:10]:
                        canale = "📱" if str(cid).startswith("wa_") else ("💬" if str(cid).isdigit() else "📝")
                        lines.append(f"• {canale} {p.get('nome','?')} ({p.get('lingua','?')}) — {p['checkin']} → {p['checkout']}")
                    lines.append("")
                if passate:
                    lines.append(f"⚪ *Passate* (ultime 5 su {len(passate)}):")
                    for cid, p, ci, co in passate[:5]:
                        lines.append(f"• {p.get('nome','?')} — {p['checkin']} → {p['checkout']}")
                lines.append("\n_Aggiungi: /prenotazione_")
                invia_messaggio(chat_id, "\n".join(lines), parse_mode="Markdown")
            except Exception as e:
                invia_messaggio(chat_id, f"❌ Errore: {e}")
            return "ok"

        # ── /dashboard ── manda link sicuro alla dashboard web ──
        if testo == "/dashboard" and is_owner:
            if not DASHBOARD_KEY:
                invia_messaggio(chat_id, "⚠️ DASHBOARD_KEY non configurata su Vercel.")
                return "ok"
            url = f"https://appartamento-bot.vercel.app/dashboard?key={DASHBOARD_KEY}"
            invia_messaggio(chat_id,
                f"🔗 *Dashboard*\n\n[Apri →]({url})\n\n"
                f"⚠️ Questo link contiene la chiave: non condividerlo!",
                parse_mode="Markdown"
            )
            return "ok"

        # ── /stats ──
        if testo == "/stats" and is_owner:
            try:
                invia_messaggio(chat_id, formatta_stats(), parse_mode="Markdown")
            except Exception as e:
                invia_messaggio(chat_id, f"Errore stats: {e}")
            return "ok"

        # ── /listamedia ── elenca tutti i media con bottoni di cancellazione ──
        if testo == "/listamedia" and is_owner:
            try:
                media_list = leggi_media()
                if not media_list:
                    invia_messaggio(chat_id, "📭 Nessun media salvato.")
                    return "ok"
                righe = ["📸 *Media salvati:*\n"]
                for i, m in enumerate(media_list, 1):
                    icona = "🎬" if m["tipo"] == "video" else "📸"
                    keywords = ", ".join(m["keywords"][:5])
                    if len(m["keywords"]) > 5:
                        keywords += f" (+{len(m['keywords'])-5})"
                    caption = m.get("caption", "").strip()
                    riga = f"*{i}.* {icona} _{keywords}_"
                    if caption:
                        riga += f"\n   📝 {caption[:80]}{'...' if len(caption) > 80 else ''}"
                    righe.append(riga)
                righe.append("\n👇 *Tocca il numero per cancellare:*")
                # Costruisci bottoni 🗑️ 1, 🗑️ 2, ... in righe di 4
                bottoni = []
                riga_btn = []
                for i in range(1, len(media_list) + 1):
                    riga_btn.append({"text": f"🗑️ {i}", "callback_data": f"DEL_MEDIA:{i}"})
                    if len(riga_btn) == 4:
                        bottoni.append(riga_btn)
                        riga_btn = []
                if riga_btn:
                    bottoni.append(riga_btn)
                bottoni.append([{"text": "❌ Chiudi", "callback_data": "DEL_MEDIA_CANCEL"}])
                invia_bottoni(chat_id, "\n\n".join(righe), bottoni, parse_mode="Markdown")
            except Exception as e:
                invia_messaggio(chat_id, f"❌ Errore: {e}")
            return "ok"

        # ── /rispondi ──
        if testo.startswith("/rispondi") and is_owner:
            parti = testo.split(" ", 2)
            if len(parti) >= 3:
                try:
                    invia_messaggio(int(parti[1]), f"💬 {parti[2]}")
                    invia_messaggio(chat_id, "✅ Risposta inviata!")
                except Exception as e:
                    invia_messaggio(chat_id, f"❌ Errore: {e}")
            return "ok"

        if testo.startswith("/"):
            return "ok"

        # ── Ospite in attesa di date ────────────────────────────────────────
        if not is_owner and chat_id in _attesa_date:
            checkin, checkout = estrai_date(testo)
            if checkin and checkout:
                info_attesa = _attesa_date.pop(chat_id)
                lingua      = info_attesa.get("lingua", "italian")
                # Conferma all'ospite
                conferma = CONFERMA_DATE.get(lingua, CONFERMA_DATE["english"]).format(
                    checkin=checkin, checkout=checkout
                )
                invia_messaggio(chat_id, conferma, remove_kb=True)
                # Salva su GitHub
                try:
                    salva_prenotazione(chat_id, nome, checkin, checkout, lingua)
                except Exception:
                    pass
                # Notifica Lorenzo con pulsanti
                nome_display = f"@{username}" if username else nome
                if OWNER_ID:
                    invia_bottoni(int(OWNER_ID),
                        f"📅 Nuova prenotazione registrata!\n\n"
                        f"Ospite: {nome_display} [ID:{chat_id}]\n"
                        f"📆 Check-in:  {checkin}\n"
                        f"🏁 Check-out: {checkout}",
                        [[
                            {"text": "✏️ Modifica date", "callback_data": f"MODIFICA_DATE:{chat_id}"},
                            {"text": "✅ Ok",             "callback_data": "DATE_OK"}
                        ]]
                    )
                return "ok"
            else:
                # Date non trovate — rispondi alla domanda se c'è, poi chiedi ancora le date
                lingua = _attesa_date[chat_id].get("lingua", "italian")
                # Proviamo a rispondere normalmente e aggiungiamo il reminder date
                try:
                    info  = leggi_info()
                    reply = chiedi_ai(testo, info, chat_id=chat_id)
                    aggiorna_storia(chat_id, testo, reply)
                    invia_messaggio(chat_id, reply, remove_kb=True)
                except Exception:
                    pass
                invia_messaggio(chat_id, ERRORE_DATE.get(lingua, ERRORE_DATE["english"]), remove_kb=True)
                return "ok"

        # ── Ospite tornato: saluto speciale (solo prima volta dopo lungo periodo) ──
        if not is_owner and is_ospite_tornato(chat_id):
            try:
                lingua_t = rileva_lingua(testo)
                msg_bt = BENTORNATO.get(lingua_t, BENTORNATO["english"]).format(nome=nome)
                invia_messaggio(chat_id, msg_bt)
                marca_bentornato(chat_id)
            except Exception:
                pass
            # NON return: prosegui poi con la risposta AI normale

        # ── Pausa AI: notifica Lorenzo e basta, non rispondere ──
        if not is_owner and is_paused(chat_id):
            if OWNER_ID:
                nome_display = f"@{username}" if username else nome
                _voce_pre = "🎙️ _Vocale trascritto:_ " if era_vocale else ""
                invia_bottoni(int(OWNER_ID),
                    f"⏸️ *[PAUSA]* {nome_display}\n\n❓ {_domanda_per_owner(_voce_pre, testo)}\n\n[ID:{chat_id}]",
                    [[{"text": "▶️ Riattiva AI", "callback_data": f"RIPRENDI:{chat_id}"}]],
                    parse_mode="Markdown"
                )
            try:
                aggiorna_user(chat_id, "telegram", nome, testo, rileva_lingua(testo), username)
            except Exception:
                pass
            return "ok"

        # ── GUARD: l'owner non deve mai ricevere risposte AI dal proprio bot ──
        # Se siamo qui ed è is_owner, qualcosa nei flussi precedenti non ha gestito
        # correttamente. Non rispondere e basta.
        if is_owner:
            return "ok"

        # ── Risposta AI ─────────────────────────────────────────────────────
        t_start = datetime.now().timestamp()
        try:
            info  = leggi_info()
            reply = chiedi_ai(testo, info, chat_id=chat_id)
            aggiorna_storia(chat_id, testo, reply)
            try:
                lingua_stat = rileva_lingua(testo)
                aggiorna_stats(testo, lingua_stat)
                aggiorna_daily_stats(testo, lingua_stat, chat_id)
                if not is_owner:
                    aggiorna_user(chat_id, "telegram", nome, testo, lingua_stat, username)
                # Analytics
                durata_sec = datetime.now().timestamp() - t_start
                non_risolto = bot_non_sa(reply)
                log_evento_analytics("telegram", _topic_di(testo), durata_sec,
                                     takeover=False, non_risolto=non_risolto,
                                     era_vocale=era_vocale)
                if non_risolto and not is_owner:
                    log_msg_non_risolto(testo, chat_id, lingua_stat)
            except Exception:
                pass
        except Exception:
            reply = "Mi dispiace, in questo momento non riesco a rispondere. Lo chiedo a Lorenzo e ti rispondo al più presto!"

        invia_messaggio(chat_id, reply, remove_kb=True)

        # ── Emergenze ──
        PAROLE_EMERGENZA = [
            "allagamento","allaga","perdita acqua","tubo rotto","guasto luce","luce non funziona",
            "corrente","blackout","senza corrente","senza luce","corto circuito","gas","odore gas",
            "riscaldamento","caldaia","ascensore bloccato",
            "flood","flooding","water leak","no electricity","power cut","gas leak",
            "inondation","fuite d'eau","panne électrique","coupure de courant",
            "fuga de agua","sin electricidad","wasserrohrbruch","stromausfall","gasgeruch"
        ]
        e_emergenza = any(p in testo.lower() for p in PAROLE_EMERGENZA)

        # ── Insoddisfazione ospite ──
        PAROLE_NEGATIVE = [
            # italiano
            "sporco","sporca","sporchi","non funziona","rotto","rotta","rotti","puzza","puzza",
            "disgustoso","disgustosa","pessimo","pessima","terribile","inaccettabile",
            "deluso","delusa","delusione","problema","problemi","lamentela","lamento",
            "non va","non va bene","vergogna","scandaloso","orribile","schifo","schifoso",
            # inglese
            "dirty","broken","disgusting","terrible","awful","horrible","unacceptable",
            "disappointed","disappointment","complaint","complain","not working","doesn't work",
            "problem","issue","filthy","stinks","smell bad","unhappy","unhygienic",
            # francese
            "sale","cassé","cassée","dégoûtant","terrible","horrible","inacceptable",
            "déçu","déçue","déception","plainte","problème","ne fonctionne pas","ça pue",
            "insatisfait","malpropre","scandaleux",
            # spagnolo
            "sucio","roto","asqueroso","terrible","horrible","inaceptable",
            "decepcionado","queja","problema","no funciona","huele mal","insatisfecho",
            # tedesco
            "schmutzig","kaputt","ekelhaft","schrecklich","furchtbar","inakzeptabel",
            "enttäuscht","beschwerde","problem","funktioniert nicht","riecht schlecht",
        ]
        t_lower = testo.lower()
        e_insoddisfatto = any(p in t_lower for p in PAROLE_NEGATIVE) and not e_emergenza
        e_arrabbiato = rileva_sentiment_negativo(testo) and not e_emergenza

        # ── Notifica proprietario ──
        if OWNER_ID and not is_owner:
            try:
                nome_display = f"@{username}" if username else nome
                _voce = "🎙️ Vocale trascritto: " if era_vocale else ""
                _voce_md = "🎙️ _Vocale trascritto:_ " if era_vocale else ""
                _q_md = _domanda_per_owner(_voce_md, testo)
                _q    = _domanda_per_owner(_voce, testo)
                if e_emergenza:
                    invia_messaggio(int(OWNER_ID),
                        f"🚨🚨 EMERGENZA TECNICA 🚨🚨\n\n"
                        f"Ospite: {nome_display} [ID:{chat_id}]\n\n"
                        f"❓ {_q}\n\n🤖 {reply}\n\n"
                        f"⚡ Rispondi subito all'ospite premendo Rispondi."
                    )
                elif e_arrabbiato:
                    invia_bottoni(int(OWNER_ID),
                        f"🚨 *ATTENZIONE — OSPITE ARRABBIATO*\n\n"
                        f"Ospite: {nome_display} [ID:{chat_id}]\n\n"
                        f"❓ {_domanda_per_owner(_voce_md, testo, italic=True)}\n\n"
                        f"🤖 {reply}",
                        [[
                            {"text": "💬 Rispondi qui", "callback_data": f"REPLY:ID:{chat_id}"},
                            {"text": "⏸️ Pausa", "callback_data": f"PAUSA:{chat_id}"}
                        ]],
                        parse_mode="Markdown"
                    )
                elif e_insoddisfatto:
                    invia_bottoni(int(OWNER_ID),
                        f"😤 OSPITE INSODDISFATTO\n\n"
                        f"Ospite: {nome_display} [ID:{chat_id}]\n\n"
                        f"❓ {_q}\n\n"
                        f"🤖 {reply}",
                        [[
                            {"text": "💬 Rispondi qui", "callback_data": f"REPLY:ID:{chat_id}"},
                            {"text": "⏸️ Pausa", "callback_data": f"PAUSA:{chat_id}"}
                        ]]
                    )
                else:
                    notifica_owner_aggregata(chat_id,
                        f"📩 {nome_display} [ID:{chat_id}]\n\n❓ {_q_md}\n\n🤖 {reply}",
                        [[
                            {"text": "💬 Rispondi qui", "callback_data": f"REPLY:ID:{chat_id}"},
                            {"text": "💬 Prendi chat", "callback_data": f"TAKEOVER:{chat_id}"},
                            {"text": "⏸️ Pausa", "callback_data": f"PAUSA:{chat_id}"}
                        ]],
                        parse_mode="Markdown"
                    )
            except Exception:
                pass

        # ── Media automatici ──
        if not is_owner:
            media = trova_media(testo)
            if media:
                try:
                    if media["tipo"] == "video":
                        invia_video(chat_id, media["file_id"], media["caption"])
                    else:
                        invia_foto(chat_id, media["file_id"], media["caption"])
                except Exception:
                    pass

        # ── Avviso "non sa rispondere" ──
        if OWNER_ID and not is_owner and bot_non_sa(reply) and not e_emergenza:
            nome_display = f"@{username}" if username else nome
            invia_bottoni(int(OWNER_ID),
                f"⚠️ RISPOSTA RICHIESTA\n\n"
                f"Ospite: {nome_display}\n"
                f"❓ \"{testo}\"\n\n"
                f"[ID:{chat_id}]",
                [[{"text": "💬 Rispondi qui", "callback_data": f"REPLY:ID:{chat_id}"}]]
            )

    except Exception:
        pass

    return "ok"


@app.route("/daily-report", methods=["GET", "POST"])
def daily_report():
    """Chiamato da Vercel Cron ogni sera alle 21:00 CET."""
    cron_secret = os.environ.get("CRON_SECRET", "").strip()
    if cron_secret:
        if request.headers.get("Authorization") != f"Bearer {cron_secret}":
            return ("Forbidden", 403)
    else:
        ua = request.headers.get("User-Agent", "").lower()
        if "vercel" not in ua:
            return ("Forbidden", 403)
    try:
        testo = formatta_daily_stats()
        invia_messaggio(int(OWNER_ID), testo, parse_mode="Markdown")
    except Exception:
        pass
    return "ok"

@app.route("/reset-keyboards")
def reset_keyboards():
    """Rimuove la tastiera rapida da tutti gli utenti con prenotazione."""
    if not DASHBOARD_KEY or request.args.get("key") != DASHBOARD_KEY:
        return ("Forbidden", 403)
    try:
        prenotazioni, _ = carica_prenotazioni()
        count = 0
        for chat_id in prenotazioni:
            try:
                invia_messaggio(int(chat_id), "🔄", remove_kb=True)
                count += 1
            except Exception:
                pass
        return f"ok — tastiera rimossa per {count} utenti"
    except Exception as e:
        return f"errore: {e}"

@app.route("/cron/scheduled", methods=["GET", "POST"])
def cron_scheduled():
    """Endpoint cron unificato. Eseguito ogni mattina. Decide cosa fare in base alla data."""
    # Auth: se CRON_SECRET è settato, è OBBLIGATORIO. Altrimenti accettiamo il fallback Vercel.
    cron_secret = os.environ.get("CRON_SECRET", "").strip()
    if cron_secret:
        if request.headers.get("Authorization") != f"Bearer {cron_secret}":
            return ("Forbidden", 403)
    else:
        # Fallback: solo User-Agent vercel-cron (spoofabile, ma non c'è secret configurato).
        ua = request.headers.get("User-Agent", "").lower()
        if "vercel" not in ua:
            return ("Forbidden", 403)

    risultato = {"promemoria": None, "report_mensile": None, "reminder_pulizie": None}
    # Sempre: esegui promemoria
    try:
        risultato["promemoria"] = esegui_promemoria()
    except Exception as e:
        try:
            log_errore("cron_promemoria_outer", e)
        except Exception:
            pass
        risultato["promemoria"] = "errore"

    # Sempre: reminder pulizie mattina dei check-out di oggi
    try:
        risultato["reminder_pulizie"] = esegui_reminder_pulizie()
    except Exception as e:
        try:
            log_errore("cron_reminder_pulizie_outer", e)
        except Exception:
            pass
        risultato["reminder_pulizie"] = "errore"

    # Solo il 1° del mese: report mensile
    try:
        if datetime.now().day == 1:
            risultato["report_mensile"] = esegui_report_mensile()
    except Exception as e:
        try:
            log_errore("cron_report_outer", e)
        except Exception:
            pass

    return json.dumps(risultato)


@app.route("/")
def health():
    return "Bot attivo! ✓"


# ── Dashboard ─────────────────────────────────────────────────────────────────
def _check_dash_key():
    """Verifica chiave dashboard. Ritorna True se valida, False altrimenti."""
    if not DASHBOARD_KEY:
        return False
    return request.args.get("key", "") == DASHBOARD_KEY

def costi_meta(giorni=30):
    """Recupera analytics conversazioni WhatsApp degli ultimi N giorni e calcola costo."""
    if not (WA_TOKEN and WA_PHONE_ID):
        return {"errore": "WA_TOKEN o WA_PHONE_ID mancanti", "costo_eur": 0, "conv_per_categoria": {}}
    waba_id = "1476202720613528"
    end_ts = int(datetime.now().timestamp())
    start_ts = end_ts - giorni * 86400
    try:
        # Endpoint Meta: conversation_analytics con dimensione CONVERSATION_CATEGORY
        url = (
            f"https://graph.facebook.com/v22.0/{waba_id}"
            f"?fields=conversation_analytics.start({start_ts}).end({end_ts})"
            f".granularity(DAILY).dimensions(%5B%22CONVERSATION_CATEGORY%22%5D)"
        )
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {WA_TOKEN}"})
        r = urllib.request.urlopen(req, timeout=8)
        data = json.loads(r.read())
        analytics = data.get("conversation_analytics", {}).get("data", [])
        per_cat = {}
        if analytics:
            for dp in analytics[0].get("data_points", []):
                cat = dp.get("conversation_category", "UNKNOWN")
                per_cat[cat] = per_cat.get(cat, 0) + int(dp.get("conversation", 0))
        # Tariffe approssimative Italia 2025-2026 (€/conversazione)
        rates = {
            "MARKETING": 0.0691,
            "UTILITY": 0.0341,
            "AUTHENTICATION": 0.0341,
            "SERVICE": 0.0,  # gratis dal 2024
        }
        costo = 0.0
        for cat, n in per_cat.items():
            costo += n * rates.get(cat, 0)
        return {
            "giorni": giorni,
            "conv_per_categoria": per_cat,
            "totale_conv": sum(per_cat.values()),
            "costo_eur": round(costo, 4)
        }
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        return {"errore": f"HTTP {e.code}: {body}", "costo_eur": 0, "conv_per_categoria": {}}
    except Exception as e:
        return {"errore": str(e)[:200], "costo_eur": 0, "conv_per_categoria": {}}


def costi_claude_stimati(giorni=30):
    """Stima costi Claude basata sui messaggi tracciati negli ultimi N giorni."""
    try:
        _carica_users_da_github()
    except Exception:
        pass
    ora = datetime.now()
    msg_recenti = 0
    msg_lifetime = 0
    for u in _users.values():
        msg_lifetime += int(u.get("totale_msg", 0))
        try:
            ult = datetime.strptime((u.get("ultimo_msg", "") or "")[:19], "%Y-%m-%dT%H:%M:%S")
            if (ora - ult).days <= giorni:
                # approx: tutti i suoi messaggi sono nei recenti
                # (errore +-5% se cliente ha scritto sia recente che vecchio)
                msg_recenti += int(u.get("totale_msg", 0))
        except Exception:
            pass
    # Stima Haiku 4.5 con prompt caching attivo:
    # - Cache hit (~70%): 5000 tok × $0.10/M + 500 tok × $5/M = $0.003
    # - Cache miss (~30%): 5000 tok × $1.00/M + 500 tok × $5/M = $0.0075
    # Media ponderata: 0.7 × $0.003 + 0.3 × $0.0075 = ~$0.0044/msg ≈ €0.004
    costo_msg_eur = 0.004
    return {
        "giorni": giorni,
        "messaggi_recenti": msg_recenti,
        "messaggi_lifetime": msg_lifetime,
        "costo_recenti_eur": round(msg_recenti * costo_msg_eur, 4),
        "costo_lifetime_eur": round(msg_lifetime * costo_msg_eur, 4),
        "modello": "claude-haiku-4-5 (con prompt caching)",
        "costo_per_msg": costo_msg_eur,
        "nota": "Stima Haiku 4.5 con caching: system prompt cachato per 5 min ($0.10/M invece di $1/M)"
    }


def _aggrega_dashboard_data():
    """Aggrega tutti i dati per la dashboard. Best-effort, niente errori che bloccano."""
    from flask import jsonify
    out = {
        "kpi": {"totale_lifetime": 0, "totale_oggi": 0, "ospiti_attivi": 0, "totale_clienti": 0},
        "lingue": {}, "argomenti": {},
        "prenotazioni_correnti": [], "prenotazioni_prossime": [], "prenotazioni_passate": [],
        "conversazioni_attive": [], "top_clienti": [],
        "media": [], "generato_il": datetime.now().strftime("%d/%m/%Y %H:%M")
    }
    # 1. Stats lifetime + oggi
    try:
        s, _ = carica_stats()
        out["kpi"]["totale_lifetime"] = s.get("totale", 0)
        out["lingue"] = s.get("lingue", {})
        out["argomenti"] = s.get("argomenti", {})
    except Exception:
        pass
    try:
        ds, _ = carica_daily_stats()
        oggi_str = datetime.now().strftime("%d/%m/%Y")
        if ds.get("data") == oggi_str:
            out["kpi"]["totale_oggi"] = ds.get("totale", 0)
    except Exception:
        pass
    # 2. Prenotazioni
    try:
        prenotazioni, _ = carica_prenotazioni()
        oggi = datetime.now()
        for cid, p in prenotazioni.items():
            try:
                ci_d = datetime.strptime(p["checkin"], "%d/%m/%Y")
                co_d = datetime.strptime(p["checkout"], "%d/%m/%Y")
                row = {"chat_id": cid, **p}
                if co_d < oggi:
                    out["prenotazioni_passate"].append(row)
                elif ci_d <= oggi <= co_d:
                    out["prenotazioni_correnti"].append(row)
                else:
                    out["prenotazioni_prossime"].append(row)
            except Exception:
                pass
        # ordina prossime per data ascendente
        out["prenotazioni_prossime"].sort(key=lambda r: datetime.strptime(r.get("checkin","31/12/2099"), "%d/%m/%Y"))
        out["prenotazioni_passate"].sort(key=lambda r: datetime.strptime(r.get("checkout","01/01/2000"), "%d/%m/%Y"), reverse=True)
    except Exception:
        pass
    # 3. Conversazioni attive (storia ultime 24h)
    try:
        _carica_conversazioni_da_github()
        ora = datetime.now().timestamp()
        for cid, conv in _conversazioni.items():
            ultimo = conv.get("ultimo", 0)
            if ora - ultimo > 24 * 3600:
                continue
            canale = "whatsapp" if cid.startswith("wa_") else "telegram"
            ultimo_dt = datetime.fromtimestamp(ultimo).strftime("%d/%m %H:%M")
            n_msg = len(conv.get("storia", [])) // 2
            # Trova nome utente
            u = _users.get(cid, {}) if _users_loaded else {}
            nome = u.get("nome", "?")
            out["conversazioni_attive"].append({
                "chat_id": cid, "canale": canale, "nome": nome,
                "ultimo_msg": ultimo_dt, "msg_in_storia": n_msg,
                "ultimo_ts": ultimo
            })
        out["conversazioni_attive"].sort(key=lambda x: x["ultimo_ts"], reverse=True)
    except Exception:
        pass
    # 4. Top clienti per attività
    try:
        _carica_users_da_github()
        out["kpi"]["totale_clienti"] = len(_users)
        clienti = []
        for cid, u in _users.items():
            clienti.append({
                "chat_id": cid,
                "canale": u.get("canale", "?"),
                "nome": u.get("nome", "?"),
                "username": u.get("username", ""),
                "totale_msg": u.get("totale_msg", 0),
                "primo_msg": u.get("primo_msg", ""),
                "ultimo_msg": u.get("ultimo_msg", ""),
                "lingua": u.get("lingua", ""),
                "topic_top": max(u.get("topic_count", {}).items(), key=lambda x: x[1])[0] if u.get("topic_count") else "—"
            })
        clienti.sort(key=lambda x: x["totale_msg"], reverse=True)
        out["top_clienti"] = clienti[:20]
        # ospiti_attivi = clienti con ultimo_msg negli ultimi 7gg
        ora_iso = datetime.now()
        attivi = 0
        for u in _users.values():
            try:
                ult = datetime.strptime(u.get("ultimo_msg", "")[:19], "%Y-%m-%dT%H:%M:%S")
                if (ora_iso - ult).total_seconds() < 7 * 86400:
                    attivi += 1
            except Exception:
                pass
        out["kpi"]["ospiti_attivi"] = attivi
    except Exception:
        pass
    # 5. Media salvati
    try:
        for i, m in enumerate(leggi_media(), 1):
            out["media"].append({
                "n": i, "tipo": m["tipo"],
                "keywords": ", ".join(m["keywords"][:6]),
                "caption": (m.get("caption") or "")[:120]
            })
    except Exception:
        pass
    return out


def _aggrega_analytics():
    """Aggrega le metriche analytics avanzate per la dashboard."""
    out = {
        "tempo_medio_sec": 0,
        "tempo_p50": 0, "tempo_p90": 0,
        "tasso_takeover": 0,
        "tasso_non_risolti": 0,
        "tasso_vocali": 0,
        "totale_eventi_30gg": 0,
        "heatmap": [[0]*24 for _ in range(7)],  # 7 giorni x 24 ore
        "trending": [],
        "distrib_tempi": {"0-2s": 0, "2-5s": 0, "5-10s": 0, "10s+": 0},
        "top_non_risolti": [],
        "generato_il": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }
    try:
        _carica_analytics_da_github()
        from datetime import timedelta as _td
        ora = datetime.now()
        sette_gg = (ora - _td(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        quattordici_gg = (ora - _td(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
        durate = []
        n_takeover = 0
        n_non_risolti = 0
        n_vocali = 0
        topic_7gg = {}
        topic_7_14gg = {}
        for e in _analytics:
            ts = e.get("ts", "")
            out["totale_eventi_30gg"] += 1
            # Durata
            ds = float(e.get("ds") or 0)
            durate.append(ds)
            if ds < 2: out["distrib_tempi"]["0-2s"] += 1
            elif ds < 5: out["distrib_tempi"]["2-5s"] += 1
            elif ds < 10: out["distrib_tempi"]["5-10s"] += 1
            else: out["distrib_tempi"]["10s+"] += 1
            # Counters
            if e.get("to"): n_takeover += 1
            if e.get("nr"): n_non_risolti += 1
            if e.get("vo"): n_vocali += 1
            # Heatmap (giorno settimana × ora)
            try:
                dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
                # weekday(): 0=lun, 6=dom
                out["heatmap"][dt.weekday()][dt.hour] += 1
            except Exception:
                pass
            # Trending: confronto 7gg vs 7-14gg
            tp = e.get("tp", "altro")
            if ts >= sette_gg:
                topic_7gg[tp] = topic_7gg.get(tp, 0) + 1
            elif ts >= quattordici_gg:
                topic_7_14gg[tp] = topic_7_14gg.get(tp, 0) + 1
        n = len(durate)
        if n:
            out["tempo_medio_sec"] = round(sum(durate)/n, 2)
            durate_ord = sorted(durate)
            out["tempo_p50"] = round(durate_ord[n//2], 2)
            out["tempo_p90"] = round(durate_ord[int(n*0.9)] if n > 1 else durate_ord[0], 2)
            out["tasso_takeover"] = round(n_takeover/n*100, 1)
            out["tasso_non_risolti"] = round(n_non_risolti/n*100, 1)
            out["tasso_vocali"] = round(n_vocali/n*100, 1)
        # Trending: tutti i topic visti
        all_topics = set(topic_7gg.keys()) | set(topic_7_14gg.keys())
        for tp in all_topics:
            v7 = topic_7gg.get(tp, 0)
            v14 = topic_7_14gg.get(tp, 0)
            if v14 > 0:
                delta_pct = int((v7 - v14)/v14*100)
            else:
                delta_pct = 100 if v7 > 0 else 0
            out["trending"].append({
                "topic": tp,
                "ultimi_7gg": v7,
                "precedenti_7gg": v14,
                "delta_pct": delta_pct
            })
        out["trending"].sort(key=lambda x: -x["ultimi_7gg"])
        out["trending"] = out["trending"][:10]
    except Exception:
        pass
    # Top messaggi non risolti (da users.json)
    try:
        _carica_users_da_github()
        all_nr = []
        for cid, u in _users.items():
            nome = u.get("nome", "?")
            for nr in u.get("non_risolti", []):
                all_nr.append({
                    "ts": nr.get("ts", ""),
                    "domanda": nr.get("domanda", ""),
                    "lingua": nr.get("lingua", ""),
                    "nome": nome,
                    "chat_id": cid
                })
        all_nr.sort(key=lambda x: x["ts"], reverse=True)
        out["top_non_risolti"] = all_nr[:15]
    except Exception:
        pass
    return out


def _aggrega_costi():
    """Aggrega tutti i costi (Meta + Claude stimati) per la sezione Costi."""
    meta = costi_meta(30)
    claude = costi_claude_stimati(30)
    totale = round(meta.get("costo_eur", 0) + claude.get("costo_recenti_eur", 0), 2)
    return {
        "meta": meta,
        "claude": claude,
        "groq": {"costo_eur": 0, "nota": "Free tier: 14400 req/giorno gratuiti"},
        "telegram": {"costo_eur": 0, "nota": "Bot API completamente gratuita"},
        "github": {"costo_eur": 0, "nota": "Repo privato — gratuito"},
        "vercel": {"costo_eur": 0, "nota": "Hobby tier — gratuito"},
        "totale_eur": totale,
        "periodo_giorni": 30,
        "generato_il": datetime.now().strftime("%d/%m/%Y %H:%M")
    }

@app.route("/dashboard/api/data")
def dashboard_api_data():
    if not _check_dash_key():
        return ("Forbidden", 403)
    from flask import jsonify
    return jsonify(_aggrega_dashboard_data())


@app.route("/dashboard/api/costi")
def dashboard_api_costi():
    if not _check_dash_key():
        return ("Forbidden", 403)
    from flask import jsonify
    return jsonify(_aggrega_costi())


@app.route("/dashboard/api/analytics")
def dashboard_api_analytics():
    if not _check_dash_key():
        return ("Forbidden", 403)
    from flask import jsonify
    return jsonify(_aggrega_analytics())


@app.route("/dashboard/conversation/<path:chat_id>")
def dashboard_conversation(chat_id):
    if not _check_dash_key():
        return ("Forbidden", 403)
    from flask import Response
    _carica_conversazioni_da_github()
    _carica_users_da_github()
    conv = _conversazioni.get(str(chat_id)) or {"storia": []}
    user = _users.get(str(chat_id)) or {}
    nome = user.get("nome", "Cliente")
    canale = "📱 WhatsApp" if str(chat_id).startswith("wa_") else "💬 Telegram"
    storia = conv.get("storia", [])
    msgs_html = ""
    for m in storia:
        role = m.get("role", "")
        content = (m.get("content", "") or "").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        if role == "user":
            msgs_html += f'<div class="msg user"><div class="role">👤 Cliente</div>{content}</div>'
        else:
            msgs_html += f'<div class="msg bot"><div class="role">🤖 Bot / Lorenzo</div>{content}</div>'
    if not msgs_html:
        msgs_html = '<div class="empty">Nessun messaggio in storia (potrebbe essere scaduta).</div>'
    info_user = ""
    if user:
        topic_top = ", ".join([f"{k}: {v}" for k, v in sorted(user.get("topic_count", {}).items(), key=lambda x: -x[1])[:5]])
        info_user = f"""<div class="info">
        <strong>Totale messaggi:</strong> {user.get('totale_msg', 0)}<br>
        <strong>Lingua:</strong> {user.get('lingua', '?')}<br>
        <strong>Primo messaggio:</strong> {user.get('primo_msg', '?')[:16].replace('T', ' ')}<br>
        <strong>Ultimo messaggio:</strong> {user.get('ultimo_msg', '?')[:16].replace('T', ' ')}<br>
        <strong>Argomenti:</strong> {topic_top or '—'}
        </div>"""
    key = request.args.get("key", "")
    html = f"""<!DOCTYPE html>
<html lang="it"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chat con {nome}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f5f7fa;color:#222;padding:20px;max-width:760px;margin:0 auto}}
h1{{color:#0066cc;margin-bottom:6px;font-size:22px}}
.sub{{color:#888;font-size:13px;margin-bottom:16px}}
.info{{background:#fff;padding:14px;border-radius:10px;margin-bottom:16px;font-size:14px;line-height:1.6;box-shadow:0 2px 4px rgba(0,0,0,.05)}}
.msg{{padding:10px 14px;border-radius:12px;margin-bottom:10px;max-width:80%;font-size:14px;line-height:1.4}}
.msg.user{{background:#dbeafe;margin-left:auto;text-align:right}}
.msg.bot{{background:#fff;border:1px solid #eee}}
.role{{font-size:11px;color:#666;text-transform:uppercase;margin-bottom:4px;font-weight:600}}
.empty{{text-align:center;color:#999;padding:40px;font-style:italic}}
a{{color:#0066cc}}
</style></head><body>
<h1>{canale} — {nome}</h1>
<div class="sub">Chat ID: <code>{chat_id}</code> &nbsp;·&nbsp; <a href="/dashboard?key={key}">← Torna alla dashboard</a></div>
{info_user}
<div>{msgs_html}</div>
</body></html>"""
    return Response(html, mimetype="text/html")


@app.route("/dashboard")
def dashboard_page():
    if not _check_dash_key():
        return ("Forbidden — chiave mancante o errata", 403)
    from flask import Response
    key = request.args.get("key", "")
    html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0,viewport-fit=cover">
<title>Dashboard Bot</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
:root{
  --bg:#000;--card-r1:#2a0a0a;--card-r2:#5b1010;--card-r3:#7a1818;
  --card-y1:#2a200a;--card-y2:#5b3e10;--card-y3:#a07208;
  --card-g1:#0a2418;--card-g2:#0f4a30;--card-g3:#16704a;
  --txt:#fff;--txt-mute:rgba(255,255,255,.55);--txt-light:rgba(255,255,255,.85);
  --tab-bg:rgba(28,28,30,.86);--accent:#22c55e;
}
html,body{background:var(--bg);color:var(--txt);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
body{padding:14px 14px 110px;max-width:1100px;margin:0 auto;min-height:100vh}
header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding:6px 4px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11.5px;color:var(--txt-light)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.dot{width:9px;height:9px;border-radius:50%}
.dot.r{background:#e84141}.dot.y{background:#f7c84a}.dot.g{background:#22c55e}
.gear{background:rgba(255,255,255,.08);border:0;color:#fff;width:34px;height:34px;border-radius:50%;font-size:16px;cursor:pointer}
.section{font-size:12px;letter-spacing:.6px;color:var(--txt-mute);margin:14px 4px 10px;text-transform:uppercase;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:11px}
@media(min-width:900px){.grid{grid-template-columns:repeat(3,1fr)}}
@media(min-width:1200px){.grid{grid-template-columns:repeat(4,1fr)}}
.card{
  position:relative;border-radius:22px;padding:16px 16px 14px;min-height:180px;
  display:flex;flex-direction:column;justify-content:space-between;
  box-shadow:0 2px 12px rgba(0,0,0,.6);overflow:hidden;cursor:pointer;
  transition:transform .15s ease;
}
.card:active{transform:scale(.98)}
.card.red{background:radial-gradient(120% 100% at 100% 100%,var(--card-r3) 0%,var(--card-r2) 45%,var(--card-r1) 100%)}
.card.yellow{background:radial-gradient(120% 100% at 100% 100%,var(--card-y3) 0%,var(--card-y2) 45%,var(--card-y1) 100%)}
.card.green{background:radial-gradient(120% 100% at 100% 100%,var(--card-g3) 0%,var(--card-g2) 45%,var(--card-g1) 100%)}
.card .top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.card .ttl{font-size:13.5px;font-weight:700;text-transform:uppercase;letter-spacing:.6px;color:#fff}
.card .rank{background:rgba(0,0,0,.3);padding:3px 10px;border-radius:10px;font-size:11px;color:#fff;flex-shrink:0;font-weight:600}
.card .big{font-size:46px;font-weight:800;line-height:1;letter-spacing:-1px;margin-top:14px;color:#fff}
.card .sub{font-size:13px;color:#fff;font-weight:600;opacity:.95;margin-top:4px}
.card .lines{margin-top:8px;font-size:11px;line-height:1.5;color:rgba(255,255,255,.65)}
.card .lines b{color:rgba(255,255,255,.85);font-weight:600}
.empty{padding:50px 20px;text-align:center;color:var(--txt-mute);font-size:14px}
.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:11px;margin-bottom:6px}
@media(min-width:600px){.kpis{grid-template-columns:repeat(4,1fr)}}
.kpi{background:rgba(255,255,255,.05);border-radius:18px;padding:14px 14px;display:flex;flex-direction:column;justify-content:center;min-height:90px}
.kpi .v{font-size:32px;font-weight:800;color:#fff;line-height:1}
.kpi .l{font-size:11px;color:var(--txt-mute);margin-top:4px;text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.tabs{
  position:fixed;bottom:max(20px,env(safe-area-inset-bottom));left:50%;transform:translateX(-50%);
  background:var(--tab-bg);backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px);
  border-radius:30px;padding:10px 8px;display:flex;gap:6px;box-shadow:0 4px 24px rgba(0,0,0,.5);
  border:1px solid rgba(255,255,255,.08);z-index:100;
}
.tab{background:transparent;border:0;color:rgba(255,255,255,.55);padding:7px 11px;border-radius:24px;cursor:pointer;font-size:11.5px;font-weight:600;display:flex;flex-direction:column;align-items:center;gap:2px;min-width:58px}
.tab.active{color:var(--accent);background:rgba(34,197,94,.15)}
.tab .ti{font-size:17px}
.view{display:none;animation:fadeIn .2s ease}
.view.active{display:block}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
.refresh{position:fixed;top:14px;right:14px;background:rgba(255,255,255,.1);border:0;color:#fff;width:36px;height:36px;border-radius:50%;font-size:16px;cursor:pointer;z-index:50}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;padding:14px;z-index:200;backdrop-filter:blur(8px)}
.modal.open{display:flex}
.modal-box{background:#1c1c1e;border-radius:22px;padding:22px;max-width:420px;width:100%;color:#fff;border:1px solid rgba(255,255,255,.1)}
.modal-box h3{margin-bottom:14px;font-size:18px}
.modal-box .row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.08);font-size:13px}
.modal-box .row b{color:rgba(255,255,255,.7);font-weight:500}
.modal-box .row span{color:#fff;font-weight:600;text-align:right}
.modal-box .actions{margin-top:16px;display:flex;gap:8px}
.modal-box button{flex:1;background:rgba(255,255,255,.1);color:#fff;border:0;padding:12px;border-radius:12px;cursor:pointer;font-weight:600;font-size:13px}
.modal-box button.primary{background:var(--accent);color:#000}
.list-row{display:flex;justify-content:space-between;align-items:center;padding:14px;background:rgba(255,255,255,.04);border-radius:14px;margin-bottom:8px;cursor:pointer}
.list-row:active{background:rgba(255,255,255,.08)}
.list-row .L{display:flex;align-items:center;gap:12px}
.list-row .L .ic{font-size:20px}
.list-row .name{font-weight:600;font-size:14px}
.list-row .meta{font-size:11.5px;color:var(--txt-mute);margin-top:2px}
.list-row .R{text-align:right}
.list-row .R .num{font-size:16px;font-weight:700}
.list-row .R .lbl{font-size:10.5px;color:var(--txt-mute);margin-top:2px}
.aggiornata{font-size:11px;color:var(--txt-mute);margin:8px 4px 0;text-align:center}
.cli-row{display:flex;align-items:center;gap:12px;background:rgba(255,255,255,.04);border-radius:14px;padding:14px 12px;margin-bottom:8px;cursor:pointer;transition:background .15s ease}
.cli-row:active{background:rgba(255,255,255,.08)}
.cli-rank{flex-shrink:0;min-width:42px;text-align:center;color:#fff;font-weight:700;font-size:12px;padding:6px 8px;border-radius:10px}
.cli-main{flex:1;min-width:0}
.cli-name{font-size:14.5px;font-weight:600;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cli-period{font-size:12px;color:var(--txt-mute);margin-top:3px}
.cli-count{flex-shrink:0;text-align:right;min-width:48px}
.cli-num{font-size:22px;font-weight:800;color:#fff;line-height:1}
.cli-lbl{font-size:10.5px;color:var(--txt-mute);margin-top:2px;text-transform:uppercase;letter-spacing:.4px}
.costi-tot{background:radial-gradient(120% 100% at 100% 100%,#16704a 0%,#0f4a30 50%,#0a2418 100%);border-radius:22px;padding:24px;text-align:center;margin-bottom:8px;box-shadow:0 2px 12px rgba(0,0,0,.6)}
.costi-tot .v{font-size:42px;font-weight:800;color:#fff;line-height:1.05}
.costi-tot .l{font-size:12px;color:rgba(255,255,255,.75);margin-top:6px;text-transform:uppercase;letter-spacing:.5px}
.cost-row{display:flex;align-items:center;gap:12px;background:rgba(255,255,255,.04);border-radius:14px;padding:14px 14px;margin-bottom:8px}
.cost-row .ic{font-size:22px;flex-shrink:0}
.cost-row .info{flex:1;min-width:0}
.cost-row .nm{font-size:14px;font-weight:600;color:#fff}
.cost-row .nt{font-size:11.5px;color:var(--txt-mute);margin-top:3px}
.cost-row .pr{font-size:18px;font-weight:700;color:#fff;flex-shrink:0;text-align:right;min-width:80px}
.cost-row .pr.zero{color:var(--accent)}
.cost-link{display:inline-block;margin-top:6px;font-size:11.5px;color:#5aa3ff;text-decoration:none;background:rgba(90,163,255,.12);padding:4px 8px;border-radius:8px;font-weight:500}
.cost-link:active{background:rgba(90,163,255,.2)}
.costi-note{background:rgba(255,255,255,.04);border-radius:14px;padding:14px;font-size:12px;line-height:1.55;color:var(--txt-mute)}
.cat-row{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:rgba(255,255,255,.04);border-radius:10px;margin-bottom:6px;font-size:13px}
.cat-row .free{color:var(--accent);font-weight:600}
.cat-row .paid{color:#f7c84a;font-weight:600}
.heatmap-wrap{overflow-x:auto;background:rgba(255,255,255,.04);border-radius:14px;padding:14px}
.heatmap{display:inline-grid;grid-template-columns:auto repeat(24,minmax(14px,1fr));gap:2px;font-size:10px;color:var(--txt-mute);min-width:600px}
.heatmap .hcorner{background:transparent}
.heatmap .hour-label{text-align:center;padding:2px;color:var(--txt-mute)}
.heatmap .day-label{padding:2px 6px 2px 0;text-align:right;font-weight:600;color:#fff}
.heatmap .cell{height:18px;border-radius:3px;background:rgba(255,255,255,.06)}
.tempi-row{display:flex;gap:8px;margin-bottom:10px}
.tempi-cell{flex:1;background:rgba(255,255,255,.04);border-radius:10px;padding:12px;text-align:center}
.tempi-cell .v{font-size:18px;font-weight:700;color:#fff}
.tempi-cell .l{font-size:10.5px;color:var(--txt-mute);margin-top:3px;text-transform:uppercase}
.trending-row{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:rgba(255,255,255,.04);border-radius:10px;margin-bottom:6px;font-size:13px}
.trending-row .delta-up{color:var(--accent);font-weight:600}
.trending-row .delta-down{color:#e84141;font-weight:600}
.trending-row .delta-flat{color:var(--txt-mute)}
.nr-row{background:rgba(255,196,77,.08);border-left:3px solid #f7c84a;padding:10px 14px;border-radius:8px;margin-bottom:6px;font-size:13px}
.nr-row .domanda{color:#fff;margin-bottom:4px}
.nr-row .meta{font-size:11px;color:var(--txt-mute)}
</style>
</head>
<body>
<button class="refresh" onclick="caricaDati()" title="Aggiorna">↻</button>

<header>
  <div class="legend">
    <span><span class="dot r"></span>Inattivo</span>
    <span><span class="dot y"></span>Recente</span>
    <span><span class="dot g"></span>Attivo</span>
  </div>
</header>

<!-- VIEW 1: CLIENTI (Top per attività) -->
<div class="view active" id="view-clienti">
  <div class="section">Tutti i clienti</div>
  <div class="grid" id="clientiGrid"><div class="empty">Caricamento...</div></div>
</div>

<!-- VIEW 2: ARGOMENTI -->
<div class="view" id="view-argomenti">
  <div class="section">Argomenti più richiesti</div>
  <div class="grid" id="argomentiGrid"><div class="empty">Caricamento...</div></div>
</div>

<!-- VIEW 3: PRENOTAZIONI -->
<div class="view" id="view-prenotazioni">
  <div class="section">Prenotazioni in corso</div>
  <div id="prenCorrenti"></div>
  <div class="section">Prossime prenotazioni</div>
  <div id="prenProssime"></div>
  <div class="section">Storico</div>
  <div id="prenPassate"></div>
</div>

<!-- VIEW ANALYTICS -->
<div class="view" id="view-analytics">
  <div class="section">Performance bot (ultimi 30 giorni)</div>
  <div class="kpis" id="analyticsKpis"></div>
  <div class="section">Distribuzione tempi risposta</div>
  <div id="analyticsTempi"></div>
  <div class="section">📈 Trending argomenti (ultimi 7gg vs precedenti)</div>
  <div id="analyticsTrending"></div>
  <div class="section">🔥 Heatmap traffico (giorno × ora)</div>
  <div class="heatmap-wrap" id="analyticsHeatmap"></div>
  <div class="section">⚠️ Top messaggi non risolti</div>
  <div id="analyticsNonRisolti"></div>
</div>

<!-- VIEW 5: COSTI -->
<div class="view" id="view-costi">
  <div class="section">Costi servizi (ultimi 30 giorni)</div>
  <div id="costiTotale"></div>
  <div class="section">Dettaglio per servizio</div>
  <div id="costiList"></div>
  <div class="section">WhatsApp — conversazioni per categoria</div>
  <div id="costiMetaCat"></div>
  <div class="section">Note</div>
  <div class="costi-note" id="costiNote"></div>
</div>

<!-- VIEW 4: PANORAMICA -->
<div class="view" id="view-stats">
  <div class="section">Panoramica</div>
  <div class="kpis" id="kpis"></div>
  <div class="section">Conversazioni attive (24h)</div>
  <div id="conversazioni"></div>
  <div class="section">Lingue</div>
  <div class="grid" id="lingueGrid"></div>
  <div class="section">Media salvati</div>
  <div id="mediaList"></div>
</div>

<div class="aggiornata" id="aggiornata">Caricamento...</div>

<!-- Bottom tab bar -->
<nav class="tabs">
  <button class="tab active" data-view="clienti"><span class="ti">👥</span>Clienti</button>
  <button class="tab" data-view="argomenti"><span class="ti">💡</span>Argom.</button>
  <button class="tab" data-view="prenotazioni"><span class="ti">📅</span>Pren.</button>
  <button class="tab" data-view="stats"><span class="ti">📊</span>Stats</button>
  <button class="tab" data-view="analytics"><span class="ti">📈</span>Analyt.</button>
  <button class="tab" data-view="costi"><span class="ti">💰</span>Costi</button>
</nav>

<!-- Modale dettaglio cliente -->
<div class="modal" id="modalCliente" onclick="if(event.target===this)chiudiModale()">
  <div class="modal-box" id="modalClienteBox"></div>
</div>

<script>
const KEY = """ + json.dumps(key) + """;
let DATI = null;

function fmtData(s){if(!s)return '—';return s.substring(0,16).replace('T',' ')}
function badgeCanale(c){return c==='whatsapp'?'📱 WhatsApp':'💬 Telegram'}

function colorByActivity(ultimoIso){
  if(!ultimoIso)return 'red';
  const d=new Date(ultimoIso);
  const days=(Date.now()-d.getTime())/86400000;
  if(days<7)return 'green';
  if(days<30)return 'yellow';
  return 'red';
}

function colorByRank(rank,total){
  // top 33% verde, mid 33% giallo, bottom rosso
  const r=rank/total;
  if(r<=.33)return 'green';
  if(r<=.66)return 'yellow';
  return 'red';
}

function fmtGiorno(s){
  if(!s)return '?';
  const d=new Date(s);
  if(isNaN(d.getTime()))return s.substring(0,10);
  return d.toLocaleDateString('it-IT',{day:'2-digit',month:'2-digit',year:'2-digit'});
}

function renderClienti(){
  const g=document.getElementById('clientiGrid');
  const lst=DATI.top_clienti||[];
  if(!lst.length){g.innerHTML='<div class="empty">Nessun cliente registrato.<br><br>Apparirà qui appena ricevi il primo messaggio.</div>';return}
  // Cambio layout: lista invece di grid
  g.style.gridTemplateColumns='1fr';
  g.innerHTML=lst.map((c,i)=>{
    const rank=i+1;
    const color=colorByActivity(c.ultimo_msg);
    const ic=c.canale==='whatsapp'?'📱':'💬';
    const dal=fmtGiorno(c.primo_msg);
    const al=fmtGiorno(c.ultimo_msg);
    const stesso=dal===al;
    const periodo=stesso?dal:`${dal} → ${al}`;
    const badgeCol={red:'#7a1818',yellow:'#a07208',green:'#16704a'}[color];
    return `<div class="cli-row" onclick='apriCliente(${JSON.stringify(c)})'>
      <div class="cli-rank" style="background:${badgeCol}">#${rank}</div>
      <div class="cli-main">
        <div class="cli-name">${ic} ${escapeHtml(c.nome||'?')}${c.username?' <span style="opacity:.5">@'+escapeHtml(c.username)+'</span>':''}</div>
        <div class="cli-period">📅 ${periodo}</div>
      </div>
      <div class="cli-count">
        <div class="cli-num">${c.totale_msg||0}</div>
        <div class="cli-lbl">msg</div>
      </div>
    </div>`;
  }).join('');
}

function renderArgomenti(){
  const g=document.getElementById('argomentiGrid');
  const arg=DATI.argomenti||{};
  const tot=Object.values(arg).reduce((a,b)=>a+b,0)||1;
  const ordinati=Object.entries(arg).sort((a,b)=>b[1]-a[1]);
  if(!ordinati.length){g.innerHTML='<div class="empty">Nessun argomento ancora tracciato.</div>';return}
  g.innerHTML=ordinati.map(([nome,n],i)=>{
    const rank=i+1;
    const color=colorByRank(rank,ordinati.length);
    const pct=Math.round(n/tot*100);
    const emoji=topicEmoji(nome);
    return `<div class="card ${color}">
      <div class="top">
        <div class="ttl">${emoji} ${escapeHtml(nome)}</div>
        <div class="rank"># ${rank}°</div>
      </div>
      <div>
        <div class="big">${n}</div>
        <div class="sub">messaggi</div>
        <div class="lines">
          <div><b>${pct}%</b> del totale</div>
        </div>
      </div>
    </div>`;
  }).join('');
}

function topicEmoji(t){
  const m={"wifi":"📶","check-in":"🔑","check-out":"🚪","parcheggio":"🚗","spiaggia":"🌊","supermercato":"🛒","ristorante":"🍽️","lavatrice":"🧺","aria condizionata":"❄️","emergenza":"🚨","trasporti":"🚌","altro":"💬"};
  return m[t]||"💬";
}

function listRow(ic,name,meta,num,lbl,onclick){
  return `<div class="list-row" ${onclick?'onclick="'+onclick+'"':''}>
    <div class="L"><span class="ic">${ic}</span><div><div class="name">${escapeHtml(name)}</div><div class="meta">${escapeHtml(meta||'')}</div></div></div>
    <div class="R"><div class="num">${num||''}</div><div class="lbl">${lbl||''}</div></div>
  </div>`;
}

function renderPrenotazioni(){
  const fmt=l=>l.length?l.map(p=>listRow('📅',p.nome,p.lingua,p.checkin+' → '+p.checkout,'')).join(''):'<div class="empty">Nessuna.</div>';
  document.getElementById('prenCorrenti').innerHTML=fmt(DATI.prenotazioni_correnti);
  document.getElementById('prenProssime').innerHTML=fmt(DATI.prenotazioni_prossime);
  document.getElementById('prenPassate').innerHTML=fmt(DATI.prenotazioni_passate);
}

function renderStats(){
  const k=DATI.kpi||{};
  document.getElementById('kpis').innerHTML=[
    ['Totale msg','totale_lifetime'],['Oggi','totale_oggi'],
    ['Clienti','totale_clienti'],['Attivi 7gg','ospiti_attivi']
  ].map(([l,key])=>`<div class="kpi"><div class="v">${k[key]||0}</div><div class="l">${l}</div></div>`).join('');
  // Conversazioni attive
  const cv=DATI.conversazioni_attive||[];
  document.getElementById('conversazioni').innerHTML=cv.length?cv.map(c=>{
    const ic=c.canale==='whatsapp'?'📱':'💬';
    return `<div class="list-row" onclick="window.open('/dashboard/conversation/'+encodeURIComponent('${c.chat_id}')+'?key='+encodeURIComponent(KEY),'_blank')">
      <div class="L"><span class="ic">${ic}</span><div><div class="name">${escapeHtml(c.nome||'?')}</div><div class="meta">Ultimo: ${escapeHtml(c.ultimo_msg)}</div></div></div>
      <div class="R"><div class="num">${c.msg_in_storia}</div><div class="lbl">msg</div></div>
    </div>`;
  }).join(''):'<div class="empty">Nessuna conversazione attiva.</div>';
  // Lingue come mini-card
  const lg=DATI.lingue||{};
  const totL=Object.values(lg).reduce((a,b)=>a+b,0)||1;
  document.getElementById('lingueGrid').innerHTML=Object.entries(lg).sort((a,b)=>b[1]-a[1]).map(([l,n],i)=>{
    const flags={"italian":"🇮🇹","english":"🇬🇧","french":"🇫🇷","spanish":"🇪🇸","german":"🇩🇪","portuguese":"🇵🇹","dutch":"🇳🇱"};
    const color=colorByRank(i+1,Object.keys(lg).length);
    return `<div class="card ${color}" style="min-height:140px"><div class="top"><div class="ttl">${flags[l]||'🌍'} ${escapeHtml(l)}</div></div><div><div class="big">${n}</div><div class="sub">${Math.round(n/totL*100)}% del totale</div></div></div>`;
  }).join('');
  // Media
  const md=DATI.media||[];
  document.getElementById('mediaList').innerHTML=md.length?md.map(m=>{
    const ic=m.tipo==='video'?'🎬':'📸';
    return listRow(ic,m.keywords||'?',m.caption||'','','');
  }).join(''):'<div class="empty">Nessun media salvato.</div>';
}

function escapeHtml(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function apriCliente(c){
  const box=document.getElementById('modalClienteBox');
  const url='/dashboard/conversation/'+encodeURIComponent(c.chat_id)+'?key='+encodeURIComponent(KEY);
  box.innerHTML=`<h3>${escapeHtml(c.nome||'Cliente')}</h3>
    <div class="row"><b>Canale</b><span>${badgeCanale(c.canale)}</span></div>
    <div class="row"><b>Username</b><span>${escapeHtml(c.username||'—')}</span></div>
    <div class="row"><b>Lingua</b><span>${escapeHtml(c.lingua||'—')}</span></div>
    <div class="row"><b>Messaggi totali</b><span>${c.totale_msg||0}</span></div>
    <div class="row"><b>Topic preferito</b><span>${escapeHtml(c.topic_top||'—')}</span></div>
    <div class="row"><b>Primo contatto</b><span>${fmtData(c.primo_msg)}</span></div>
    <div class="row"><b>Ultimo contatto</b><span>${fmtData(c.ultimo_msg)}</span></div>
    <div class="row"><b>Chat ID</b><span style="font-family:monospace;font-size:11px">${escapeHtml(c.chat_id)}</span></div>
    <div class="actions">
      <button onclick="chiudiModale()">Chiudi</button>
      <button class="primary" onclick="window.open('${url}','_blank')">Vedi chat →</button>
    </div>`;
  document.getElementById('modalCliente').classList.add('open');
}
function chiudiModale(){document.getElementById('modalCliente').classList.remove('open')}

document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  t.classList.add('active');
  const v=t.dataset.view;
  document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));
  document.getElementById('view-'+v).classList.add('active');
  window.scrollTo(0,0);
}));

function fmtEur(v){
  if(v==null||isNaN(v))return '—';
  if(v===0)return '0,00 €';
  return v.toLocaleString('it-IT',{style:'currency',currency:'EUR',minimumFractionDigits:2,maximumFractionDigits:4});
}

let ANALYTICS=null;
async function caricaAnalytics(){
  try{
    const r=await fetch('/dashboard/api/analytics?key='+encodeURIComponent(KEY));
    if(!r.ok)return;
    ANALYTICS=await r.json();
    renderAnalytics();
  }catch(e){}
}

function renderAnalytics(){
  if(!ANALYTICS)return;
  const A=ANALYTICS;
  // KPI
  document.getElementById('analyticsKpis').innerHTML=[
    [A.tempo_medio_sec+'s','Tempo medio'],
    [A.tempo_p90+'s','P90 (90% sotto)'],
    [A.tasso_takeover+'%','Takeover'],
    [A.tasso_non_risolti+'%','Non risolti'],
    [A.tasso_vocali+'%','Vocali'],
    [A.totale_eventi_30gg,'Eventi 30gg'],
  ].map(([v,l])=>`<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');
  // Distribuzione tempi
  const dt=A.distrib_tempi||{};
  const totDt=Object.values(dt).reduce((a,b)=>a+b,0)||1;
  document.getElementById('analyticsTempi').innerHTML='<div class="tempi-row">'+
    Object.entries(dt).map(([k,v])=>{
      const pct=Math.round(v/totDt*100);
      return `<div class="tempi-cell"><div class="v">${v}</div><div class="l">${k} (${pct}%)</div></div>`;
    }).join('')+'</div>';
  // Trending
  const tr=A.trending||[];
  if(tr.length){
    document.getElementById('analyticsTrending').innerHTML=tr.map(t=>{
      const arrow=t.delta_pct>10?'📈':(t.delta_pct<-10?'📉':'➡️');
      const cls=t.delta_pct>10?'delta-up':(t.delta_pct<-10?'delta-down':'delta-flat');
      const sign=t.delta_pct>=0?'+':'';
      return `<div class="trending-row">
        <span>${arrow} ${escapeHtml(t.topic)}</span>
        <span>${t.ultimi_7gg} msg <span class="${cls}">${sign}${t.delta_pct}%</span></span>
      </div>`;
    }).join('');
  }else{
    document.getElementById('analyticsTrending').innerHTML='<div class="empty">Dati insufficienti. Servono almeno 14 giorni di attività.</div>';
  }
  // Heatmap
  const hm=A.heatmap||[];
  const giorni=['Lun','Mar','Mer','Gio','Ven','Sab','Dom'];
  let maxVal=0;
  hm.forEach(row=>row.forEach(v=>{if(v>maxVal)maxVal=v}));
  let html='<div class="heatmap"><div class="hcorner"></div>';
  for(let h=0;h<24;h++)html+=`<div class="hour-label">${h}</div>`;
  for(let d=0;d<7;d++){
    html+=`<div class="day-label">${giorni[d]}</div>`;
    for(let h=0;h<24;h++){
      const v=hm[d]?hm[d][h]||0:0;
      const intensity=maxVal>0?v/maxVal:0;
      const color=intensity===0?'rgba(255,255,255,.04)':`rgba(34,197,94,${0.15+intensity*0.85})`;
      html+=`<div class="cell" style="background:${color}" title="${giorni[d]} ${h}:00 — ${v} msg"></div>`;
    }
  }
  html+='</div>';
  document.getElementById('analyticsHeatmap').innerHTML=html;
  // Non risolti
  const nr=A.top_non_risolti||[];
  if(nr.length){
    document.getElementById('analyticsNonRisolti').innerHTML=nr.map(n=>{
      const data=(n.ts||'').substring(0,10);
      return `<div class="nr-row">
        <div class="domanda">"${escapeHtml(n.domanda)}"</div>
        <div class="meta">— ${escapeHtml(n.nome)} (${escapeHtml(n.lingua||'?')}) · ${data}</div>
      </div>`;
    }).join('');
  }else{
    document.getElementById('analyticsNonRisolti').innerHTML='<div class="empty">Nessuna domanda non risolta. Bravo bot! 🎉</div>';
  }
}

let COSTI=null;
async function caricaCosti(){
  try{
    const r=await fetch('/dashboard/api/costi?key='+encodeURIComponent(KEY));
    if(!r.ok)return;
    COSTI=await r.json();
    renderCosti();
  }catch(e){
    document.getElementById('costiList').innerHTML='<div class="empty">Errore caricamento costi: '+escapeHtml(e.message)+'</div>';
  }
}

function renderCosti(){
  if(!COSTI)return;
  // Totale grande
  document.getElementById('costiTotale').innerHTML=
    `<div class="costi-tot"><div class="v">${fmtEur(COSTI.totale_eur||0)}</div><div class="l">Totale stimato — ultimi ${COSTI.periodo_giorni} giorni</div></div>`;
  // Lista servizi (con link diretti per costi reali)
  const m=COSTI.meta||{}, c=COSTI.claude||{};
  const servizi=[
    {ic:'📱', nm:'WhatsApp Cloud API (Meta)',
     nt:m.errore?'⚠️ '+m.errore:`${m.totale_conv||0} conversazioni totali`,
     pr:m.costo_eur||0,
     link:'https://business.facebook.com/wa/manage/insights/?asset_id=1476202720613528',
     linkLbl:'Apri WhatsApp Manager'},
    {ic:'🤖', nm:'Claude API (Anthropic)',
     nt:`~${c.messaggi_recenti||0} msg · ${c.modello||''}`,
     pr:c.costo_recenti_eur||0,
     link:'https://console.anthropic.com/settings/usage',
     linkLbl:'Apri Console Anthropic'},
    {ic:'🆓', nm:'Groq (fallback)', nt:(COSTI.groq||{}).nota||'', pr:0,
     link:'https://console.groq.com/usage', linkLbl:'Apri console Groq'},
    {ic:'💬', nm:'Telegram Bot API', nt:(COSTI.telegram||{}).nota||'', pr:0},
    {ic:'💾', nm:'GitHub (storage dati)', nt:(COSTI.github||{}).nota||'', pr:0,
     link:'https://github.com/settings/billing', linkLbl:'Apri billing GitHub'},
    {ic:'☁️', nm:'Vercel (hosting)', nt:(COSTI.vercel||{}).nota||'', pr:0,
     link:'https://vercel.com/lorenzog2006s-projects/appartamento-bot/usage', linkLbl:'Apri usage Vercel'}
  ];
  document.getElementById('costiList').innerHTML=servizi.map(s=>
    `<div class="cost-row">
      <div class="ic">${s.ic}</div>
      <div class="info">
        <div class="nm">${escapeHtml(s.nm)}</div>
        <div class="nt">${escapeHtml(s.nt)}</div>
        ${s.link?`<a class="cost-link" href="${s.link}" target="_blank" rel="noopener">🔗 ${escapeHtml(s.linkLbl)} →</a>`:''}
      </div>
      <div class="pr ${s.pr===0?'zero':''}">${fmtEur(s.pr)}</div>
    </div>`).join('');
  // Categorie WhatsApp
  const cats=m.conv_per_categoria||{};
  const elemCat=document.getElementById('costiMetaCat');
  if(Object.keys(cats).length){
    const labels={
      "SERVICE":"🆓 Service (cliente scrive primo)",
      "MARKETING":"💸 Marketing (tu inizi promo)",
      "UTILITY":"🔔 Utility (tu confermi/notifichi)",
      "AUTHENTICATION":"🔐 Authentication (OTP)",
      "FREE_ENTRY_POINT":"🆓 Free entry point",
      "FREE_TIER":"🆓 Free tier"
    };
    const free=['SERVICE','FREE_ENTRY_POINT','FREE_TIER'];
    elemCat.innerHTML=Object.entries(cats).map(([k,v])=>{
      const isFree=free.includes(k);
      return `<div class="cat-row"><span>${labels[k]||escapeHtml(k)}</span><span class="${isFree?'free':'paid'}">${v} conv${isFree?' (gratis)':''}</span></div>`;
    }).join('');
  }else{
    elemCat.innerHTML='<div class="empty">Nessuna conversazione negli ultimi 30 giorni.</div>';
  }
  // Note
  document.getElementById('costiNote').innerHTML=
    `<p><b>Stima Claude:</b> ${escapeHtml(c.nota||'')}</p>
     <p style="margin-top:8px"><b>Tariffe Meta Italia 2025:</b> Marketing ~€0,069/conv · Utility ~€0,034/conv · Service gratis illimitato</p>
     <p style="margin-top:8px;font-size:11px;opacity:.7">⚠️ I valori sono stime — Meta fattura mensilmente al CMC effettivo, possono variare di pochi centesimi.</p>`;
}

async function caricaDati(){
  document.getElementById('aggiornata').textContent='⏳ Caricamento...';
  try{
    const r=await fetch('/dashboard/api/data?key='+encodeURIComponent(KEY));
    if(!r.ok){document.getElementById('aggiornata').textContent='❌ Errore '+r.status;return}
    DATI=await r.json();
    renderClienti();renderArgomenti();renderPrenotazioni();renderStats();
    document.getElementById('aggiornata').textContent='✓ Aggiornata: '+DATI.generato_il;
  }catch(e){document.getElementById('aggiornata').textContent='❌ '+e.message}
}
caricaDati();
caricaCosti();
caricaAnalytics();
setInterval(()=>{caricaDati();caricaCosti();caricaAnalytics()},60000);
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")


@app.route("/privacy")
def privacy_policy():
    html = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Privacy Policy — Bot Appartamento Juan les Pins</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; max-width: 720px; margin: 40px auto; padding: 0 20px; color: #222; line-height: 1.6; }
h1 { color: #0066cc; border-bottom: 2px solid #eee; padding-bottom: 10px; }
h2 { margin-top: 30px; color: #333; }
.update { color: #666; font-style: italic; }
</style>
</head>
<body>
<h1>Informativa sulla Privacy</h1>
<p class="update">Ultimo aggiornamento: 9 maggio 2026</p>

<h2>1. Titolare del trattamento</h2>
<p>Lorenzo Guzzi, proprietario dell'appartamento in affitto a Juan les Pins (Antibes, Francia). Contatto: tramite Booking, Airbnb o WhatsApp al numero ufficiale dell'appartamento.</p>

<h2>2. Cosa fa questo bot</h2>
<p>Questo è un assistente virtuale automatico che risponde alle domande degli ospiti dell'appartamento (informazioni su check-in, parcheggio, WiFi, ecc.) tramite Telegram e WhatsApp.</p>

<h2>3. Dati raccolti</h2>
<ul>
<li><strong>Identificativo della chat</strong> (numero di telefono WhatsApp o ID Telegram), per poter rispondere.</li>
<li><strong>Nome pubblico del profilo</strong> (se disponibile), usato solo internamente per identificare l'ospite.</li>
<li><strong>Contenuto dei messaggi</strong> scambiati con il bot, conservati per un massimo di 2 ore per mantenere il contesto della conversazione.</li>
</ul>

<h2>4. Come usiamo i dati</h2>
<p>I dati sono usati esclusivamente per:</p>
<ul>
<li>rispondere automaticamente alle tue domande tramite intelligenza artificiale (Anthropic Claude / Groq);</li>
<li>inoltrare le tue richieste al proprietario in caso il bot non sappia rispondere;</li>
<li>generare risposte coerenti tenendo conto della conversazione recente.</li>
</ul>

<h2>5. Condivisione con terze parti</h2>
<p>I messaggi vengono elaborati da:</p>
<ul>
<li><strong>Anthropic</strong> (Claude API) e <strong>Groq</strong> per generare le risposte AI;</li>
<li><strong>Meta / WhatsApp</strong> (gestisce l'infrastruttura di messaggistica);</li>
<li><strong>Telegram</strong> (gestisce l'infrastruttura del bot Telegram);</li>
<li><strong>Vercel</strong> e <strong>GitHub</strong> per hosting e persistenza temporanea.</li>
</ul>
<p>Nessun dato viene venduto né usato per profilazione pubblicitaria.</p>

<h2>6. Conservazione</h2>
<p>Le conversazioni sono conservate per un massimo di 2 ore dopo l'ultimo messaggio, dopodiché vengono cancellate automaticamente. Le statistiche aggregate anonime (numero di domande per categoria) sono mantenute a scopo di miglioramento del servizio.</p>

<h2>7. I tuoi diritti (GDPR)</h2>
<p>Hai diritto di accesso, rettifica e cancellazione dei tuoi dati. Per esercitarli, contatta direttamente Lorenzo tramite la piattaforma di prenotazione.</p>

<h2>8. Sicurezza</h2>
<p>I dati sono trasmessi tramite HTTPS/TLS. Non vengono mai richieste credenziali di pagamento o dati sensibili: il bot risponde solo a domande sull'appartamento.</p>

<h2>9. Contatti</h2>
<p>Per qualsiasi questione relativa al trattamento dei dati, contatta Lorenzo tramite la chat WhatsApp ufficiale dell'appartamento o tramite Booking/Airbnb.</p>

</body>
</html>"""
    from flask import Response
    return Response(html, mimetype="text/html")


# ── WhatsApp: invia messaggio ─────────────────────────────────────────────────
def wa_invia(to, testo):
    """Invia un messaggio di testo via WhatsApp Cloud API."""
    try:
        url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": testo}
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {WA_TOKEN}"
        })
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        # Notifica Lorenzo dell'errore di invio WhatsApp
        try:
            telegram("sendMessage", {
                "chat_id": OWNER_ID,
                "text": f"⚠️ WA errore invio a {to}:\n{e}\nPHONE_ID={WA_PHONE_ID}\nTOKEN={'ok' if WA_TOKEN else 'MANCANTE'}"
            })
        except Exception:
            pass


def wa_invia_template(to, template_name, nome_ospite, lingua_code="it"):
    """Invia un template WhatsApp approvato da Meta. Ritorna True se 200 OK.
    nome_ospite va nel componente body come variabile named `nome_ospite`."""
    try:
        url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": lingua_code},
                "components": [{
                    "type": "body",
                    "parameters": [{
                        "type": "text",
                        "parameter_name": "nome_ospite",
                        "text": (nome_ospite or "ospite")
                    }]
                }]
            }
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {WA_TOKEN}"
        })
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        try:
            log_errore(f"wa_template_{template_name}", e)
        except Exception:
            pass
        return False


# Cache: telegram_file_id → wa_media_id (validi per molte ore lato Meta)
_wa_media_cache = {}

def _telegram_file_url(file_id):
    """Risolve un Telegram file_id in URL HTTPS scaricabile."""
    r = telegram("getFile", {"file_id": file_id})
    file_path = r.get("result", {}).get("file_path")
    if not file_path:
        return None
    return f"https://api.telegram.org/file/bot{TOKEN}/{file_path}"

def _wa_upload_media(file_id, tipo):
    """Scarica un file da Telegram e lo carica su WhatsApp Cloud API. Ritorna media_id."""
    if file_id in _wa_media_cache:
        return _wa_media_cache[file_id]
    # 1. Scarica da Telegram
    url = _telegram_file_url(file_id)
    if not url:
        return None
    file_data = urllib.request.urlopen(url, timeout=15).read()
    # 2. Determina mime e estensione
    if tipo == "video":
        mime = "video/mp4"
        filename = "video.mp4"
    else:
        # Probiamo a capire dall'URL (ext jpg/png/webp)
        if url.lower().endswith(".png"):
            mime, filename = "image/png", "photo.png"
        elif url.lower().endswith(".webp"):
            mime, filename = "image/webp", "photo.webp"
        else:
            mime, filename = "image/jpeg", "photo.jpg"
    # 3. Multipart upload su Meta
    boundary = "----WAUpload" + str(int(datetime.now().timestamp()))
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="messaging_product"\r\n\r\n'
        f"whatsapp\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="type"\r\n\r\n'
        f"{mime}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + file_data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(
        f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/media",
        data=body,
        headers={
            "Authorization": f"Bearer {WA_TOKEN}",
            "Content-Type": f"multipart/form-data; boundary={boundary}"
        }
    )
    r = urllib.request.urlopen(req, timeout=30)
    media_id = json.loads(r.read()).get("id")
    if media_id:
        _wa_media_cache[file_id] = media_id
    return media_id

def wa_invia_media(to, telegram_file_id, tipo, caption=""):
    """Invia foto/video via WhatsApp partendo da un file_id di Telegram."""
    try:
        media_id = _wa_upload_media(telegram_file_id, tipo)
        if not media_id:
            return False
        wa_type = "video" if tipo == "video" else "image"
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": wa_type,
            wa_type: {"id": media_id}
        }
        if caption:
            payload[wa_type]["caption"] = caption
        url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {WA_TOKEN}"
        })
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        try:
            log_errore("wa_media", e)
        except Exception:
            pass
        return False


# ── WhatsApp webhook ──────────────────────────────────────────────────────────
@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    # ── Verifica webhook (richiesta GET da Meta) ──
    if request.method == "GET":
        mode      = request.args.get("hub.mode")
        token     = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and WA_VERIFY_TOKEN and token == WA_VERIFY_TOKEN:
            return challenge, 200
        return "Unauthorized", 403

    # ── Messaggi in arrivo (POST da Meta) ──
    try:
        body    = request.get_json(force=True)
        entry   = body.get("entry", [])
        if not entry:
            log_info("wa_webhook", "no entry in body")
            return "ok"
        changes = entry[0].get("changes", [])
        if not changes:
            log_info("wa_webhook", "no changes")
            return "ok"
        value    = changes[0].get("value", {})
        messages = value.get("messages", [])
        statuses = value.get("statuses", [])
        if not messages:
            if statuses:
                st = statuses[0]
                log_info("wa_webhook", f"status only: type={st.get('status')} recipient_masked=...{str(st.get('recipient_id',''))[-4:]}")
            else:
                log_info("wa_webhook", f"no messages no statuses, value_keys={list(value.keys())}")
            return "ok"

        msg = messages[0]
        msg_type = msg.get("type")
        wa_from = msg["from"]   # es. "393202599675" (senza +)
        contacts = value.get("contacts", [])
        nome = contacts[0]["profile"]["name"] if contacts else "Ospite"
        log_info("wa_webhook", f"msg_in type={msg_type} from_masked=...{wa_from[-4:]} nome={nome}")

        # Audio/voice in arrivo → trascrivi e tratta come testo
        era_vocale = False
        if msg_type == "audio":
            audio_obj = msg.get("audio", {})
            media_id = audio_obj.get("id")
            mime_hint = audio_obj.get("mime_type", "audio/ogg")
            if media_id:
                # Feedback all'ospite (in italiano: il bot rileva la lingua dopo dalla trascrizione)
                wa_invia(wa_from, "🎙️ Sto ascoltando il messaggio vocale...")
                audio_data, mime = scarica_wa_media(media_id)
                trascritto = trascrivi_audio_groq(audio_data, mime or mime_hint) if audio_data else None
                if trascritto:
                    testo = trascritto
                    era_vocale = True
                else:
                    wa_invia(wa_from, "Mi dispiace, non sono riuscito a capire l'audio 🙏 Puoi scrivermi a testo?")
                    return "ok"
            else:
                wa_invia(wa_from, "Mi dispiace, non sono riuscito a ricevere l'audio 🙏")
                return "ok"
        elif msg_type != "text":
            # Altri tipi (image, video, document, sticker, location...) non gestiti per ora
            wa_invia(wa_from, "Ciao! 😊 Al momento gestisco solo testo e messaggi vocali. Scrivi o registra pure la tua domanda!")
            return "ok"
        else:
            testo = msg["text"]["body"]

        # ── Intercept signora pulizie ──
        # Se il mittente è il numero della signora delle pulizie, trattiamo come
        # conferma del turno aperto (qualsiasi testo conta come "ok"). Se non c'è
        # turno aperto, inoltriamo a Lorenzo come messaggio normale.
        if WA_PULIZIE and wa_from == WA_PULIZIE:
            tid, turno = pulizie_trova_ultimo_aperto()
            if tid:
                pulizie_mark_confermato(tid, testo)
                if OWNER_ID:
                    try:
                        invia_messaggio(int(OWNER_ID),
                            f"✅ *{NOME_PULIZIE} ha confermato* il turno del {turno.get('checkout','?')}\n\n"
                            f"💬 _Risposta:_ {testo}",
                            parse_mode="Markdown"
                        )
                    except Exception:
                        pass
                try:
                    wa_invia(wa_from, "👍 Grazie, ricevuto!")
                except Exception:
                    pass
                return "ok"
            # Nessun turno aperto: inoltra a Lorenzo come notifica
            if OWNER_ID:
                try:
                    invia_messaggio(int(OWNER_ID),
                        f"📩 *{NOME_PULIZIE}* (nessun turno aperto):\n\n💬 {testo}",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            return "ok"

        # Chiave sessione WhatsApp separata da Telegram
        wa_session_id = f"wa_{wa_from}"

        # Ospite tornato: saluto speciale prima della risposta
        if is_ospite_tornato(wa_session_id):
            try:
                lingua_t = rileva_lingua(testo)
                msg_bt = BENTORNATO.get(lingua_t, BENTORNATO["english"]).format(nome=nome)
                wa_invia(wa_from, msg_bt)
                marca_bentornato(wa_session_id)
            except Exception:
                pass

        # Pausa AI: notifica Lorenzo e basta, non rispondere
        if is_paused(wa_session_id):
            if OWNER_ID:
                try:
                    _voce_pre = "🎙️ _Vocale trascritto:_ " if era_vocale else ""
                    invia_bottoni(int(OWNER_ID),
                        f"⏸️ *[PAUSA WA]* {nome}\n\n❓ {_domanda_per_owner(_voce_pre, testo)}\n\n[WA:{wa_from}]",
                        [[{"text": "▶️ Riattiva AI", "callback_data": f"RIPRENDI:{wa_session_id}"}]],
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
            try:
                aggiorna_user(wa_session_id, "whatsapp", nome, testo, rileva_lingua(testo), None)
            except Exception:
                pass
            return "ok"

        # Primo messaggio → invia benvenuto prima della risposta AI
        storia_wa = get_storia(wa_session_id)
        if not storia_wa:
            try:
                lingua    = rileva_lingua(testo)
                benvenuto = genera_benvenuto(lingua, leggi_info())
                wa_invia(wa_from, benvenuto)
            except Exception:
                wa_invia(wa_from, "Benvenuto! 😊 Sono l'assistente virtuale dell'appartamento. Come posso aiutarti?")

        # Risposta AI (riusa tutta la logica esistente)
        t_start_wa = datetime.now().timestamp()
        info  = leggi_info()
        reply = chiedi_ai(testo, info, chat_id=wa_session_id)
        aggiorna_storia(wa_session_id, testo, reply)
        # Aggiorna anagrafica utente (per dashboard) + analytics
        try:
            lingua_stat = rileva_lingua(testo)
            aggiorna_user(wa_session_id, "whatsapp", nome, testo, lingua_stat, None)
            durata_sec_wa = datetime.now().timestamp() - t_start_wa
            non_risolto_wa = bot_non_sa(reply)
            log_evento_analytics("whatsapp", _topic_di(testo), durata_sec_wa,
                                 takeover=False, non_risolto=non_risolto_wa,
                                 era_vocale=era_vocale)
            if non_risolto_wa:
                log_msg_non_risolto(testo, wa_session_id, lingua_stat)
        except Exception:
            pass

        # Invia risposta all'ospite su WhatsApp
        wa_invia(wa_from, reply)

        # Media automatici (foto/video) — stessa logica di Telegram
        try:
            media = trova_media(testo)
            if media:
                wa_invia_media(wa_from, media["file_id"], media["tipo"], media.get("caption", ""))
        except Exception as e:
            try:
                log_errore("wa_media_trigger", e)
            except Exception:
                pass

        # Notifica Lorenzo su Telegram (con [WA:...] per permettere reply diretto)
        _voce_pre_md = "🎙️ _Vocale trascritto:_ " if era_vocale else ""
        _voce_pre = "🎙️ Vocale trascritto: " if era_vocale else ""
        if OWNER_ID:
            try:
                wa_arrabbiato = rileva_sentiment_negativo(testo)
                if wa_arrabbiato:
                    invia_bottoni(int(OWNER_ID),
                        f"🚨 *ATTENZIONE — OSPITE WHATSAPP ARRABBIATO*\n\n"
                        f"Ospite: {nome}\n\n"
                        f"❓ {_domanda_per_owner(_voce_pre_md, testo, italic=True)}\n\n"
                        f"🤖 {reply}\n\n"
                        f"[WA:{wa_from}]",
                        [[
                            {"text": "💬 Rispondi qui", "callback_data": f"REPLY:WA:{wa_from}"},
                            {"text": "⏸️ Pausa AI", "callback_data": f"PAUSA:{wa_session_id}"}
                        ]],
                        parse_mode="Markdown"
                    )
                else:
                    notifica_owner_aggregata(wa_session_id,
                        f"📱 *WhatsApp* — {nome}\n\n❓ {_domanda_per_owner(_voce_pre_md, testo)}\n\n🤖 {reply}\n\n[WA:{wa_from}]",
                        [[
                            {"text": "💬 Rispondi qui", "callback_data": f"REPLY:WA:{wa_from}"},
                            {"text": "💬 Prendi chat", "callback_data": f"TAKEOVER:{wa_session_id}"},
                            {"text": "⏸️ Pausa", "callback_data": f"PAUSA:{wa_session_id}"}
                        ]],
                        parse_mode="Markdown"
                    )
            except Exception:
                pass

        # Emergenza?
        PAROLE_EMERGENZA_WA = [
            "allagamento","perdita acqua","tubo rotto","guasto luce","senza corrente",
            "blackout","corto circuito","gas","odore gas","flood","water leak",
            "no electricity","power cut","gas leak","inondation","fuite d'eau",
            "panne électrique","fuga de agua","sin electricidad","stromausfall","gasgeruch"
        ]
        if OWNER_ID and any(p in testo.lower() for p in PAROLE_EMERGENZA_WA):
            try:
                invia_messaggio(int(OWNER_ID),
                    f"🚨🚨 EMERGENZA WHATSAPP 🚨🚨\n\n"
                    f"Ospite WhatsApp: {nome} (+{wa_from})\n\n"
                    f"❓ {_domanda_per_owner(_voce_pre, testo)}\n\n🤖 {reply}\n\n[WA:{wa_from}]"
                )
            except Exception:
                pass

        # Stats
        try:
            lingua_stat = rileva_lingua(testo)
            aggiorna_stats(testo, lingua_stat)
            aggiorna_daily_stats(testo, lingua_stat, wa_session_id)
        except Exception as e:
            log_warn("wa_webhook", f"stats err: {type(e).__name__}: {str(e)[:200]}")

    except Exception as e:
        import traceback as _tb
        log_errore("wa_webhook_outer", f"{type(e).__name__}: {str(e)[:300]}")
        log_warn("wa_webhook_outer", _tb.format_exc()[:1500])

    return "ok"


# ════════════════════════════════════════════════════════════════════════════════
# CHANNEL MANAGER — Modulo isolato per sincronizzazione iCal Airbnb + Booking
#
# Questo modulo è completamente indipendente dal flusso messaggi-ospiti:
# - Usa file propri: calendar_events.json, calendar_wizard_state.json
# - Non scrive su bookings.json (gestito dal wizard /prenotazione)
# - Non manda messaggi automatici agli ospiti (solo notifiche al proprietario)
# - Cancellando questo blocco, il bot ospiti continua a funzionare uguale.
#
# Vedi PIANO.md per l'architettura completa.
# ════════════════════════════════════════════════════════════════════════════════

AIRBNB_ICAL_URL  = (os.environ.get("AIRBNB_ICAL_URL")  or "").strip()
BOOKING_ICAL_URL = (os.environ.get("BOOKING_ICAL_URL") or "").strip()

CAL_EVENTS_API = f"https://api.github.com/repos/{REPO}/contents/calendar_events.json"
CAL_WIZARD_API = f"https://api.github.com/repos/{REPO}/contents/calendar_wizard_state.json"


def _cal_load_events():
    """Ritorna (dict_eventi, sha) da calendar_events.json su GitHub. ({}, None) se assente."""
    if not GITHUB_TOKEN:
        return ({}, None)
    try:
        url = f"{CAL_EVENTS_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=5)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        eventi = json.loads(contenuto) if contenuto.strip() else {}
        return (eventi, data["sha"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ({}, None)
        return ({}, None)
    except Exception:
        return ({}, None)


def _cal_save_events(eventi, sha):
    """PUT calendar_events.json. Retry una volta su conflitto SHA."""
    if not GITHUB_TOKEN:
        return False
    for _attempt in range(2):
        try:
            payload = {
                "message": "Channel manager: aggiorna eventi calendario",
                "content": base64.b64encode(json.dumps(eventi, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8"),
            }
            if sha:
                payload["sha"] = sha
            req = urllib.request.Request(CAL_EVENTS_API, data=json.dumps(payload).encode(), headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "appartamento-bot"
            }, method="PUT")
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (409, 422):
                _, sha = _cal_load_events()
                continue
            return False
        except Exception:
            return False
    return False


def _cal_load_wizard_state():
    """Ritorna (dict_stato, sha). Lo stato ha forma {'pending_completions': [chiave, ...]}."""
    if not GITHUB_TOKEN:
        return ({"pending_completions": []}, None)
    try:
        url = f"{CAL_WIZARD_API}?t={int(datetime.now().timestamp())}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "appartamento-bot"
        })
        r = urllib.request.urlopen(req, timeout=4)
        data = json.loads(r.read())
        contenuto = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")
        stato = json.loads(contenuto) if contenuto.strip() else {}
        if "pending_completions" not in stato or not isinstance(stato["pending_completions"], list):
            stato["pending_completions"] = []
        return (stato, data["sha"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ({"pending_completions": []}, None)
        return ({"pending_completions": []}, None)
    except Exception:
        return ({"pending_completions": []}, None)


def _cal_save_wizard_state(stato, sha):
    """PUT calendar_wizard_state.json con retry su conflitto."""
    if not GITHUB_TOKEN:
        return False
    for _attempt in range(2):
        try:
            payload = {
                "message": "Channel manager: aggiorna wizard state",
                "content": base64.b64encode(json.dumps(stato, ensure_ascii=False, indent=2).encode("utf-8")).decode("utf-8"),
            }
            if sha:
                payload["sha"] = sha
            req = urllib.request.Request(CAL_WIZARD_API, data=json.dumps(payload).encode(), headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "appartamento-bot"
            }, method="PUT")
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            if e.code in (409, 422):
                _, sha = _cal_load_wizard_state()
                continue
            return False
        except Exception:
            return False
    return False


def cal_has_pending():
    """True se c'è almeno uno stub calendario in attesa di completamento."""
    try:
        stato, _ = _cal_load_wizard_state()
        return bool(stato.get("pending_completions"))
    except Exception:
        return False


def _cal_parse_ical(testo, channel):
    """Parsa un feed iCalendar e ritorna lista di eventi normalizzati.
    Non usa librerie esterne: estrae blocchi VEVENT con regex.

    Filtri specifici per canale:
    - Airbnb: solo SUMMARY="Reserved" (i blocchi manuali appaiono come "Not available"
      o "Airbnb (Not available)" e vanno ignorati).
    - Booking: tutti gli eventi (il loro iCal mostra sempre "CLOSED - Not available"
      sia per prenotazioni sia per blocchi manuali, non c'è modo di distinguerli).
    """
    eventi = []
    blocchi = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", testo, re.DOTALL)
    for blocco in blocchi:
        # Unfold delle righe continue (RFC 5545: una riga che inizia con spazio/tab
        # è la continuazione della precedente)
        unfolded = re.sub(r"\r?\n[ \t]", "", blocco)
        def _campo(nome):
            m = re.search(rf"^{nome}(?:;[^:]*)?:(.*?)$", unfolded, re.MULTILINE)
            return m.group(1).strip() if m else None
        uid = _campo("UID")
        dtstart = _campo("DTSTART")
        dtend = _campo("DTEND")
        summary = _campo("SUMMARY") or ""
        if not (uid and dtstart and dtend):
            continue
        # Filtro Airbnb: scarta i blocchi manuali. Le prenotazioni vere hanno
        # SUMMARY esatto "Reserved"; i blocchi hanno "Not available" o varianti.
        if channel == "airbnb":
            sl = summary.lower()
            if "not available" in sl or "blocked" in sl:
                continue
        # DTSTART/DTEND formato YYYYMMDD (date-only) — Airbnb e Booking usano questo
        m_in = re.match(r"(\d{4})(\d{2})(\d{2})", dtstart)
        m_out = re.match(r"(\d{4})(\d{2})(\d{2})", dtend)
        if not (m_in and m_out):
            continue
        checkin = f"{m_in.group(3)}/{m_in.group(2)}/{m_in.group(1)}"
        checkout = f"{m_out.group(3)}/{m_out.group(2)}/{m_out.group(1)}"
        # Codice prenotazione: Airbnb mette nel SUMMARY o nella DESCRIPTION
        # un URL tipo /details/HMABCD1234. In fallback usiamo i primi 10 char di UID.
        code = None
        m_code = re.search(r"/details/([A-Z0-9]+)", unfolded)
        if m_code:
            code = m_code.group(1)
        else:
            m_code = re.search(r"\b([A-Z0-9]{8,12})\b", summary)
            if m_code:
                code = m_code.group(1)
        if not code:
            code = uid.split("@")[0][:12] if "@" in uid else uid[:12]
        eventi.append({
            "uid": uid,
            "checkin": checkin,
            "checkout": checkout,
            "summary": summary,
            "code": code,
            "channel": channel,
        })
    return eventi


def cal_fetch_ical_events(url, channel):
    """Scarica un feed iCal pubblico e ritorna la lista di eventi parsati."""
    if not url:
        return []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "appartamento-bot"})
        r = urllib.request.urlopen(req, timeout=10)
        body = r.read().decode("utf-8", errors="ignore")
        return _cal_parse_ical(body, channel)
    except Exception as e:
        try:
            log_errore(f"cal_fetch_{channel}", e)
        except Exception:
            pass
        return []


def _cal_count_notti(checkin, checkout):
    """Calcola numero notti tra due date DD/MM/YYYY. 0 se errore."""
    try:
        ci = datetime.strptime(checkin, "%d/%m/%Y")
        co = datetime.strptime(checkout, "%d/%m/%Y")
        return max(0, (co - ci).days)
    except Exception:
        return 0


def _cal_find_overlap_match(eventi_esistenti, channel, checkin, checkout):
    """Cerca un evento esistente dello stesso canale che si sovrapponga in modo
    sostanziale con (checkin, checkout). Serve a evitare falsi positivi quando
    Booking rigenera UID per blocchi auto-rolling (il "passato" viene tagliato
    o l'intero range slitta di 1 giorno → UID nuovo per stesso evento).

    Ritorna la chiave dell'evento esistente match, o None se è davvero nuovo.
    """
    try:
        c_in = datetime.strptime(checkin, "%d/%m/%Y")
        c_out = datetime.strptime(checkout, "%d/%m/%Y")
    except Exception:
        return None
    nuovo_durata = max(1, (c_out - c_in).days)
    for k, v in eventi_esistenti.items():
        if v.get("canale") != channel:
            continue
        try:
            e_in = datetime.strptime(v["checkin"], "%d/%m/%Y")
            e_out = datetime.strptime(v["checkout"], "%d/%m/%Y")
        except Exception:
            continue
        # Calcola sovrapposizione (in giorni)
        overlap_start = max(c_in, e_in)
        overlap_end = min(c_out, e_out)
        if overlap_start >= overlap_end:
            continue
        shared = (overlap_end - overlap_start).days
        esistente_durata = max(1, (e_out - e_in).days)
        # Se condividono >=50% di uno dei due, è lo stesso evento.
        # 50% è abbastanza permissivo da catturare gli auto-roll (90%+) ma evita
        # di unire due brevi soggiorni adiacenti diversi.
        if shared / esistente_durata >= 0.5 or shared / nuovo_durata >= 0.5:
            return k
    return None


def cal_extract_details_from_freetext(testo):
    """Usa Groq per estrarre {nome, num_ospiti, prezzo_eur} da una risposta libera del proprietario.
    Tollera formati diversi: 'Mario Rossi / 3 / 720', 'Mario 3 ospiti 720€', ecc.
    Ritorna dict con i tre campi o None se l'estrazione fallisce."""
    if not GROQ_KEY:
        return None
    prompt = (
        "Estrai SOLO un oggetto JSON con i campi nome (string), num_ospiti (int), "
        "prezzo_eur (number). Niente testo prima o dopo, solo JSON. "
        "Se un campo non è presente nel testo, ometti la chiave.\n\n"
        f"Testo: {testo.strip()}"
    )
    try:
        risposta = _chiama_groq(
            "llama-3.1-8b-instant",
            [
                {"role": "system", "content": "Sei un parser. Output solo JSON valido."},
                {"role": "user", "content": prompt},
            ],
            timeout=8,
        )
        cleaned = re.sub(r"^```(?:json)?|```$", "", risposta.strip(), flags=re.MULTILINE).strip()
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return None
        dati = json.loads(m.group(0))
        nome = (dati.get("nome") or "").strip()
        try:
            num_ospiti = int(dati.get("num_ospiti") or 0)
        except Exception:
            num_ospiti = 0
        try:
            prezzo_eur = float(str(dati.get("prezzo_eur") or 0).replace(",", "."))
        except Exception:
            prezzo_eur = 0.0
        if not nome and num_ospiti == 0 and prezzo_eur == 0:
            return None
        return {"nome": nome, "num_ospiti": num_ospiti, "prezzo_eur": prezzo_eur}
    except Exception as e:
        try:
            log_errore("cal_extract", e)
        except Exception:
            pass
        return None


def cal_complete_oldest_stub(testo_risposta):
    """Completa il primo stub in coda con i dati estratti da testo_risposta.
    Ritorna (stringa_conferma, key_evento_completato) — key è None se errore."""
    stato, sha_w = _cal_load_wizard_state()
    pending = stato.get("pending_completions") or []
    if not pending:
        return ("⚠️ Nessuno stub calendario in attesa.", None)
    eventi, sha_e = _cal_load_events()
    key = pending[0]
    ev = eventi.get(key)
    if not ev:
        stato["pending_completions"] = pending[1:]
        _cal_save_wizard_state(stato, sha_w)
        return ("⚠️ Stub non trovato (forse cancellato). Coda ripulita.", None)
    dati = cal_extract_details_from_freetext(testo_risposta)
    if not dati:
        return ((
            "❌ Non sono riuscito a interpretare la risposta.\n\n"
            "Riprova con un formato tipo: `Mario Rossi / 3 / 720`\n"
            "oppure: `nome Mario Rossi, 3 ospiti, 720 euro`"
        ), None)
    ev["nome"] = dati["nome"] or ev.get("nome", "")
    ev["num_ospiti"] = dati["num_ospiti"] or ev.get("num_ospiti", 0)
    ev["prezzo_eur"] = dati["prezzo_eur"] or ev.get("prezzo_eur", 0.0)
    ev["stato"] = "complete"
    ev["completed_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    eventi[key] = ev
    ok_ev = _cal_save_events(eventi, sha_e)
    stato["pending_completions"] = pending[1:]
    ok_w = _cal_save_wizard_state(stato, sha_w)
    if not (ok_ev and ok_w):
        return ("⚠️ Errore nel salvataggio su GitHub. Riprova fra qualche secondo.", None)
    notti = _cal_count_notti(ev["checkin"], ev["checkout"])
    riga_rim = ""
    if stato["pending_completions"]:
        riga_rim = f"\n\nℹ️ Restano {len(stato['pending_completions'])} prenotazion{'e' if len(stato['pending_completions']) == 1 else 'i'} da completare."
    msg = (
        f"✅ *Evento calendario salvato*\n\n"
        f"🏷️ Canale: {ev.get('canale','?').title()}\n"
        f"👤 {ev['nome']} ({ev['num_ospiti']} ospit{'e' if ev['num_ospiti']==1 else 'i'})\n"
        f"📅 {ev['checkin']} → {ev['checkout']} ({notti} nott{'e' if notti==1 else 'i'})\n"
        f"💶 {ev['prezzo_eur']:.0f} €\n"
        f"🔖 Cod. {ev.get('code','?')}"
        f"{riga_rim}"
    )
    return (msg, key)


def cal_set_culla(key, culla):
    """Imposta il flag culla su un evento calendario. Ritorna True se salvato."""
    eventi, sha_e = _cal_load_events()
    if key not in eventi:
        return False
    eventi[key]["culla"] = bool(culla)
    return _cal_save_events(eventi, sha_e)


def cal_format_pending_list():
    """Ritorna un messaggio markdown con la lista degli stub pending."""
    stato, _ = _cal_load_wizard_state()
    pending = stato.get("pending_completions") or []
    if not pending:
        return "📭 Nessuna prenotazione in attesa di completamento.\n\nQuando arriva una prenotazione su Airbnb o Booking, ti scrivo qui."
    eventi, _ = _cal_load_events()
    righe = ["📋 *Prenotazioni in attesa di dettagli*\n"]
    for i, key in enumerate(pending, 1):
        ev = eventi.get(key)
        if not ev:
            continue
        righe.append(
            f"{i}. *{ev.get('canale','?').title()}* — {ev['checkin']} → {ev['checkout']}\n"
            f"   🔖 Cod. {ev.get('code','?')}"
        )
    righe.append("\nRispondi con i dati della *prima* prenotazione nel formato:\n`nome / ospiti / prezzo`\nEs: `Mario Rossi / 3 / 720`")
    return "\n".join(righe)


def cal_format_full_list():
    """Ritorna un riepilogo markdown di tutte le prenotazioni dal canale (oggi in poi),
    ordinate per data di check-in. Mostra anche quelle pending o seeded."""
    eventi, _ = _cal_load_events()
    if not eventi:
        return "📭 Nessuna prenotazione dai canali (Airbnb/Booking).\n\nUsa /calsync per forzare un import."
    oggi = datetime.now().date()
    righe = []
    for key, ev in eventi.items():
        try:
            ci = datetime.strptime(ev["checkin"], "%d/%m/%Y").date()
            co = datetime.strptime(ev["checkout"], "%d/%m/%Y").date()
        except Exception:
            continue
        if co < oggi:
            continue  # già passate
        righe.append((ci, co, ev))
    righe.sort(key=lambda t: t[0])
    if not righe:
        return "📭 Nessuna prenotazione futura sui canali."
    out = ["📅 *Prenotazioni canali — dal", oggi.strftime("%d/%m/%Y"), "in poi*\n"]
    out = [f"📅 *Prenotazioni canali — dal {oggi.strftime('%d/%m/%Y')} in poi*\n"]
    for ci, co, ev in righe:
        canale = (ev.get("canale") or "?").title()
        stato_ev = ev.get("stato", "")
        notti = (co - ci).days
        if stato_ev == "complete":
            icon = "🟢"
            corpo = (
                f"{icon} *{canale}* — {ev['checkin']} → {ev['checkout']} ({notti} nott{'e' if notti==1 else 'i'})\n"
                f"   👤 {ev.get('nome','?')} • {ev.get('num_ospiti',0)} ospit{'e' if ev.get('num_ospiti',0)==1 else 'i'} • {ev.get('prezzo_eur',0):.0f} €"
            )
        elif stato_ev == "seeded":
            icon = "⚫"
            corpo = f"{icon} *{canale}* — {ev['checkin']} → {ev['checkout']} ({notti} nott{'e' if notti==1 else 'i'}) — _preesistente_"
        else:
            icon = "🟡"
            corpo = (
                f"{icon} *{canale}* — {ev['checkin']} → {ev['checkout']} ({notti} nott{'e' if notti==1 else 'i'})\n"
                f"   _da completare — cod. {ev.get('code','?')}_"
            )
        out.append(corpo)
    out.append("\n_Legenda: 🟢 completa • 🟡 da completare (/cal per aggiungere dati) • ⚫ preesistente_")
    return "\n\n".join(out)


def _cal_notify_owner_new_stub(ev):
    """Notifica il proprietario di una nuova prenotazione rilevata."""
    if not OWNER_ID:
        return
    notti = _cal_count_notti(ev["checkin"], ev["checkout"])
    msg = (
        f"🆕 *Nuova prenotazione {ev.get('channel','?').title()}*\n\n"
        f"📅 {ev['checkin']} → {ev['checkout']} ({notti} nott{'e' if notti==1 else 'i'})\n"
        f"🔖 Cod. {ev.get('code','?')}\n\n"
        f"Rispondimi con: `nome / ospiti / prezzo`\n"
        f"Es: `Mario Rossi / 3 / 720`"
    )
    try:
        invia_messaggio(int(OWNER_ID), msg, parse_mode="Markdown")
    except Exception:
        pass


@app.route("/cron/sync-ical", methods=["GET", "POST"])
def cron_sync_ical():
    """Polling dei feed iCal Airbnb/Booking. Per ogni nuovo UID crea uno stub
    in calendar_events.json e mette il proprietario in attesa di completamento.
    Idempotente: UID già visti vengono ignorati.

    Primo sync (seed): se calendar_events.json è vuoto, gli eventi esistenti
    vengono salvati con stato 'seeded' SENZA mandare notifica Telegram né
    aggiungerli alla coda di completamento. Solo i nuovi eventi visti dopo
    il seed iniziale generano notifiche."""
    risultato = {"airbnb": {"fetched": 0, "new": 0, "seeded": 0}, "booking": {"fetched": 0, "new": 0, "seeded": 0}}
    try:
        eventi_esistenti, sha_e = _cal_load_events()
        stato, sha_w = _cal_load_wizard_state()
        pending = stato.get("pending_completions") or []
        is_first_sync = not eventi_esistenti
        modificato = False
        for url, channel in ((AIRBNB_ICAL_URL, "airbnb"), (BOOKING_ICAL_URL, "booking")):
            if not url:
                continue
            lista = cal_fetch_ical_events(url, channel)
            risultato[channel]["fetched"] = len(lista)
            for ev in lista:
                # Chiave canonica: <canale>_<code>. Garantisce idempotenza.
                key = f"{channel}_{ev['code']}"
                if key in eventi_esistenti:
                    continue
                # Dedup secondario per sovrapposizione date (gestisce auto-roll
                # di Booking che cambia UID per blocchi che includono "oggi").
                match_key = _cal_find_overlap_match(eventi_esistenti, channel, ev["checkin"], ev["checkout"])
                if match_key:
                    # Aggiorna le date dell'evento esistente per matchare il feed.
                    # Non rigenera notifica, non aggiunge a pending: è solo un update.
                    eventi_esistenti[match_key]["checkin"] = ev["checkin"]
                    eventi_esistenti[match_key]["checkout"] = ev["checkout"]
                    eventi_esistenti[match_key]["ical_uid"] = ev["uid"]
                    modificato = True
                    continue
                if is_first_sync:
                    # Seed silenzioso: salva ma non notifica e non chiede dettagli.
                    eventi_esistenti[key] = {
                        "canale": channel,
                        "code": ev["code"],
                        "checkin": ev["checkin"],
                        "checkout": ev["checkout"],
                        "stato": "seeded",
                        "nome": "",
                        "num_ospiti": 0,
                        "prezzo_eur": 0.0,
                        "ical_uid": ev["uid"],
                        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    risultato[channel]["seeded"] += 1
                else:
                    eventi_esistenti[key] = {
                        "canale": channel,
                        "code": ev["code"],
                        "checkin": ev["checkin"],
                        "checkout": ev["checkout"],
                        "stato": "pending_details",
                        "nome": "",
                        "num_ospiti": 0,
                        "prezzo_eur": 0.0,
                        "ical_uid": ev["uid"],
                        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                    if key not in pending:
                        pending.append(key)
                    risultato[channel]["new"] += 1
                    _cal_notify_owner_new_stub({
                        "channel": channel, "code": ev["code"],
                        "checkin": ev["checkin"], "checkout": ev["checkout"],
                    })
                modificato = True
        if modificato:
            _cal_save_events(eventi_esistenti, sha_e)
            stato["pending_completions"] = pending
            _cal_save_wizard_state(stato, sha_w)
            # Notifica una volta al proprietario il completamento del seed iniziale
            if is_first_sync and OWNER_ID:
                try:
                    tot_seed = risultato["airbnb"]["seeded"] + risultato["booking"]["seeded"]
                    invia_messaggio(int(OWNER_ID),
                        f"✅ Channel manager attivato.\n\n"
                        f"Ho importato silenziosamente {tot_seed} eventi già presenti sui tuoi calendari "
                        f"(Airbnb: {risultato['airbnb']['seeded']}, Booking: {risultato['booking']['seeded']}).\n\n"
                        f"D'ora in poi ti avviso solo quando arriva una *nuova* prenotazione.",
                        parse_mode="Markdown"
                    )
                except Exception:
                    pass
    except Exception as e:
        try:
            log_errore("cron_sync_ical", e)
        except Exception:
            pass
        return (json.dumps({"error": str(e)}), 500, {"Content-Type": "application/json"})
    return json.dumps(risultato)


def _cal_ics_escape(testo):
    """Escape minimo dei caratteri speciali iCalendar (RFC 5545 §3.3.11)."""
    if not testo:
        return ""
    return (testo.replace("\\", "\\\\")
                 .replace(";", "\\;")
                 .replace(",", "\\,")
                 .replace("\n", "\\n"))


@app.route("/calendar.ics", methods=["GET"])
def calendar_ics():
    """Feed iCalendar pubblico con tutti gli eventi di calendar_events.json.
    Sottoscrivibile da Google Calendar / Apple Calendar."""
    eventi, _ = _cal_load_events()
    righe = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Appartamento Juan les Pins//Channel Manager//IT",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Prenotazioni Juan les Pins",
        "X-WR-TIMEZONE:Europe/Paris",
    ]
    now_stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    for key, ev in (eventi or {}).items():
        try:
            ci = datetime.strptime(ev["checkin"], "%d/%m/%Y").strftime("%Y%m%d")
            co = datetime.strptime(ev["checkout"], "%d/%m/%Y").strftime("%Y%m%d")
        except Exception:
            continue
        canale = (ev.get("canale") or "?").title()
        nome = ev.get("nome") or ""
        num_ospiti = ev.get("num_ospiti") or 0
        prezzo = ev.get("prezzo_eur") or 0.0
        stato_ev = ev.get("stato")
        if stato_ev == "complete" and nome:
            summary = f"[{canale}] {nome} — {num_ospiti} ospiti — {prezzo:.0f}€"
            placeholder = ""
        elif stato_ev == "seeded":
            # Eventi importati dal primo sync (preesistenti): segnati come occupato
            # senza chiedere completamento (potrebbero essere blocchi manuali).
            summary = f"[{canale}] Occupato"
            placeholder = "(occupato — preesistente)"
        else:
            summary = f"[{canale}] Da completare — cod. {ev.get('code','?')}"
            placeholder = "(da completare)"
        desc_parts = [
            f"Canale: {canale}",
            f"Codice: {ev.get('code','?')}",
            f"Nome: {nome or placeholder}",
            f"Ospiti: {num_ospiti or placeholder}",
            f"Prezzo: {prezzo:.0f} €" if prezzo else f"Prezzo: {placeholder}",
        ]
        description = _cal_ics_escape("\n".join(desc_parts))
        uid = ev.get("ical_uid") or f"{key}@appartamento-bot.vercel.app"
        righe.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"DTSTART;VALUE=DATE:{ci}",
            f"DTEND;VALUE=DATE:{co}",
            f"SUMMARY:{_cal_ics_escape(summary)}",
            f"DESCRIPTION:{description}",
            "END:VEVENT",
        ])
    righe.append("END:VCALENDAR")
    body = "\r\n".join(righe) + "\r\n"
    return (body, 200, {"Content-Type": "text/calendar; charset=utf-8"})

# ════════════════════════════════════════════════════════════════════════════════
# Fine modulo CHANNEL MANAGER
# ════════════════════════════════════════════════════════════════════════════════
