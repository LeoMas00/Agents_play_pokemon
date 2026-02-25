# LLM Pokemon Scaffold - Extended Edition

![LLM Pokemon Scaffold](https://res.cloudinary.com/lesswrong-2-0/image/upload/c_scale,w_250/f_auto,q_auto/v1/mirroredImages/8aPyKyRrMAQatFSnG/fcqugqcpuloqkloqz9bw)

Toolkit per far giocare a Pokemon Red agli LLM tramite emulatore PyBoy, con un sistema **dual-agent** (Esploratore + Allenatore), memoria RAG, strumenti di navigazione avanzati e UI in tempo reale.

Riferimento pubblico: [Research Notes: Running Claude 3.7, Gemini 2.5 Pro, and o3 on Pokemon Red](https://www.lesswrong.com/posts/8aPyKyRrMAQatFSnG).

## Crediti

Questo progetto deriva dal codice iniziale di David Hershey (Anthropic):
https://github.com/davidhershey/ClaudePlaysPokemonStarter/tree/main

## In breve

La versione originale offriva un agente semplice, lettura memoria e controllo base dell'emulatore. Questa versione estesa introduce:

- **Dual-agent**: Esploratore (navigazione, esplorazione) e Allenatore (combattimenti, party), chiamati in parallelo ogni step.
- **Memoria RAG** (note + riassunti) persistente in `rag/memory_summary.json`.
- **Mappa di collisione ASCII** aggiornata in tempo reale con pathfinding e etichette.
- **UI console** che legge `ui/state.json` per obiettivi, messaggi, tool, token e opinioni in tempo reale.

Supporta modelli OpenAI-compatibili (endpoint e modelli configurabili per Esploratore, Allenatore e Summarizer).

---

## Funzionalità e caratteristiche

### Sistema dual-agent

- **Esploratore (Explorer)**: esplora il mondo, naviga, etichetta luoghi, usa la mappa. Riceve RAM estesa (nome giocatore, rival, mosse valide, badge, dialoghi).
- **Allenatore (Trainer)**: decide in combattimento e nella gestione del party. Si concentra su obiettivi di progressione.
- Ogni step: uno snapshot condiviso (screenshot, RAM, mappa, RAG, obiettivi, opinioni) viene inviato a **entrambi** gli agenti; le chiamate API sono **parallele**; le risposte vengono applicate in ordine di arrivo (per ridurre latenza).
- **Obiettivi separati**: ogni agente può impostare il proprio obiettivo (tool `objective`); entrambi vedono gli obiettivi dell’altro.
- **Opinioni tra agenti**: tool `opinion` per inviare consigli/avvisi all’altro agente; le opinioni compaiono nel payload del turno successivo.

### Strumenti (tools) per gli LLM

| Tool | Descrizione |
|------|-------------|
| `press_buttons` | Sequenza di tasti (a, b, start, select, up, down, left, right). |
| `navigate_to` | Pathfinding automatico verso una cella **visibile** sullo schermo (solo overworld). |
| `navigate_to_offscreen_coordinate` | Pathfinding verso coordinate **fuori schermo** usando la mappa espansa (DIRECT_NAVIGATION). |
| `bookmark_location_or_overwrite_label` | Etichetta una posizione (es. "Scale primo piano", "Entrata zona X") per riferimento futuro. |
| `mark_checkpoint` | Segna un traguardo raggiunto; resetta il contatore di step e il location tracker (riduce allucinazioni). |
| `remember_note` | Salva una nota in memoria RAG (testo + tag opzionali). |
| `delete_remember_note` | Rimuove note dalla RAG (per timestamp o testo esatto, con `confirm: true`). |
| `opinion` | Invia un’opinione all’altro agente (explorer ↔ trainer). |
| `objective` | Imposta o aggiorna l’obiettivo (explorer/trainer). |
| `detailed_navigator` | In modalità dual-agent non è attivo (risposta fissa). |

### Navigazione e mappa

- **Mappa ASCII di collisione** (`LocationCollisionMap`): griglia 9×10 centrata sul giocatore, espansa man mano che ci si sposta; numeri **StepsToReach** per la distanza in passi dal player; tile percorribili, ostacoli, sprite e posizione giocatore.
- **Pathfinding**: `find_path` per celle visibili; per coordinate offscreen viene usata la mappa piena e `generate_buttons_to_coord` (sequenza di direzioni).
- **Etichette**: manuali (tool `bookmark_location_or_overwrite_label`) e automatiche (es. "Entrance to &lt;location&gt; (Approximate)" al cambio area). Dopo molti step senza cambio location le etichette non "approximate" possono essere ripulite per evitare label errate.
- **Location tracker**: dopo 50 step dall’ultimo checkpoint si attiva il tracciamento delle celle visitate (evidenziate sullo screenshot); utile in labirinti.
- **Screenshot annotati**: griglia 10×9, coordinate (col,row), rettangoli per ostacoli (rosso) e sprite (giallo), etichette e celle già visitate (cyan) quando il tracker è attivo.

### Memoria RAG (rag/memory_summary.json)

- **Note**: liste di note con `timestamp`, `text`, `tags`, `agent`; gli agenti le usano con `remember_note` / `delete_remember_note`.
- **Messages**: riassunto dello stato di gioco prodotto da un modello di summarization (JSON con struttura definita dal prompt).
- **Objectives**: obiettivi `explorer` e `trainer` salvati qui e ricaricati all’avvio (persistenza tra interruzioni).
- **Summarization**: quando la history di uno dei due agenti supera `max_history`, viene chiamato il client Summarize (configurabile in `config.py`); l’output aggiorna i "messages" nel RAG e le history vengono troncate (ultimi N messaggi conservati).

### Reasoning e controllo stato

- **Checkpoint**: `mark_checkpoint` registra un traguardo e resetta step/label reset; i checkpoint recenti sono inclusi nel payload e nel prompt di summarization.
- **Log prime visite**: ogni nuova location viene registrata con step assoluto e scritta in `location_milestones.txt`.
- **Meta-Critique** (prompts in `prompts.py`): ordinamento dei fatti per affidabilità e cleanup dei riassunti; usato nella pipeline di summarization/ragionamento dove configurato.

### Emulatore (PyBoy)

- **Thread separato**: l’emulatore gira in un thread dedicato (o nel main thread su macOS con `--main-thread-target emulator`) per non bloccare durante le chiamate LLM.
- **PriorityLock**: accesso thread-safe alla coda dei comandi e allo stato PyBoy.
- **Salvataggio/caricamento**: `save_state` / `load_state` su file (es. `save.state`); le location e le etichette restano in `locations.pkl` (archive separato).

### Lettura RAM (memory_reader)

- Location, coordinate (col, row), dialoghi, nome giocatore e rival, badge, mosse valide.
- Stato combattimento, tileset, specie Pokémon, tipi, condizioni di stato (sleep, poison, burn, freeze, paralysis), ecc.
- Usata per costruire lo snapshot RAM inviato agli agenti e per la mappa/percorsi.

### UI opzionale (console dual-agent)

- **ui/state.json**: scritto periodicamente dal coordinator; contiene timestamp, `last_summary_time`, obiettivi (explorer/trainer), per ogni agente messaggi/tool/token usage, e liste di opinioni.
- **ui/index.html**: dashboard stile Twitch con font Space Grotesk/IBM Plex Mono, pannelli per obiettivi, messaggi, tool calls, token e opinioni; legge `state.json` (es. tramite refresh o polling).
- **ui/agent-images/**: immagini per Esploratore e Allenatore (explorer.jpg, trainer.jpg).

### Qualità della vita

- **--max-history**: numero massimo di messaggi in history prima di summarization (default 30).
- **--main-thread-target**: `emulator` | `agent` | `auto` (su macOS default `auto` usa il main thread per PyBoy).
- Salvataggio automatico ogni N step: `save.state`, `locations.pkl`, `location_milestones.txt`.
- Interruzione con Ctrl+C salva di solito lo stato; backup manuali consigliati per evitare corruzione se l’interrupt avviene durante scrittura.

### Funzionalità NON incluse

- Gestione completa dei file di salvataggio di gioco oltre allo stato dell’emulatore (save.state / locations.pkl).

## Requisiti

- **Python 3.11** consigliato (versioni superiori possono dare problemi con PyBoy).
- ROM di Pokemon Red (a tua cura) nella root o percorso indicato con `--rom`.

## Setup

1. Clona il repository.
2. Installa le dipendenze:

   ```bash
   pip install -r requirements.txt
   ```

   (anthropic, pyboy, Pillow, numpy, google-genai, openai, ollama.)

3. Crea `secret_api_keys.py` nella root con le chiavi per i client OpenAI-compatibili:
   - `API_OPENAI_EXPLORER`
   - `API_OPENAI_TRAINER`
   - `API_OPENAI_SUMMARIZE`
4. Configura in `config.py` i modelli e le `BASE_URL` per Explorer, Trainer e Summarizer.
5. Metti la ROM di Pokemon Red nella root (es. `pokemon.gb`) o specifica il percorso con `--rom`.

## Uso

Configura in `config.py` gli endpoint e i modelli per Esploratore, Allenatore e Summarizer (e, se usi, le API key in `secret_api_keys.py`), poi avvia:

```bash
python main.py
```

Argomenti da riga di comando:

- `--rom`: percorso della ROM (default: `pokemon.gb`)
- `--steps`: numero di step (default: `10`)
- `--display`: abilita finestra dell’emulatore (non headless)
- `--sound`: abilita audio (solo con `--display`)
- `--load-state`: percorso di un file stato emulatore (es. `save.state`) da caricare all’avvio  
  Nota: l’archivio location/etichette resta in `locations.pkl`.
- `--max-history`: messaggi massimi in history prima della summarization (default: `30`)
- `--main-thread-target`: `emulator` | `agent` | `auto` — su quale thread far girare PyBoy (default: `auto`; su macOS di solito si usa il main thread per l’emulatore)

Esempi:

```bash
python main.py --rom pokemon.gb --steps 20000 --display --sound
python main.py --rom pokemon.gb --steps 20000 --display --sound --load-state save.state
```

### UI opzionale (console dual-agent)

```bash
python -m http.server 8081 --directory ui
```

Apri http://localhost:8081/ nel browser. L'interfaccia legge ui/state.json.

Nota: l'interruzione con tastiera salva di solito in automatico, ma se avviene durante la scrittura dello stato puo corrompere i file. Consigliati backup manuali.

## Dettagli implementativi

### Componenti principali

- **main.py**: entry point; crea `DualAgentCoordinator` e avvia il loop.
- **agent/dual_agent.py**: coordinatore dual-agent (Explorer + Trainer), costruzione snapshot, chiamate API in parallelo, gestione tool, RAG, UI state, summarization, salvataggio.
- **agent/simple_agent.py**: `LocationCollisionMap`, `TextDisplay`, logica mappa/etichette e utility condivise; non è più l’agente “principale” (ora è il dual-agent).
- **agent/emulator.py**: wrapper PyBoy, coda comandi, thread player, `find_path`, `get_screenshot`, `get_state_from_memory`, `save_state`/`load_state`, `get_in_combat`.
- **agent/memory_reader.py**: lettura RAM (location, coordinate, dialoghi, badge, party, Pokémon, stati, ecc.).
- **agent/prompts.py**: prompt di sistema per Explorer, Trainer e Summarizer (OpenAI e varianti).
- **agent/tool_definitions.py**: definizione strumenti (OpenAI/Google format), OPENAI_TOOLS, NAVIGATOR_TOOLS.
- **agent/utils.py**: conversioni tool (OpenAI ↔ Google), utility.
- **config.py**: modelli e base URL per Explorer, Trainer, Summarizer; `MODEL`, `TEMPERATURE`, `DIRECT_NAVIGATION`, ecc.
- **secret_api_keys.py**: API key per Explorer, Trainer, Summarizer (non in repo).

### Flusso di esecuzione (dual-agent)

1. Lettura stato: screenshot, RAM, location, coordinate.
2. Aggiornamento mappa di collisione (se overworld) e eventuale location tracker.
3. Costruzione messaggio utente per Explorer e Trainer (testo + screenshot annotato + RAG + obiettivi + opinioni).
4. Chiamate **parallele** ai due modelli con i rispettivi prompt e tool.
5. Risposte ordinate per tempo; per ogni agente: append messaggio assistant, esecuzione tool calls, append tool results.
6. Se una history supera `max_history`: chiamata al modello di summarization, scrittura RAG, troncamento history.
7. Aggiornamento obiettivi/etichette automatiche, salvataggio periodico (stato, locations, milestones), scrittura `ui/state.json`.

## Licenza

Questo progetto e rilasciato sotto GNU GPL v3.0. Vedi LICENSE per i dettagli.
La GPLv3 e copyleft: qualsiasi derivato non solo personale deve restare open source.
Il progetto originale di David Hershey (Anthropic) era pubblicato senza licenza.

## Overlay UI per streaming (OBS / Twitch)

La **dashboard principale** per stato in tempo reale è `ui/index.html` (legge `ui/state.json`). Per lo streaming è possibile usare una overlay con pannelli per Obiettivi e Opinioni, spazio per il video e indicatori tasti. Se presenti in `ui/`, i file sono:

- `ui/overlay.html` — HTML dell'overlay
- `ui/overlay.css` — stili moderni e responsive
- `ui/overlay.js` — comportamento: autoscroll demo, API `postMessage`, feedback tasti

Come usarla in preview o con OBS:

1. Avvia un semplice server nella root del progetto:

```bash
python -m http.server 8000
```

2. Apri in browser o aggiungi come Browser Source in OBS l'URL:

```
http://<HOST>:8000/ui/overlay.html
```

3. Opzioni utili in OBS:
- imposta larghezza/altezza della Browser Source a risoluzione dello streaming (es. 1280x720)
- disabilita "Shutdown source when not visible" se vuoi che l'overlay continui a ricevere eventi

4. Per mostrare il video direttamente nell'overlay, invia un messaggio postMessage dal tuo controller (es. console del browser o script) con `{type:'setVideo', src: '<url_video>'}`. `overlay.js` espone anche funzioni di comodo su `window.StreamOverlay`.

Esempio via console del browser:

```js
window.postMessage(JSON.stringify({type:'addObjective', text:'Nuovo obiettivo: visita la città'}), '*')
window.postMessage(JSON.stringify({type:'setVideo', src:'https://example.com/stream.mp4'}), '*')
```

## Riepilogo comandi

Breve elenco dei comandi e degli script utili nel progetto.

- Installazione dipendenze:

```bash
pip install -r requirements.txt
```

- Avviare l'app principale:

```bash
python main.py
```

- Esempi di avvio con opzioni:

```bash
python main.py --rom pokemon.gb --steps 20000 --display --sound
python main.py --rom pokemon.gb --steps 20000 --display --sound --load-state save.state
```

- Avviare la UI statica (per debug / OBS Browser Source):

```bash
python -m http.server 8081 --directory ui
```

- Preview overlay locale (porta 8000):

```bash
python -m http.server 8000
# apri http://localhost:8000/ui/overlay.html
```

- Esempi `postMessage` per aggiornare l'overlay (browser console o script esterno):

```js
// aggiunge un obiettivo
window.postMessage(JSON.stringify({type:'addObjective', text:'Cattura un Pokémon di tipo Fuoco'}), '*')

// aggiunge una opinione/commento
window.postMessage(JSON.stringify({type:'addOpinion', text:'Buona scelta, tieni le difese alte'}), '*')

// imposta sorgente video (se il browser può riprodurla)
window.postMessage(JSON.stringify({type:'setVideo', src:'https://example.com/clip.mp4'}), '*')
```

- API console esposte per debug nella pagina overlay:

```js
// dall'interno della pagina overlay
window.StreamOverlay.addObjective('Test obiettivo')
window.StreamOverlay.addOpinion('Test opinione')
window.StreamOverlay.setVideo('https://example.com/clip.mp4')
```

Se vuoi, posso aggiungere screenshot dell'overlay, opzioni per cambiare palette o un piccolo script per inviare eventi da command-line (curl/node). Dimmi quale preferisci.
