import os
import logging
from dotenv import load_dotenv
load_dotenv()

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
from groq import Groq
from docx import Document

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
GROQ_KEY = os.environ.get("GROQ_API_KEY")
OWNER_ID = os.environ.get("OWNER_CHAT_ID")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", 8000))

client = Groq(api_key=GROQ_KEY)


def leggi_info_appartamento() -> str:
    try:
        doc = Document("appartamento.docx")
        testo = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        return testo if testo else "Informazioni non disponibili."
    except FileNotFoundError:
        return "File appartamento.docx non trovato."
    except Exception as e:
        logger.error(f"Errore lettura docx: {e}")
        return "Errore nel leggere le informazioni."


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ciao! Sono l'assistente virtuale dell'appartamento. "
        "Come posso aiutarti? Scrivi pure la tua domanda! 😊"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message.text
    chat_id = update.effective_chat.id

    # Notifica il proprietario
    if OWNER_ID and str(chat_id) != OWNER_ID:
        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=f"📩 Messaggio da *{user.first_name}* (ID: `{chat_id}`):\n\n_{msg}_",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Notifica proprietario fallita: {e}")

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    info = leggi_info_appartamento()

    try:
        risposta = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Sei un assistente virtuale professionale per un appartamento in affitto su Booking e Airbnb. "
                        "Rispondi SOLO con le informazioni contenute nel documento qui sotto. "
                        "Se non hai l'informazione richiesta, di' cortesemente che contatterai il proprietario. "
                        "Rispondi sempre nella stessa lingua dell'ospite (italiano, inglese, francese, ecc.). "
                        "Sii cordiale e conciso.\n\n"
                        f"INFORMAZIONI APPARTAMENTO:\n{info}"
                    )
                },
                {"role": "user", "content": msg}
            ]
        )
        testo_risposta = risposta.choices[0].message.content
    except Exception as e:
        logger.error(f"Errore Groq: {e}")
        testo_risposta = (
            "Mi dispiace, al momento non riesco a rispondere. "
            "Il proprietario sarà contattato al più presto. Grazie!"
        )

    await update.message.reply_text(testo_risposta)


async def rispondi_ospite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Permette al proprietario di rispondere a un ospite.
    Uso: /rispondi <chat_id> <messaggio>
    """
    if str(update.effective_chat.id) != OWNER_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "Uso: /rispondi <chat_id_ospite> <messaggio>\n"
            "Esempio: /rispondi 123456789 Il check-in è dalle 15:00!"
        )
        return
    try:
        await context.bot.send_message(chat_id=int(args[0]), text=" ".join(args[1:]))
        await update.message.reply_text("✅ Messaggio inviato!")
    except Exception as e:
        await update.message.reply_text(f"❌ Errore: {e}")


def main():
    if not TOKEN:
        raise ValueError("TELEGRAM_TOKEN non impostato!")
    if not GROQ_KEY:
        raise ValueError("GROQ_API_KEY non impostata!")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rispondi", rispondi_ospite))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    if WEBHOOK_URL:
        logger.info(f"Avvio con webhook: {WEBHOOK_URL}/webhook")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="webhook",
            webhook_url=f"{WEBHOOK_URL}/webhook",
            drop_pending_updates=True
        )
    else:
        logger.info("Avvio in modalità polling...")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
