from flask import Flask, request
import json, os, re, base64, urllib.request
from datetime import datetime

app = Flask(__name__)

TOKEN          = os.environ.get("TELEGRAM_TOKEN")
GROQ_KEY       = os.environ.get("GROQ_API_KEY")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_KEY")
OWNER_ID       = os.environ.get("OWNER_CHAT_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")

WA_TOKEN        = os.environ.get("WHATSAPP_TOKEN")
WA_PHONE_ID     = os.environ.get("WHATSAPP_PHONE_ID")
WA_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "juanlespins2026").strip()

REPO         = "Lorenzog2006/appartamento-bot"
GITHUB_RAW   = f"https://raw.githubusercontent.com/{REPO}/main/appartamento.txt"
GITHUB_API   = f"https://api.github.com/repos/{REPO}/contents/appartamento.txt"
STATS_API       = f"https://api.github.com/repos/{REPO}/contents/stats.json"
DAILY_STATS_API = f"https://api.github.com/repos/{REPO}/contents/daily_stats.json"
BOOKINGS_API    = f"https://api.github.com/repos/{REPO}/contents/bookings.json"
INFO_PATH    = os.path.join(os.path.dirname(__file__), "appartamento.txt")

# ── Stato sessioni ────────────────────────────────────────────────────────────
# chat_id → {"storia": [...], "ultimo": timestamp}
_conversazioni = {}
MAX_MESSAGGI   = 10
SCADENZA_ORE   = 2

# chat_id ospite → {"nome": nome, "lingua": lingua} — aspettiamo le date
_attesa_date = {}
# OWNER_ID → guest_chat_id — Lorenzo sta per inviare date corrette
_attesa_correzione_owner = {}
# Flusso guidato upload media: OWNER_ID → {file_id, tipo, step, keywords}
_upload_media = {}

def get_storia(chat_id):
    ora = datetime.now().timestamp()
    conv = _conversazioni.get(chat_id)
    if conv and (ora - conv["ultimo"]) > SCADENZA_ORE * 3600:
        del _conversazioni[chat_id]
        conv = None
    return conv["storia"] if conv else []

def aggiorna_storia(chat_id, domanda, risposta):
    ora = datetime.now().timestamp()
    if chat_id not in _conversazioni:
        _conversazioni[chat_id] = {"storia": [], "ultimo": ora}
    storia = _conversazioni[chat_id]["storia"]
    storia.append({"role": "user",      "content": domanda})
    storia.append({"role": "assistant", "content": risposta})
    if len(storia) > MAX_MESSAGGI * 2:
        storia = storia[-(MAX_MESSAGGI * 2):]
    _conversazioni[chat_id]["storia"] = storia
    _conversazioni[chat_id]["ultimo"] = ora


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

def invia_bottoni(chat_id, testo, bottoni):
    telegram("sendMessage", {
        "chat_id": chat_id,
        "text": testo,
        "reply_markup": {"inline_keyboard": bottoni}
    })

def modifica_messaggio(chat_id, message_id, testo):
    try:
        telegram("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": testo
        })
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

def chiedi_ai(domanda, info, chat_id=None):
    lingua = rileva_lingua(domanda)
    system_text = SYSTEM_PROMPT.get(lingua, SYSTEM_PROMPT["english"]).format(info=info[:12000])
    storia = get_storia(chat_id) if chat_id else []
    # Converti storia in formato Anthropic (no "system" nei messages)
    messages_claude = []
    for m in storia:
        messages_claude.append({"role": m["role"], "content": m["content"]})
    messages_claude.append({"role": "user", "content": domanda})
    try:
        # Claude Haiku 3.5 — prima scelta
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": "claude-haiku-4-5",
            "max_tokens": 1024,
            "system": system_text,
            "messages": messages_claude
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01"
        })
        r = urllib.request.urlopen(req, timeout=20)
        return json.loads(r.read())["content"][0]["text"]
    except Exception:
        # Fallback Groq se Claude non risponde
        messages_groq = [{"role": "system", "content": system_text}, *storia, {"role": "user", "content": domanda}]
        return _chiama_groq("llama-3.1-8b-instant", messages_groq, timeout=10)

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
                m_fid  = re.search(r'FILE_ID: (.+)', cb_testo)
                m_tipo = re.search(r'TIPO: (.+)', cb_testo)
                m_kw   = re.search(r'PAROLE_CHIAVE: (.+)', cb_testo)
                m_desc = re.search(r'DESCRIZIONE: (.+)', cb_testo)
                if m_fid and m_tipo and m_kw and m_desc:
                    salvato = salva_media_su_github(
                        m_kw.group(1).strip(), m_tipo.group(1).strip(),
                        m_fid.group(1).strip(), m_desc.group(1).strip()
                    )
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"✅ Media salvato!\n\nParole chiave: {m_kw.group(1).strip()}\nDa ora rispondo automaticamente con questa foto/video."
                        if salvato else "❌ Errore nel salvataggio.")

            # ── Salva Q&A o info ──
            elif cb_data == "SALVA":
                match_dq = re.search(r'D: (.+?)\nR: (.+)', cb_testo, re.DOTALL)
                match_r  = re.search(r'R: (.+)', cb_testo, re.DOTALL)
                if match_dq:
                    domanda  = match_dq.group(1).strip()
                    risposta = match_dq.group(2).strip()
                    salvato  = salva_su_github(f"{domanda}: {risposta}", "")
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"🧠 Salvato!\n\nD: {domanda}\nR: {risposta}\n\nLa prossima volta rispondo in autonomia."
                        if salvato else "❌ Errore nel salvataggio.")
                elif match_r:
                    salvato = salva_su_github(match_r.group(1).strip(), "")
                    modifica_messaggio(cb_chat_id, cb_msg_id,
                        f"✅ Info aggiunta:\n\n{match_r.group(1).strip()}"
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
            match_id = re.search(r'\[ID:(\d+)\]', testo_originale)
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
    except Exception:
        pass


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

        # Invia risposta all'ospite su WhatsApp
        wa_invia(wa_from, reply)

        # Notifica Lorenzo su Telegram
        if OWNER_ID:
            try:
                invia_messaggio(int(OWNER_ID),
                    f"📱 *WhatsApp* — {nome}\n\n❓ {testo}\n\n🤖 {reply}"
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
                    f"❓ {testo}\n\n🤖 {reply}"
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
