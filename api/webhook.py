from http.server import BaseHTTPRequestHandler
import json, os, urllib.request

TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
OWNER_ID = os.environ.get("OWNER_CHAT_ID")

INFO_PATH = os.path.join(os.path.dirname(__file__), "..", "appartamento.txt")


def invia_messaggio(chat_id, testo):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": testo}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def leggi_info():
    try:
        with open(INFO_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return "Informazioni appartamento non disponibili."


def chiedi_ai(domanda, info):
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Sei un assistente virtuale per un appartamento in affitto su Booking e Airbnb. "
                    "Rispondi SOLO con le informazioni qui sotto. "
                    "Se non hai l'informazione richiesta, di' che contatterai il proprietario al più presto. "
                    "Rispondi nella stessa lingua dell'ospite. Sii cordiale e conciso.\n\n"
                    f"INFORMAZIONI APPARTAMENTO:\n{info}"
                )
            },
            {"role": "user", "content": domanda}
        ]
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_KEY}"
        }
    )
    r = urllib.request.urlopen(req, timeout=25)
    result = json.loads(r.read())
    return result["choices"][0]["message"]["content"]


class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            message = body.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            testo = message.get("text", "")
            nome = message.get("from", {}).get("first_name", "Ospite")

            if not chat_id or not testo:
                self._ok()
                return

            if testo == "/start":
                invia_messaggio(chat_id, "Ciao! Sono l'assistente virtuale dell'appartamento. Come posso aiutarti? 😊")
                self._ok()
                return

            if testo.startswith("/"):
                self._ok()
                return

            # Notifica il proprietario
            if OWNER_ID and str(chat_id) != OWNER_ID:
                try:
                    invia_messaggio(OWNER_ID, f"📩 Messaggio da {nome} (ID: {chat_id}):\n\n{testo}")
                except Exception:
                    pass

            # Risposta AI
            try:
                info = leggi_info()
                reply = chiedi_ai(testo, info)
            except Exception:
                reply = "Mi dispiace, al momento non riesco a rispondere. Il proprietario sarà contattato al più presto!"

            invia_messaggio(chat_id, reply)

        except Exception:
            pass

        self._ok()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot attivo!")

    def _ok(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
