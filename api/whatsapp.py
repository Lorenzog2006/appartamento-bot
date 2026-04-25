from http.server import BaseHTTPRequestHandler
import json, os, urllib.request, urllib.parse, re, base64
from datetime import datetime

# ── Credenziali ───────────────────────────────────────────────────────────────
GROQ_KEY        = os.environ.get("GROQ_API_KEY")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_KEY")
OWNER_ID        = os.environ.get("OWNER_CHAT_ID")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
WA_TOKEN        = os.environ.get("WHATSAPP_TOKEN")
WA_PHONE_ID     = os.environ.get("WHATSAPP_PHONE_ID")
WA_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "juanlespins2026").strip()

REPO       = "Lorenzog2006/appartamento-bot"
GITHUB_RAW = f"https://raw.githubusercontent.com/{REPO}/main/appartamento.txt"

# ── Sessioni conversazione WhatsApp ───────────────────────────────────────────
_wa_conversazioni = {}
MAX_MESSAGGI = 10
SCADENZA_ORE = 2

def get_storia(wa_id):
    ora = datetime.now().timestamp()
    conv = _wa_conversazioni.get(wa_id)
    if conv and (ora - conv["ultimo"]) > SCADENZA_ORE * 3600:
        del _wa_conversazioni[wa_id]
        return []
    return conv["storia"] if conv else []

def aggiorna_storia(wa_id, domanda, risposta):
    ora = datetime.now().timestamp()
    if wa_id not in _wa_conversazioni:
        _wa_conversazioni[wa_id] = {"storia": [], "ultimo": ora}
    storia = _wa_conversazioni[wa_id]["storia"]
    storia.append({"role": "user", "content": domanda})
    storia.append({"role": "assistant", "content": risposta})
    if len(storia) > MAX_MESSAGGI * 2:
        storia = storia[-(MAX_MESSAGGI * 2):]
    _wa_conversazioni[wa_id]["storia"] = storia
    _wa_conversazioni[wa_id]["ultimo"] = ora

# ── Leggi appartamento.txt da GitHub ──────────────────────────────────────────
_cache = {"testo": "", "ts": 0}

def leggi_info():
    ora = datetime.now().timestamp()
    if _cache["testo"] and (ora - _cache["ts"]) < 300:
        return _cache["testo"]
    try:
        req = urllib.request.Request(
            f"{GITHUB_RAW}?t={int(ora)}",
            headers={"Cache-Control": "no-cache", "User-Agent": "appartamento-bot"}
        )
        r = urllib.request.urlopen(req, timeout=5)
        testo = r.read().decode("utf-8")
        # Rimuovi sezione [MEDIA]
        info = re.sub(r'\[MEDIA\].*', '', testo, flags=re.DOTALL).strip()
        if info:
            _cache["testo"] = info
            _cache["ts"] = ora
            return info
    except Exception:
        pass
    return _cache["testo"] or "Informazioni appartamento non disponibili."

# ── Rilevamento lingua ────────────────────────────────────────────────────────
def rileva_lingua(testo):
    t = " " + testo.lower() + " "
    punteggi = {"french": 0, "english": 0, "spanish": 0, "german": 0}
    parole_fr = ["bonjour","bonsoir","merci","comment","quelle","où","clé","plage","voiture","parking"]
    parole_en = ["hello","hi ","thanks","where","what","how","wifi","beach","parking","check"]
    parole_es = ["hola","buenos","gracias","dónde","cómo","playa","wifi","parking"]
    parole_de = ["hallo","guten","danke","bitte","gibt es","wie ","wo ist","strand"]
    for w in parole_fr: punteggi["french"]  += t.count(w)
    for w in parole_en: punteggi["english"] += t.count(w)
    for w in parole_es: punteggi["spanish"] += t.count(w)
    for w in parole_de: punteggi["german"]  += t.count(w)
    migliore = max(punteggi, key=punteggi.get)
    return migliore if punteggi[migliore] > 0 else "italian"

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = {
    "italian": (
        "Sei un assistente virtuale per un appartamento in affitto su Booking e Airbnb. "
        "Rispondi SOLO con le informazioni presenti nel testo qui sotto. "
        "ATTENZIONE AI NUMERI: cita ogni numero ESATTAMENTE come appare nel testo. "
        "Se non hai l'informazione, di' che lo chiederai a Lorenzo. "
        "Riferisciti al proprietario come 'Lorenzo'. Sii cordiale e conciso. "
        "Aggiungi 1-2 emoji coerenti con l'argomento.\n\nINFORMAZIONI APPARTAMENTO:\n{info}"
    ),
    "french": (
        "Tu es un assistant virtuel pour un appartement de location. "
        "Réponds UNIQUEMENT avec les informations du texte ci-dessous. "
        "ATTENTION AUX CHIFFRES: cite chaque numéro EXACTEMENT. "
        "Si tu n'as pas l'info, dis que tu vas demander à Lorenzo. "
        "Réfère-toi au propriétaire comme 'Lorenzo'. Sois cordial et concis. "
        "Ajoute 1-2 emojis cohérents.\n\nINFORMATIONS APPARTEMENT:\n{info}"
    ),
    "english": (
        "You are a virtual assistant for a vacation rental apartment. "
        "Answer ONLY using the information in the text below. "
        "WARNING ABOUT NUMBERS: quote every number EXACTLY as it appears. "
        "If you don't have the info, say you will ask Lorenzo. "
        "Always refer to the owner as 'Lorenzo'. Be friendly and concise. "
        "Add 1-2 relevant emojis.\n\nAPARTMENT INFORMATION:\n{info}"
    ),
    "spanish": (
        "Eres un asistente virtual para un apartamento de alquiler. "
        "Responde SOLO con la información del texto de abajo. "
        "ATENCIÓN A LOS NÚMEROS: cita cada número EXACTAMENTE. "
        "Si no tienes la info, di que se lo preguntarás a Lorenzo. "
        "Llama al propietario 'Lorenzo'. Sé cordial y conciso. "
        "Añade 1-2 emojis coherentes.\n\nINFORMACIÓN DEL APARTAMENTO:\n{info}"
    ),
    "german": (
        "Du bist ein virtueller Assistent für eine Ferienwohnung. "
        "Antworte NUR mit den Informationen aus dem Text unten. "
        "ACHTUNG BEI ZAHLEN: Zitiere jede Zahl GENAU so wie sie erscheint. "
        "Wenn du die Info nicht hast, sage dass du Lorenzo fragen wirst. "
        "Nenne den Eigentümer immer 'Lorenzo'. Sei freundlich und prägnant. "
        "Füge 1-2 passende Emojis hinzu.\n\nWOHNUNGSINFORMATIONEN:\n{info}"
    ),
}

# ── Messaggio di benvenuto ────────────────────────────────────────────────────
BENVENUTO = """Benvenuto! 😊 Sono l'assistente virtuale dell'appartamento di Lorenzo a Juan les Pins.

🔑 KeyBox codice: *8492*
📍 KeyBox: lato SINISTRO cancello garage — 67 Chemin des Liserons, Antibes
🚗 Posto auto n°53 (scendi rampa, tieniti a sinistra, fondo a sinistra)
🏠 Appartamento: 93 Bd Raymond Poincaré, piano 2°, porta n°23

🕐 Check-in dalle 16:00

Buon soggiorno in Costa Azzurra! 🌊☀️
Per qualsiasi domanda scrivi pure qui!"""

# ── AI: chiedi risposta ───────────────────────────────────────────────────────
def chiedi_ai(domanda, info, storia):
    lingua = rileva_lingua(domanda)
    system_text = SYSTEM_PROMPT.get(lingua, SYSTEM_PROMPT["english"]).format(info=info[:12000])
    messages = []
    for m in storia:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": domanda})
    try:
        # Claude Haiku (preferito)
        url = "https://api.anthropic.com/v1/messages"
        payload = {
            "model": "claude-haiku-4-5",
            "max_tokens": 1024,
            "system": system_text,
            "messages": messages
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01"
        })
        r = urllib.request.urlopen(req, timeout=20)
        return json.loads(r.read())["content"][0]["text"]
    except Exception:
        # Fallback Groq
        try:
            url = "https://api.groq.com/openai/v1/chat/completions"
            payload = {
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": system_text}] + messages
            }
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GROQ_KEY}"
            })
            r = urllib.request.urlopen(req, timeout=10)
            return json.loads(r.read())["choices"][0]["message"]["content"]
        except Exception:
            return "Mi dispiace, in questo momento non riesco a rispondere. Contatterò Lorenzo per te! 🙏"

# ── Invia messaggio WhatsApp ──────────────────────────────────────────────────
def wa_invia(to, testo):
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

# ── Notifica Lorenzo su Telegram ──────────────────────────────────────────────
def notifica_telegram(testo):
    if not OWNER_ID or not TELEGRAM_TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": OWNER_ID, "text": testo}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

# ── Handler Vercel ────────────────────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def do_GET(self):
        """Verifica webhook da Meta."""
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            mode      = params.get("hub.mode", [""])[0]
            token     = params.get("hub.verify_token", [""])[0]
            challenge = params.get("hub.challenge", [""])[0]
            if mode == "subscribe" and token.strip() == WA_VERIFY_TOKEN:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(challenge.encode())
                return
        except Exception:
            pass
        self.send_response(403)
        self.end_headers()
        self.wfile.write(b"Unauthorized")

    def do_POST(self):
        """Messaggi in arrivo da WhatsApp."""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))

            entry = body.get("entry", [])
            if not entry:
                self._ok()
                return
            value    = entry[0].get("changes", [{}])[0].get("value", {})
            messages = value.get("messages", [])
            if not messages:
                self._ok()
                return

            msg     = messages[0]
            wa_from = msg["from"]

            # Solo messaggi di testo
            if msg.get("type") != "text":
                wa_invia(wa_from, "Ciao! 😊 Scrivi pure la tua domanda in testo e ti rispondo subito!")
                self._ok()
                return

            testo    = msg["text"]["body"]
            contacts = value.get("contacts", [])
            nome     = contacts[0]["profile"]["name"] if contacts else "Ospite"

            # Prima volta → benvenuto
            storia = get_storia(wa_from)
            if not storia:
                wa_invia(wa_from, BENVENUTO)

            # Risposta AI
            info  = leggi_info()
            reply = chiedi_ai(testo, info, storia)
            aggiorna_storia(wa_from, testo, reply)

            # Invia risposta all'ospite
            wa_invia(wa_from, reply)

            # Notifica Lorenzo su Telegram
            notifica_telegram(f"📱 WhatsApp — {nome}\n\n❓ {testo}\n\n🤖 {reply}")

        except Exception:
            pass

        self._ok()

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
