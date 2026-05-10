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
WA_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "juanlespins2026").strip()

REPO         = "Lorenzog2006/appartamento-bot"
GITHUB_RAW   = f"https://raw.githubusercontent.com/{REPO}/main/appartamento.txt"
GITHUB_API   = f"https://api.github.com/repos/{REPO}/contents/appartamento.txt"
STATS_API       = f"https://api.github.com/repos/{REPO}/contents/stats.json"
DAILY_STATS_API = f"https://api.github.com/repos/{REPO}/contents/daily_stats.json"
BOOKINGS_API    = f"https://api.github.com/repos/{REPO}/contents/bookings.json"
CONVERSATIONS_API = f"https://api.github.com/repos/{REPO}/contents/conversations.json"
USERS_API       = f"https://api.github.com/repos/{REPO}/contents/users.json"
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

def _topic_di(testo):
    """Determina la categoria di una domanda dalle TOPIC_KEYWORDS."""
    t = (testo or "").lower()
    for topic, kws in TOPIC_KEYWORDS.items():
        if any(k in t for k in kws):
            return topic
    return "altro"

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

def log_errore(contesto, errore):
    """Notifica Lorenzo via Telegram di un errore. Best-effort, mai bloccante."""
    if not OWNER_ID or not TOKEN:
        return
    try:
        msg = f"⚠️ Bot errore [{contesto}]: {type(errore).__name__}: {str(errore)[:300]}"
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

def salva_prenotazione(chat_id, nome, checkin, checkout, lingua):
    if not GITHUB_TOKEN:
        return False
    try:
        prenotazioni, sha = carica_prenotazioni()
        prenotazioni[str(chat_id)] = {
            "nome": nome,
            "checkin": checkin,
            "checkout": checkout,
            "lingua": lingua,
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
    r = urllib.request.urlopen(req, timeout=10)
    return json.loads(r.read())

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

def modifica_messaggio(chat_id, message_id, testo, parse_mode=None):
    try:
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": testo
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
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

SYSTEM_PROMPT = {
    "italian": (
        "Sei un assistente virtuale per un appartamento in affitto su Booking e Airbnb. "
        "Rispondi SOLO con le informazioni presenti nel testo qui sotto — non aggiungere nulla che non sia scritto. "
        "ATTENZIONE AI NUMERI: cita ogni numero ESATTAMENTE come appare nel testo. Non confondere mai numeri diversi tra loro (es. numero civico, numero appartamento, codice, piano sono cose diverse). "
        "Se la domanda riguarda un argomento specifico, rispondi SOLO su quell'argomento senza aggiungere altre informazioni non richieste. "
        "Se non hai l'informazione richiesta, di' che lo chiederai a Lorenzo e risponderai al più presto. "
        "IMPORTANTE: non condividere MAI il numero di telefono del proprietario a meno che l'ospite non lo chieda esplicitamente. "
        "Riferisciti sempre al proprietario come 'Lorenzo'. "
        "Sii cordiale e conciso. "
        "Aggiungi 1-2 emoji coerenti con l'argomento (es. 🚗 parcheggio, 🏖️ spiaggia, 🚆 treno, 📶 wifi, 🔑 check-in, 🛒 supermercato, 🍽️ ristorante).\n\nINFORMAZIONI APPARTAMENTO:\n{info}"
    ),
    "french": (
        "Tu es un assistant virtuel pour un appartement de location sur Booking et Airbnb. "
        "Réponds UNIQUEMENT avec les informations du texte ci-dessous — n'ajoute rien qui n'y soit pas écrit. "
        "ATTENTION AUX CHIFFRES: cite chaque numéro EXACTEMENT comme il apparaît dans le texte. Ne confonds jamais des numéros différents (numéro de rue, numéro d'appartement, code, étage sont des choses distinctes). "
        "Si la question porte sur un sujet précis, réponds UNIQUEMENT sur ce sujet sans ajouter d'autres informations non demandées. "
        "Si tu n'as pas l'information, dis que tu vas demander à Lorenzo. "
        "IMPORTANT: ne partage JAMAIS le numéro de téléphone sauf si demandé explicitement. "
        "Réfère-toi toujours au propriétaire comme 'Lorenzo'. "
        "Sois cordial et concis. "
        "Ajoute 1-2 emojis cohérents avec le sujet (ex. 🚗 parking, 🏖️ plage, 🚆 train, 📶 wifi, 🔑 check-in).\n\nINFORMATIONS APPARTEMENT:\n{info}"
    ),
    "english": (
        "You are a virtual assistant for a vacation rental apartment on Booking and Airbnb. "
        "Answer ONLY using the information in the text below — do not add anything not written there. "
        "WARNING ABOUT NUMBERS: quote every number EXACTLY as it appears in the text. Never confuse different numbers (street number, apartment number, access code, floor are all different things). "
        "If the question is about a specific topic, answer ONLY about that topic without adding unrequested information. "
        "If you don't have the information, say you will ask Lorenzo. "
        "IMPORTANT: never share the owner's phone number unless explicitly asked. "
        "Always refer to the owner as 'Lorenzo'. "
        "Be friendly and concise. "
        "Add 1-2 relevant emojis (e.g. 🚗 parking, 🏖️ beach, 🚆 train, 📶 wifi, 🔑 check-in, 🛒 supermarket, 🍽️ restaurant).\n\nAPARTMENT INFORMATION:\n{info}"
    ),
    "spanish": (
        "Eres un asistente virtual para un apartamento de alquiler en Booking y Airbnb. "
        "Responde SOLO con la información del texto de abajo — no añadas nada que no esté escrito. "
        "ATENCIÓN A LOS NÚMEROS: cita cada número EXACTAMENTE como aparece en el texto. No confundas nunca números distintos (número de calle, número de apartamento, código, piso son cosas diferentes). "
        "Si la pregunta es sobre un tema específico, responde SOLO sobre ese tema. "
        "Si no tienes la información, di que se lo preguntarás a Lorenzo. "
        "IMPORTANTE: nunca compartas el teléfono salvo si se pide explícitamente. "
        "Llama siempre al propietario 'Lorenzo'. "
        "Sé cordial y conciso. "
        "Añade 1-2 emojis coherentes con el tema (ej. 🚗 aparcamiento, 🏖️ playa, 🚆 tren, 📶 wifi, 🔑 check-in).\n\nINFORMACIÓN DEL APARTAMENTO:\n{info}"
    ),
    "german": (
        "Du bist ein virtueller Assistent für eine Ferienwohnung auf Booking und Airbnb. "
        "Antworte NUR mit den Informationen aus dem Text unten — füge nichts hinzu, was nicht dort steht. "
        "ACHTUNG BEI ZAHLEN: Zitiere jede Zahl GENAU so wie sie im Text erscheint. Verwechsle niemals verschiedene Zahlen (Hausnummer, Wohnungsnummer, Code, Etage sind verschiedene Dinge). "
        "Wenn die Frage ein bestimmtes Thema betrifft, antworte NUR zu diesem Thema. "
        "Wenn du die Information nicht hast, sage dass du Lorenzo fragen wirst. "
        "WICHTIG: Teile die Telefonnummer NIEMALS mit, außer wenn ausdrücklich danach gefragt. "
        "Nenne den Eigentümer immer 'Lorenzo'. "
        "Sei freundlich und prägnant. "
        "Füge 1-2 passende Emojis hinzu (z.B. 🚗 Parkplatz, 🏖️ Strand, 🚆 Zug, 📶 WLAN, 🔑 Check-in).\n\nWOHNUNGSINFORMATIONEN:\n{info}"
    ),
}

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
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 1024,
        "system": system_text,
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

        # ── Proprietario invia foto/video → avvia flusso guidato ──
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
                _upload_media[str(chat_id)] = {"file_id": file_id, "tipo": tipo, "step": "keywords"}
                invia_messaggio(chat_id,
                    f"📸 {'Foto' if tipo == 'photo' else 'Video'} ricevuto! Procediamo passo per passo.\n\n"
                    f"1️⃣ Scrivi le *parole chiave* che attiveranno questo media.\n"
                    f"Separale con una virgola — scrivi in tutte le lingue dei tuoi ospiti.\n\n"
                    f"Esempio:\n`box, garage, parcheggio, parking, park`"
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
            # Cerca prima [WA:numero] (ospite WhatsApp), poi [ID:numero] (ospite Telegram)
            match_wa = re.search(r'\[WA:(\d+)\]', testo_originale)
            match_id = re.search(r'\[ID:(\d+)\]', testo_originale)
            if match_wa:
                wa_numero = match_wa.group(1)
                # Inoltra a WhatsApp via Cloud API
                wa_invia(wa_numero, f"💬 {testo}")
                # Aggiorna anche la storia conversazione lato bot
                try:
                    aggiorna_storia(f"wa_{wa_numero}", "[Risposta diretta di Lorenzo]", testo)
                except Exception:
                    pass
                invia_messaggio(chat_id, f"✅ Risposta inviata su WhatsApp a +{wa_numero}!")
                # Offri di salvare in memoria
                match_domanda = re.search(r'❓ "(.+?)"', testo_originale, re.DOTALL)
                if not match_domanda:
                    match_domanda = re.search(r'❓ (.+?)(?:\n|$)', testo_originale)
                if match_domanda:
                    domanda_originale = match_domanda.group(1).strip()
                    invia_bottoni(chat_id,
                        f"💾 Vuoi salvare questa risposta nella memoria del bot?\n\nD: {domanda_originale}\nR: {testo}",
                        [[
                            {"text": "✅ Sì, salva", "callback_data": "SALVA"},
                            {"text": "❌ No",         "callback_data": "NO"}
                        ]]
                    )
                return "ok"
            if match_id:
                id_ospite = int(match_id.group(1))
                invia_messaggio(id_ospite, f"💬 {testo}")
                invia_messaggio(chat_id, "✅ Risposta inviata all'ospite!")
                # Estrae la domanda sia dal formato ❓ "testo" che da ❓ testo
                match_domanda = re.search(r'❓ "(.+?)"', testo_originale, re.DOTALL)
                if not match_domanda:
                    match_domanda = re.search(r'❓ (.+?)(?:\n|$)', testo_originale)
                if match_domanda:
                    domanda_originale = match_domanda.group(1).strip()
                    invia_bottoni(chat_id,
                        f"💾 Vuoi salvare questa risposta nella memoria del bot?\n\nD: {domanda_originale}\nR: {testo}",
                        [[
                            {"text": "✅ Sì, salva", "callback_data": "SALVA"},
                            {"text": "❌ No",         "callback_data": "NO"}
                        ]]
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

        # ── Proprietario scrive info direttamente → chiede se salvare ──
        if is_owner and not message.get("reply_to_message") and not testo.startswith("/"):
            invia_bottoni(chat_id,
                f"💾 Vuoi aggiungere questa info ad appartamento.txt?\n\nR: {testo}",
                [[
                    {"text": "✅ Sì, aggiungi", "callback_data": "SALVA"},
                    {"text": "❌ No",            "callback_data": "NO"}
                ]]
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

        # ── Risposta AI ─────────────────────────────────────────────────────
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

        # ── Notifica proprietario ──
        if OWNER_ID and not is_owner:
            try:
                nome_display = f"@{username}" if username else nome
                if e_emergenza:
                    invia_messaggio(int(OWNER_ID),
                        f"🚨🚨 EMERGENZA TECNICA 🚨🚨\n\n"
                        f"Ospite: {nome_display} [ID:{chat_id}]\n\n"
                        f"❓ {testo}\n\n🤖 {reply}\n\n"
                        f"⚡ Rispondi subito all'ospite premendo Rispondi."
                    )
                elif e_insoddisfatto:
                    invia_messaggio(int(OWNER_ID),
                        f"😤 OSPITE INSODDISFATTO\n\n"
                        f"Ospite: {nome_display} [ID:{chat_id}]\n\n"
                        f"❓ {testo}\n\n"
                        f"🤖 {reply}\n\n"
                        f"👆 Premi Rispondi per contattarlo direttamente."
                    )
                else:
                    invia_messaggio(int(OWNER_ID),
                        f"📩 {nome_display} [ID:{chat_id}]\n\n❓ {testo}\n\n🤖 {reply}"
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
            invia_messaggio(int(OWNER_ID),
                f"⚠️ RISPOSTA RICHIESTA\n\n"
                f"L'ospite {nome_display} ha chiesto qualcosa che non so rispondere:\n\n"
                f"❓ \"{testo}\"\n\n"
                f"Premi Rispondi e scrivi la tua risposta.\n[ID:{chat_id}]"
            )

    except Exception:
        pass

    return "ok"


@app.route("/daily-report", methods=["GET", "POST"])
def daily_report():
    """Chiamato da Vercel Cron ogni sera alle 21:00 CET."""
    try:
        testo = formatta_daily_stats()
        invia_messaggio(int(OWNER_ID), testo, parse_mode="Markdown")
    except Exception:
        pass
    return "ok"

@app.route("/reset-keyboards")
def reset_keyboards():
    """Rimuove la tastiera rapida da tutti gli utenti con prenotazione."""
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

@app.route("/")
def health():
    return "Bot attivo! ✓"


# ── Dashboard ─────────────────────────────────────────────────────────────────
def _check_dash_key():
    """Verifica chiave dashboard. Ritorna True se valida, False altrimenti."""
    if not DASHBOARD_KEY:
        return False
    return request.args.get("key", "") == DASHBOARD_KEY

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

@app.route("/dashboard/api/data")
def dashboard_api_data():
    if not _check_dash_key():
        return ("Forbidden", 403)
    from flask import jsonify
    return jsonify(_aggrega_dashboard_data())


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
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard — Bot Appartamento</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;background:#f5f7fa;color:#222;padding:20px;max-width:1200px;margin:0 auto}
h1{color:#0066cc;margin-bottom:8px;font-size:24px}
.sub{color:#888;font-size:13px;margin-bottom:20px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:24px}
.kpi{background:#fff;padding:16px;border-radius:10px;box-shadow:0 2px 4px rgba(0,0,0,.06);text-align:center}
.kpi .num{font-size:28px;font-weight:700;color:#0066cc}
.kpi .lbl{font-size:12px;color:#666;text-transform:uppercase;letter-spacing:.5px;margin-top:4px}
.row{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
.card{background:#fff;padding:16px;border-radius:10px;box-shadow:0 2px 4px rgba(0,0,0,.06)}
.card h2{font-size:16px;margin-bottom:12px;color:#333;border-bottom:1px solid #eee;padding-bottom:8px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;padding:8px 6px;background:#f8f9fa;font-weight:600;color:#555;font-size:12px;text-transform:uppercase}
td{padding:8px 6px;border-bottom:1px solid #f0f0f0}
tr:hover td{background:#f9fafc}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600}
.b-tg{background:#dbeafe;color:#1e40af}
.b-wa{background:#dcfce7;color:#166534}
.b-cur{background:#fef3c7;color:#92400e}
.b-fut{background:#dbeafe;color:#1e40af}
.b-past{background:#f3f4f6;color:#6b7280}
.empty{color:#999;font-style:italic;text-align:center;padding:20px}
.refresh{float:right;background:#0066cc;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px}
a{color:#0066cc;text-decoration:none}
a:hover{text-decoration:underline}
@media(max-width:720px){.row{grid-template-columns:1fr}}
</style>
</head>
<body>
<h1>🏠 Dashboard — Bot Appartamento Juan les Pins
<button class="refresh" onclick="caricaDati()">🔄 Aggiorna</button></h1>
<div class="sub" id="aggiornata">Caricamento...</div>

<div class="kpis" id="kpis"></div>

<div class="row">
  <div class="card"><h2>📊 Argomenti più richiesti</h2><canvas id="grafTopic"></canvas></div>
  <div class="card"><h2>🌍 Lingue</h2><canvas id="grafLingue"></canvas></div>
</div>

<div class="card" style="margin-bottom:16px">
  <h2>💬 Conversazioni attive (ultime 24h)</h2>
  <div id="conversazioni"></div>
</div>

<div class="card" style="margin-bottom:16px">
  <h2>👥 Top clienti per attività</h2>
  <div id="topClienti"></div>
</div>

<div class="row">
  <div class="card"><h2>📅 Prenotazioni correnti</h2><div id="prenCorrenti"></div></div>
  <div class="card"><h2>🔮 Prenotazioni prossime</h2><div id="prenProssime"></div></div>
</div>

<div class="card" style="margin-bottom:16px">
  <h2>📦 Prenotazioni passate</h2>
  <div id="prenPassate"></div>
</div>

<div class="card" style="margin-bottom:24px">
  <h2>📸 Media salvati</h2>
  <div id="mediaList"></div>
</div>

<script>
const KEY = """ + json.dumps(key) + """;
let chTopic, chLingue;
function vuoto(t){return '<div class="empty">'+t+'</div>'}
function badgeC(c){return c==='whatsapp'?'<span class="badge b-wa">📱 WA</span>':'<span class="badge b-tg">💬 TG</span>'}
function tabella(rows, cols){
  if(!rows.length)return vuoto('Nessun dato.');
  let h='<table><thead><tr>'+cols.map(c=>'<th>'+c.l+'</th>').join('')+'</tr></thead><tbody>';
  rows.forEach(r=>{h+='<tr>'+cols.map(c=>'<td>'+(c.f?c.f(r):r[c.k]||'—')+'</td>').join('')+'</tr>'});
  return h+'</tbody></table>';
}
async function caricaDati(){
  document.getElementById('aggiornata').textContent='Caricamento...';
  try{
    const r=await fetch('/dashboard/api/data?key='+encodeURIComponent(KEY));
    if(!r.ok){document.getElementById('aggiornata').textContent='Errore '+r.status;return}
    const d=await r.json();
    document.getElementById('aggiornata').textContent='Aggiornata: '+d.generato_il;
    // KPI
    document.getElementById('kpis').innerHTML=[
      ['Totale messaggi','totale_lifetime'],
      ['Messaggi oggi','totale_oggi'],
      ['Clienti totali','totale_clienti'],
      ['Ospiti attivi (7gg)','ospiti_attivi']
    ].map(([l,k])=>'<div class="kpi"><div class="num">'+(d.kpi[k]||0)+'</div><div class="lbl">'+l+'</div></div>').join('');
    // Grafici
    if(chTopic)chTopic.destroy();
    chTopic=new Chart(document.getElementById('grafTopic'),{type:'bar',data:{labels:Object.keys(d.argomenti),datasets:[{label:'Messaggi',data:Object.values(d.argomenti),backgroundColor:'#0066cc'}]},options:{plugins:{legend:{display:false}},responsive:true}});
    if(chLingue)chLingue.destroy();
    chLingue=new Chart(document.getElementById('grafLingue'),{type:'doughnut',data:{labels:Object.keys(d.lingue),datasets:[{data:Object.values(d.lingue),backgroundColor:['#0066cc','#22c55e','#f59e0b','#ef4444','#a855f7']}]},options:{responsive:true}});
    // Conversazioni
    document.getElementById('conversazioni').innerHTML=tabella(d.conversazioni_attive,[
      {l:'Canale',f:r=>badgeC(r.canale)},
      {l:'Nome',k:'nome'},
      {l:'Msg',k:'msg_in_storia'},
      {l:'Ultimo',k:'ultimo_msg'},
      {l:'Storia',f:r=>'<a href="/dashboard/conversation/'+encodeURIComponent(r.chat_id)+'?key='+encodeURIComponent(KEY)+'" target="_blank">Vedi →</a>'}
    ]);
    // Top clienti
    document.getElementById('topClienti').innerHTML=tabella(d.top_clienti,[
      {l:'#',f:(r,i)=>r._i||''},
      {l:'Canale',f:r=>badgeC(r.canale)},
      {l:'Nome',f:r=>r.nome+(r.username?' <span style="color:#888">@'+r.username+'</span>':'')},
      {l:'Msg totali',k:'totale_msg'},
      {l:'Topic top',k:'topic_top'},
      {l:'Lingua',k:'lingua'},
      {l:'Primo',f:r=>(r.primo_msg||'').substring(0,10)},
      {l:'Ultimo',f:r=>(r.ultimo_msg||'').substring(0,16).replace('T',' ')}
    ].map((c,i)=>i===0?{l:'#',f:(r)=>d.top_clienti.indexOf(r)+1}:c));
    // Prenotazioni
    const colsPren=[
      {l:'Nome',k:'nome'},{l:'Check-in',k:'checkin'},{l:'Check-out',k:'checkout'},{l:'Lingua',k:'lingua'}
    ];
    document.getElementById('prenCorrenti').innerHTML=tabella(d.prenotazioni_correnti,colsPren);
    document.getElementById('prenProssime').innerHTML=tabella(d.prenotazioni_prossime,colsPren);
    document.getElementById('prenPassate').innerHTML=tabella(d.prenotazioni_passate,colsPren);
    // Media
    document.getElementById('mediaList').innerHTML=tabella(d.media,[
      {l:'#',k:'n'},
      {l:'Tipo',f:r=>r.tipo==='video'?'🎬 Video':'📸 Foto'},
      {l:'Keywords',k:'keywords'},
      {l:'Caption',k:'caption'}
    ]);
  }catch(e){document.getElementById('aggiornata').textContent='Errore: '+e.message}
}
caricaDati();
setInterval(caricaDati,60000);
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
        if mode == "subscribe" and token == WA_VERIFY_TOKEN:
            return challenge, 200
        return "Unauthorized", 403

    # ── Messaggi in arrivo (POST da Meta) ──
    try:
        body    = request.get_json(force=True)
        entry   = body.get("entry", [])
        if not entry:
            return "ok"
        changes = entry[0].get("changes", [])
        if not changes:
            return "ok"
        value    = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            return "ok"

        msg = messages[0]

        # Gestisci solo messaggi di testo
        if msg.get("type") != "text":
            wa_from = msg["from"]
            contacts = value.get("contacts", [])
            nome = contacts[0]["profile"]["name"] if contacts else "Ospite"
            wa_invia(wa_from, "Ciao! 😊 Al momento riesco a rispondere solo ai messaggi di testo. Scrivi pure la tua domanda!")
            return "ok"

        wa_from = msg["from"]   # es. "393202599675" (senza +)
        testo   = msg["text"]["body"]

        # Nome del contatto
        contacts = value.get("contacts", [])
        nome     = contacts[0]["profile"]["name"] if contacts else "Ospite"

        # Chiave sessione WhatsApp separata da Telegram
        wa_session_id = f"wa_{wa_from}"

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
        info  = leggi_info()
        reply = chiedi_ai(testo, info, chat_id=wa_session_id)
        aggiorna_storia(wa_session_id, testo, reply)
        # Aggiorna anagrafica utente (per dashboard)
        try:
            lingua_stat = rileva_lingua(testo)
            aggiorna_user(wa_session_id, "whatsapp", nome, testo, lingua_stat, None)
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
        if OWNER_ID:
            try:
                invia_messaggio(int(OWNER_ID),
                    f"📱 *WhatsApp* — {nome}\n\n❓ {testo}\n\n🤖 {reply}\n\n[WA:{wa_from}]"
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
                    f"❓ {testo}\n\n🤖 {reply}\n\n[WA:{wa_from}]"
                )
            except Exception:
                pass

        # Stats
        try:
            lingua_stat = rileva_lingua(testo)
            aggiorna_stats(testo, lingua_stat)
            aggiorna_daily_stats(testo, lingua_stat, wa_session_id)
        except Exception:
            pass

    except Exception:
        pass

    return "ok"
