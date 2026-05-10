# PIANO — Trasformazione del bot in piattaforma SaaS multi-tenant

**Autore**: Lorenzo Guzzi (con Claude)
**Versione**: 1.0
**Data**: 10 maggio 2026
**Stato**: bozza per discussione

---

## 1. Visione

Trasformare il bot attuale (oggi single-tenant per l'appartamento di Juan les Pins) in una **piattaforma SaaS** che host singoli, B&B e property manager possono attivare in 5-10 minuti per avere un assistente AI che risponde ai loro ospiti su WhatsApp e Telegram in 5+ lingue.

**Nome lavorativo**: da decidere (proposte: *Concierge.ai*, *Hostly*, *Casabot*, *Stayhub*, *PinesAI*, *Resta*).

**Posizionamento**: "Il tuo assistente virtuale per ospiti, attivo 24/7, su WhatsApp e Telegram. Setup in 5 minuti, parla 5 lingue, costa meno di un caffè al giorno."

**Target**: mercato italiano in primis (lingua + comprensione locale), espansione UE/USA in anno 2-3.

---

## 2. Stato attuale — quello che abbiamo già

### 2.1 Funzionalità tecniche
- ✅ Bot Telegram + WhatsApp Cloud API funzionanti
- ✅ AI: Claude Haiku 4.5 con prompt caching (-60-70% costi)
- ✅ Trascrizione audio via Groq Whisper (gratis)
- ✅ Foto/video automatici basati su keyword multilingua
- ✅ Memoria conversazioni persistente su GitHub
- ✅ Tracking utenti (users.json) con totale msg, lingua, topic
- ✅ Sentiment alert (3 livelli: emergenza / arrabbiato / insoddisfatto)
- ✅ Pausa AI manuale (comandi + bottoni inline)
- ✅ Reply diretto da Telegram → WhatsApp (handoff)
- ✅ Riconoscimento ospite tornato in 6 lingue
- ✅ Privacy policy autoospitata
- ✅ Dashboard web responsive con cards stile iOS
- ✅ Tab Costi (Meta + Claude stimati)
- ✅ Comandi /listamedia, /pausa, /riprendi, /dashboard, /stats, /rispondi

### 2.2 Architettura attuale
- **Hosting**: Vercel (Hobby tier free)
- **Storage**: file JSON committati su GitHub (un solo repo, tutto di Lorenzo)
- **Config**: env vars Vercel (TELEGRAM_TOKEN, WHATSAPP_TOKEN, ANTHROPIC_KEY, GITHUB_TOKEN, OWNER_CHAT_ID, ...)
- **Codice**: monolitico in `index.py`, ~3200 righe
- **Deployment**: 1 = singolo cliente (Lorenzo)

### 2.3 Limiti per la vendita
1. **Single-tenant**: ogni nuovo cliente richiederebbe deployment separato (insostenibile a 10+ clienti)
2. **Onboarding manuale**: 4 ore di setup Meta seguendo 15 step manuali
3. **Niente UI di self-service**: tutto via codice/Vercel CLI
4. **Niente billing**: zero pagamenti automatici
5. **Niente landing page**: nessuno conosce il prodotto
6. **Account-bound**: token di Lorenzo, non rivendibili

---

## 3. Stato target — dove vogliamo arrivare

### 3.1 Esperienza cliente finale
1. Scopre il prodotto via Google / passaparola / forum host Airbnb
2. Visita la landing page → capisce in 30 secondi che valore offre
3. Clicca "Inizia gratis 7 giorni"
4. Crea account (email/password)
5. Wizard di onboarding di 5 minuti:
   - **Step 1**: dati appartamento (nome, indirizzo, lingua default)
   - **Step 2**: connette WhatsApp (popup Embedded Signup → 5 min) E/O Telegram (incolla token da BotFather)
   - **Step 3**: carica info appartamento (template precompilato basato su domande tipo "Qual è il codice WiFi?")
   - **Step 4**: personalizza messaggio di benvenuto
   - **Step 5**: test "Manda un messaggio al tuo bot da un altro numero"
6. Bot live. Cliente accede alla sua dashboard `app.nomeprodotto.com/dashboard`
7. Dopo 7 giorni, paga €49/mese via Stripe

### 3.2 Esperienza Lorenzo (operatore)
- 1 deployment Vercel (o cloud più strutturato)
- 1 codebase
- N clienti, ognuno con dati isolati
- Dashboard "admin" globale per vedere tutti i tenant, fatturato, churn
- Niente intervento manuale per onboarding nuovo cliente

---

## 4. Architettura multi-tenant

### 4.1 Modello dati: tenants come "namespace"

**Decisione confermata: storage rimane su GitHub (per ora), ma con struttura tenant-scoped.**

```
repo: appartamento-bot/
├── index.py
├── PIANO.md
├── tenants/
│   ├── lorenzo-juan-les-pines/
│   │   ├── appartamento.txt
│   │   ├── users.json
│   │   ├── conversations.json
│   │   ├── bookings.json
│   │   ├── stats.json
│   │   └── daily_stats.json
│   ├── mario-roma-trastevere/
│   │   ├── appartamento.txt
│   │   └── ...
│   └── giovanna-lago-como/
│       └── ...
└── tenants.json  ← master registry, vedi sotto
```

#### Struttura `tenants.json`
```json
{
  "lorenzo-juan-les-pines": {
    "id": "lorenzo-juan-les-pines",
    "nome_appartamento": "La Terrasse Bleue",
    "creato_il": "2026-04-15T10:00:00",

    "telegram": {
      "token": "8467...",
      "owner_chat_id": "20516342",
      "bot_username": "appartamento_juan_les_pines_bot"
    },
    "whatsapp": {
      "token": "EAAYeWE...",
      "phone_id": "1051930224675694",
      "waba_id": "1476202720613528",
      "verify_token": "juanlespins2026",
      "display_number": "+39 352 046 0764",
      "bsp_provider": null
    },

    "dashboard_key": "mB68VTGUZ...",
    "lingua_default": "italian",
    "modello_ai": "claude-haiku-4-5",

    "piano": "pro",
    "stato": "attivo",
    "trial_fine": null,
    "stripe_customer_id": null,
    "stripe_subscription_id": null,

    "feature_flags": {
      "audio": true,
      "foto_auto": true,
      "sentiment_alert": true,
      "pausa_ai": true
    }
  }
}
```

#### Decisioni puntuali

| Decisione | Scelta | Motivo |
|---|---|---|
| Storage backend | GitHub (per ora) → Postgres (a 50+ tenant) | GitHub è gratis e funziona fino a ~100 tenant. Migrazione DB quando i limiti API GitHub iniziano a stringere. |
| Tenant ID format | slug `nome-citta-zona` (es. `mario-roma-trastevere`) | Leggibile, URL-safe, mai cambia |
| Cifratura token | Sì, AES-256 prima del salvataggio (chiave in env Vercel) | Sicurezza: token Meta valgono soldi |
| Schema migration | Versioning `schema_version` in tenants.json | Ogni aggiornamento del modello dati incrementa il numero |

### 4.2 Routing — come identificare il tenant

Ogni request deve sapere "a quale cliente appartiene". Strategie:

#### WhatsApp webhook
URL unico per tutti: `/whatsapp`. Identificazione tramite **`phone_number_id`** nel payload.
```python
def whatsapp_webhook():
    body = request.get_json()
    phone_id = body["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
    tenant = trova_tenant_by_phone_id(phone_id)
    if not tenant:
        return "Tenant non trovato", 404
    # ... logica standard usando tenant come contesto
```

#### Telegram webhook
URL personalizzato per tenant: `/webhook/<tenant_id>`. Lorenzo aggiunge questo URL come webhook nel proprio bot Telegram durante onboarding.
```python
@app.route("/webhook/<tenant_id>", methods=["POST"])
def webhook(tenant_id):
    tenant = carica_tenant(tenant_id)
    if not tenant:
        return "Tenant non trovato", 404
    # ... logica standard
```

#### Dashboard
URL: `/dashboard/<tenant_id>?key=<dashboard_key>`. Auth via key + verifica owner Telegram.

#### Decisioni puntuali
| Decisione | Scelta |
|---|---|
| URL webhook WhatsApp | Singolo per tutti i tenant, routing per phone_id |
| URL webhook Telegram | `/webhook/<tenant_id>`, ogni tenant ha il suo |
| Cache tenant lookup | In-memory 5 min per evitare letture GitHub continue |
| Header verifica | Aggiungiamo `X-Tenant-Verified-At` per debug |

### 4.3 Refactor codice — il "tenant context"

Pattern: ogni funzione che oggi accede a globals (`WA_TOKEN`, `_users`, ecc.) diventa un **metodo che riceve un oggetto `Tenant`**.

#### Prima
```python
WA_TOKEN = os.environ.get("WHATSAPP_TOKEN")

def wa_invia(to, testo):
    url = f"https://graph.facebook.com/v18.0/{WA_PHONE_ID}/messages"
    headers = {"Authorization": f"Bearer {WA_TOKEN}"}
    # ...
```

#### Dopo
```python
class Tenant:
    def __init__(self, config: dict):
        self.id = config["id"]
        self.wa_token = decrypt(config["whatsapp"]["token"])
        self.wa_phone_id = config["whatsapp"]["phone_id"]
        self.tg_token = decrypt(config["telegram"]["token"])
        self.tg_owner = config["telegram"]["owner_chat_id"]
        self.dashboard_key = config["dashboard_key"]
        self.lingua_default = config.get("lingua_default", "italian")
        # ... altri campi

    def wa_invia(self, to, testo):
        url = f"https://graph.facebook.com/v18.0/{self.wa_phone_id}/messages"
        headers = {"Authorization": f"Bearer {self.wa_token}"}
        # ...

    def carica_users(self):
        return carica_json_github(f"tenants/{self.id}/users.json")

    def salva_users(self, users):
        return salva_json_github(f"tenants/{self.id}/users.json", users)

# Uso:
tenant = carica_tenant(tenant_id)
tenant.wa_invia(to, "Ciao!")
users = tenant.carica_users()
```

#### Decisioni puntuali
| Decisione | Scelta |
|---|---|
| Stile refactor | Class-based `Tenant` invece di passare tenant_id ovunque |
| Caching dei dati per tenant | In-memory dict `_tenant_cache` con TTL 5 min |
| Locking per concorrenza | Lock per tenant (non globale) — 2 client che scrivono diversi tenant non si bloccano |
| Migrazione dati esistenti | Script `scripts/migrate_to_multitenant.py` esegue una volta, sposta tutti i file di Lorenzo nella sua cartella tenant |

### 4.4 Sicurezza e isolamento

| Aspetto | Approccio |
|---|---|
| Isolamento dati | Tenant può accedere SOLO ai propri file. Funzione `tenant.carica_X()` valida il path. |
| Validazione tenant_id in URL | Regex `^[a-z0-9-]+$`, max 64 char, lookup in tenants.json |
| Tentativo cross-tenant | Loggato in error log + alert a Lorenzo |
| Token cifrati | AES-256 con chiave in `MASTER_KEY` env var (mai committata) |
| Rate limit per tenant | Max 1000 richieste/ora per tenant (anti-abuso) |
| Privacy GDPR | Endpoint `/dashboard/export` per scaricare propri dati / `/delete` per cancellarsi |

---

## 5. Onboarding clienti — strategia go-to-market

### 5.1 Decisione strategica: BSP vs Tech Provider

**Decisione confermata per anno 1: partner con 360dialog.**

Motivazioni:
- Setup in **2-3 mesi** invece di 6-12 con Tech Provider
- 360dialog è europeo (GDPR-friendly, fatturazione in €)
- Embedded Signup pronto, nessun bisogno di sviluppare da zero
- Costo €49/numero/mese sostenibile se vendiamo a €69+

**Anno 2**: valutiamo migrazione a Tech Provider quando saremo a 50+ clienti.

### 5.2 Tre stadi di evoluzione onboarding

#### Stadio 1 — Manuale assistito (Mese 0-3)
- Primi **5 clienti smanettoni**
- BYOA: cliente fa setup Meta da solo seguendo guida (lo aiuto io via call)
- Prezzo: **€39/mese + €200 setup una tantum**
- Obiettivo: validare prodotto, raccogliere feedback, fissare features

#### Stadio 2 — Self-service via 360dialog (Mese 3-9)
- Integrazione Embedded Signup di 360dialog
- Cliente fa onboarding in 5-10 min senza bisogno di me
- Prezzo: **€69/mese** (include il pass-through 360dialog)
- Obiettivo: scalare a 30-50 clienti

#### Stadio 3 — Tech Provider proprio (Anno 2+)
- Verifica Meta + App Review + sviluppo Embedded Signup proprio
- Margini al massimo (~88%)
- Prezzo: rimane €69/mese, ma il margine sale da 28% a 88%
- Obiettivo: 100-500 clienti, base solida

### 5.3 Embedded Signup — flusso tecnico

Quando integreremo 360dialog (Stadio 2):

```
1. Cliente clicca "Connetti WhatsApp" sulla nostra app
2. Frontend: window.open('https://hub.360dialog.io/dashboard/app/<APP_ID>/permissions?...')
3. Cliente fa login Facebook nel popup
4. Cliente segue 5 step (verifica numero via SMS)
5. 360dialog redirige a /oauth/360dialog/callback?client=<temp_code>
6. Backend nostro: scambia il temp_code con un permanent token
7. Salviamo token nel tenants.json del cliente
8. Configuriamo automaticamente il webhook (puntando al nostro server)
9. Cliente vede "Bot attivato!" e procede con info appartamento
```

Tempo cliente: **5-10 minuti**. Confronto: setup manuale che ho fatto io: **4 ore**.

---

## 6. Stack tecnico — decisioni puntuali

### 6.1 Hosting
| Aspetto | Scelta | Motivo |
|---|---|---|
| Backend | Vercel (Hobby per ora, Pro a $20/mese a 100+ tenant) | Gratuito, deploy Git automatico, scaling auto |
| Frontend (landing + dashboard) | Stesso Vercel, app Next.js separata | Stack moderno, SEO buono, ottimo DX |
| Storage dati | GitHub fino a ~50 tenant, poi Postgres (Neon/Supabase) | Gradualità, niente over-engineering iniziale |
| File statici (logo, immagini) | Vercel Blob o Cloudflare R2 | Veloce e a basso costo |

### 6.2 AI providers
| Use case | Provider | Modello |
|---|---|---|
| Risposta principale | Anthropic | claude-haiku-4-5 (con prompt caching) |
| Fallback se Claude fail | Groq | llama-3.3-70b-versatile |
| Trascrizione audio | Groq | whisper-large-v3-turbo (gratis tier) |
| Riorganizzazione testo | Anthropic | claude-haiku-4-5 |

### 6.3 Pagamenti
- **Stripe** per gestione abbonamenti
- Webhook Stripe → aggiorna stato tenant in `tenants.json`
- Trial 7 giorni senza carta richiesta
- Piano Starter (€39), Pro (€69), Business (€149)

### 6.4 Email transazionali
- **Resend** (€0 fino a 3000/mese, poi €20/mese fino a 50k)
- Email: signup, trial scadente, pagamento riuscito/fallito, alert importanti

### 6.5 Monitoring / errori
- **Sentry** (free tier 5k eventi/mese)
- Cron health check per ogni tenant (manda msg di test → verifica risposta)

### 6.6 Authentication
- **Clerk** o **Supabase Auth** per login utenti finali (non Lorenzo)
- Magic link + email/password
- Session via cookie HttpOnly

### 6.7 Domini
- `nomeprodotto.com` — landing
- `app.nomeprodotto.com` — dashboard cliente
- `appartamento-bot.vercel.app` — bot endpoint (rimane)
- Domini custom per cliente (es. `bot.lacasadigiovanni.com`) — Stadio 3

---

## 7. Roadmap di implementazione

### Fase 0 — Cosa abbiamo oggi ✅ FATTO
Bot single-tenant funzionante con tutte le funzioni descritte in §2.

### Fase 1 — Multi-tenancy interno (2-3 settimane)
**Obiettivo**: codebase pronta per più tenant, ma solo Lorenzo come tenant attivo.

#### Task
1. **Crea `tenants.json` master** con la config attuale di Lorenzo migrata
2. **Crea cartella `tenants/lorenzo-juan-les-pines/`** e sposta tutti i file dati
3. **Refactor**: classe `Tenant`, sostituire `_users`, `WA_TOKEN`, ecc.
4. **Refactor webhooks**: identificare tenant da `phone_number_id` (WhatsApp) e da URL (`/webhook/<tenant_id>` per Telegram)
5. **Refactor dashboard**: `/dashboard/<tenant_id>?key=...`
6. **Encryption layer** dei token in `tenants.json`
7. **Migrazione dati live**: 1 commit che muove i file. Test approfondito.
8. **Test**: bot di Lorenzo continua a funzionare identico

**Deliverable**: deployment con multi-tenant attivo, ma 1 solo tenant. Niente impatto utente.

### Fase 2 — Aggiunta secondo tenant manuale (1 settimana)
**Obiettivo**: validare l'isolamento aggiungendo un tenant di test.

#### Task
1. Crea `tenants/test-demo/` con dati finti
2. Compila `tenants.json` con tenant test (uso il Test WhatsApp number Meta originale)
3. Test cross: messaggi al numero di Lorenzo non finiscono nel tenant test
4. Test dashboard isolata
5. Documenta procedura "manuale" per aggiungere tenant (sarà la guida Stadio 1)

**Deliverable**: 2 tenant convivono, dati isolati al 100%.

### Fase 3 — Onboarding wizard manuale (2 settimane)
**Obiettivo**: pagina web dove cliente inserisce i propri token e si crea tenant.

#### Task
1. Sviluppa `app.nomeprodotto.com` con Next.js
2. Form: email + password + dati appartamento + token Telegram + token WhatsApp + phone_id + waba_id + verify_token
3. Backend: valida token (chiamata test API), salva tenant, crea cartella, manda email benvenuto
4. Pagina dashboard cliente embed di `/dashboard/<tenant_id>`
5. Stripe Checkout per piano scelto

**Deliverable**: clienti tech-savvy possono iscriversi senza il mio intervento.

### Fase 4 — Integrazione 360dialog Embedded Signup (3-4 settimane)
**Obiettivo**: cliente non-tech può connettere WhatsApp in 5 minuti.

#### Task
1. Account 360dialog Partner (€500 setup + €49/mese minimo)
2. Sviluppa flusso OAuth Embedded Signup (vedi §5.3)
3. Modifica wizard onboarding: pulsante "Connetti WhatsApp" apre popup 360dialog
4. Backend: ricevi webhook 360dialog con permanent token
5. Test end-to-end con account demo

**Deliverable**: clienti aprono account WhatsApp dal nostro sito senza dover toccare Meta.

### Fase 5 — Landing + marketing (2-3 settimane)
**Obiettivo**: avere visibilità.

#### Task
1. Landing page `nomeprodotto.com` con: value proposition, demo video, prezzi, testimonials
2. Blog (1 articolo SEO/settimana per primi 3 mesi)
3. Setup Google Analytics, Hotjar
4. Lancio su Product Hunt, gruppi Facebook host Airbnb italiani
5. Outreach diretto a 50 property manager italiani

**Deliverable**: traffico organico inizia, primi 5-10 signup.

### Fase 6 — Billing + retention (2 settimane)
**Obiettivo**: chiudere il loop economico.

#### Task
1. Stripe Subscriptions con webhook (subscription.created, .updated, .canceled)
2. Logica fatturazione: free trial 7gg → autocharge
3. Email automatiche: trial scadente, pagamento fallito, downgrade dopo cancellazione
4. Pagina "billing" nella dashboard cliente
5. Dunning (retry pagamenti falliti)

**Deliverable**: ricavi automatici, niente intervento manuale.

### Fase 7 — Tech Provider (Anno 2)
Migrazione da 360dialog a relazione diretta con Meta. Vedi §5.1 per dettagli.

---

## 8. Modello economico

### 8.1 Pricing piani

| Piano | Prezzo/mese | Target | Limiti | Caratteristiche |
|---|---|---|---|---|
| **Starter** | €29 | Host singolo | 1 appartamento, 500 msg/mese | Solo Telegram OPPURE WhatsApp, 1 lingua, no audio |
| **Pro** ⭐ | €69 | Host avanzato | 1 appartamento, msg illimitati | Telegram + WhatsApp, 5 lingue, audio, foto auto, dashboard |
| **Business** | €149 | Property manager | Fino a 5 appartamenti | Tutto del Pro + sentiment alert + pausa AI + multi-property dashboard |
| **Enterprise** | da €499 | Agenzie | Illimitato | White label, API, integrazioni PMS (Smoobu, Hostaway), SLA |

**Trial gratuito 7 giorni** su tutti i piani. Sconto annuale -20%.

### 8.2 Costi marginali per cliente (piano Pro €69)

| Voce | Costo/mese |
|---|---|
| Claude Haiku API (con cache) | ~€0,80 |
| Groq Whisper (audio) | €0,00 (free tier) |
| Vercel + GitHub | ~€0,30 (a regime, ammortizzato) |
| 360dialog | €49,00 |
| Stripe (1.5% + €0.25) | ~€1,30 |
| Email (Resend) | ~€0,10 |
| **Totale costo** | **~€51,50** |
| **Margine** | **€17,50 (25%)** |

### 8.3 Proiezione ricavi 24 mesi

| Mese | Clienti | MRR (€) | Costi (€) | Margine (€) | Note |
|---|---|---|---|---|---|
| 1-3 | 5 | 195 | 70 | 125 | Stadio 1, BYOA, primi clienti |
| 4-6 | 15 | 1.035 | 770 | 265 | Stadio 2, 360dialog integrato |
| 7-12 | 50 | 3.450 | 2.575 | 875 | Crescita organica + outreach |
| 13-18 | 120 | 8.280 | 6.180 | 2.100 | Word of mouth, content marketing |
| 19-24 | 250 | 17.250 | 12.875 | 4.375 | (Tech Provider migration parte qui) |

**Anno 1 totale**: ~€25k ricavi, ~€7k margine
**Anno 2 totale**: ~€140k ricavi, ~€40k margine
**Anno 3 (con Tech Provider, margini su)**: target €350k ricavi, ~€220k margine

### 8.4 Investimenti necessari

| Voce | Costo |
|---|---|
| Sviluppo Fasi 1-6 (3-4 mesi) | Tempo Lorenzo + €0-3000 freelance design |
| 360dialog setup | €500 |
| Domini + email | ~€100/anno |
| Marketing iniziale (Ads, content) | €2000-5000 |
| Legali (terms, privacy multi-tenant) | €500-1500 |
| **Totale investimento iniziale** | **~€3.000-10.000 + 4 mesi tempo** |

**Break-even** stimato: mese 7-9 (dipende da velocità acquisizione clienti).

---

## 9. Rischi e mitigazioni

| Rischio | Probabilità | Impatto | Mitigazione |
|---|---|---|---|
| Meta cambia regole API | Alta | Alto | Restare aderenti a documentazione, monitorare release notes settimanali |
| 360dialog aumenta prezzi | Media | Alto | Contratto con clausole, piano B con altri BSP (Wati, Twilio) |
| Concorrente con AI peggiore ma marketing meglio | Alta | Medio | Differenziarsi su qualità, recensioni, italianità |
| Acquisizione clienti più lenta del previsto | Alta | Medio | Pivot pricing, partnership con property manager, content SEO |
| Rate limit GitHub API | Media | Medio | Migrazione a Postgres a 50+ tenant (già pianificata) |
| Bug rompe bot di tutti i clienti | Bassa | Critico | Test automatici, deploy graduale (canary 10% tenant prima), rollback < 1 min |
| Token cifrati persi (chiave persa) | Bassa | Critico | Master key in vault Vercel + backup encrypted in 1Password personale |
| GDPR audit | Media | Alto | Privacy policy multi-tenant fatta da legale, log di accessi, possibilità di export/cancellazione |
| Lorenzo si stufa / cambia priorità | Media | Critico | Documentare TUTTO, scrivere onboarding interno, valutare partner tecnico |

---

## 10. Decisioni aperte da prendere

Cose che NON decidiamo ora ma su cui dovremo pronunciarci:

1. **Nome del prodotto** — entro Fase 5 (landing). Brainstorm aperto.
2. **Forma giuridica** — quando si comincia a fatturare → P.IVA o SRL? Servirà commercialista.
3. **Sito separato vs subdomain** — `nomeprodotto.com` o `nomeprodotto.it`?
4. **Localizzazione UI** — solo italiano o anche EN/FR? Suggerisco IT solo per anno 1.
5. **Supporto cliente** — solo email? Chat live? Telegram support group?
6. **Documentazione** — Notion pubblico, GitBook, blog?
7. **Affiliazione/referral program** — paga 20% del primo anno a chi porta clienti? Decidere a 50+ clienti.
8. **Integrazioni PMS** — Smoobu, Hostaway, Lodgify. Quale priorità? Dipende da feedback primi clienti.
9. **Cosa togliere dal piano Starter** per spingere upgrade? Decidere dopo primi 10 clienti reali.

---

## 11. Prossimi passi concreti (in ordine)

Quando sarai pronto a partire (anche tra qualche settimana, non c'è fretta):

1. **Decidere il nome** del prodotto (anche provvisorio)
2. **Aprire account 360dialog** Partner (puoi farlo sul loro sito, valutazione 1-2 settimane)
3. **Avviare Fase 1**: io implemento il multi-tenancy interno. Tempi 2-3 settimane part-time.
4. **In parallelo**: tu cominci a parlare con 5 property manager o host che conosci → "Sto sviluppando questo prodotto, sei interessato a essere beta tester gratis per 3 mesi?"
5. **Fine Fase 1**: deployment con tutte le features attuali ma multi-tenant ready
6. **Fase 2-3 in parallelo**: aggiungo onboarding manuale + iscrivo i primi 5 beta tester
7. **Mese 3**: feedback raccolto, sviluppo Fase 4 (360dialog) per scaling

---

## 12. Note finali

Questo piano è **vivente** — lo aggiorneremo man mano che impariamo dalle prime conversazioni con i clienti reali. La cosa più importante è non perdere tempo a fare in 6 mesi quello che si valida in 2 settimane parlando con 5 host.

Le ipotesi più rischiose da testare presto:
- Gli host pagheranno €69/mese? (hp: sì, validare con 5 interviste prima di scrivere codice)
- Quanti messaggi/mese gestisce il bot in media? (impatta costi marginali)
- Quanto è grande il problema "rispondo agli ospiti tutto il giorno"? (validare con interviste)

Il prodotto tecnicamente funziona già. Il rischio reale è **commerciale**, non tecnico.

---

## Glossario rapido

- **Multi-tenant**: una sola applicazione che serve molti clienti con dati isolati
- **BSP** (Business Solution Provider): aziende certificate Meta che rivendono accesso WhatsApp Cloud API
- **Embedded Signup**: flusso ufficiale Meta per onboardare cliente WhatsApp in 5 min
- **Tech Provider**: status Meta che ti permette di fare Embedded Signup senza BSP intermedio
- **WABA**: WhatsApp Business Account, l'oggetto Meta che contiene 1+ numeri di telefono
- **MRR**: Monthly Recurring Revenue, ricavi mensili ricorrenti
- **PMS**: Property Management System (Smoobu, Hostaway, ecc.)

---

*Fine piano. Versione 1.0. Da iterare.*
