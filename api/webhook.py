from http.server import BaseHTTPRequestHandler
import json, os, urllib.request
from groq import Groq
from docx import Document

TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
OWNER_ID = os.environ.get("OWNER_CHAT_ID")

DOCX_PATH = os.path.join(os.path.dirname(__file__), "..", "appartamento.docx")


def invia_messaggio(chat_id, testo):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = json.dumps({"chat_id": chat_id, "text": testo}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10)


def leggi_info():
    try:
        doc = Document(DOCX_PATH)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as e:
        return "Informazioni appartamento non disponibili."


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

            # Comando /start
            if testo == "/start":
                invia_messaggio(chat_id, "Ciao! Sono l'assistente virtuale dell'appartamento. Come posso aiutarti? 😊")
                self._ok()
                return

            if testo.startswith("/"):
                self._ok()
                return

            # Notifica al proprietario
            if OWNER_ID and str(chat_id) != OWNER_ID:
                try:
                    invia_messaggio(OWNER_ID, f"📩 Messaggio da {nome} (ID: {chat_id}):\n\n{testo}")
                except:
                    pass

            # Risposta AI
            try:
                info = leggi_info()
                client = Groq(api_key=GROQ_KEY)
                risposta = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Sei un assistente virtuale per un appartamento in affitto su Booking e Airbnb. "
                                "Rispondi SOLO con le informazioni qui sotto. "
                                "Se non hai l'informazione, di' che contatterai il proprietario al più presto. "
                                "Rispondi nella stessa lingua dell'ospite. Sii cordiale e conciso.\n\n"
                                f"INFORMAZIONI APPARTAMENTO:\n{info}"
                            )
                        },
                        {"role": "user", "content": testo}
                    ]
                )
                reply = risposta.choices[0].message.content
            except Exception as e:
                reply = "Mi dispiace, al momento non riesco a rispondere. Il proprietario sarà contattato al più presto!"

            invia_messaggio(chat_id, reply)

        except Exception as e:
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
