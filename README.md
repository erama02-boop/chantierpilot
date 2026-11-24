---
title: ChantierPilot
emoji: 🏗️
colorFrom: yellow
colorTo: gray
sdk: gradio
sdk_version: 6.32.0
app_file: app.py
pinned: false
short_description: Site report and permits assistant for Lausanne
---

# ChantierPilot

**Site report and permits assistant for construction sites in the City of Lausanne.**
Course project – Applied AI and Deep Learning for Managers (HEC Lausanne, 2026–2027).

> ⚠️ Demo for a university course. Not legal advice. Use fictional notes only.

## Who it is for
Site managers (chefs de chantier) of small and mid-sized construction companies in Lausanne. At the end of the day they write or dictate a short note. ChantierPilot turns it into:

1. a **safety alert** (injury, accident or suspected asbestos), when needed;
2. a structured **daily log** (missing items marked "not mentioned");
3. a **compliance table**: which City of Lausanne, Canton of Vaud or Suva forms the note triggers, with the deadline and how to submit;
4. a **weather go / no-go** for planned outdoor work (crane, concrete, roofing…), with the next suitable day if it is a no;
5. **pre-filled form drafts** and the questions needed to complete them.

It works as a chat: you can answer its questions, correct a date or ask "can we lift with the crane on Wednesday?" and it updates the report. It answers in the language of the note (FR, EN, DE, IT, PT, ES…); official form names stay in French.

## How it works
| Component | Choice |
|---|---|
| Model | Google Gemini (gemini-3.8-flash, then 3 fallbacks), temperature 0.2 |
| Instructions | `instructions.md` (role, steps, output format, rules, security rule) |
| Knowledge | Form Library v3 (F01–F28: City of Lausanne, Canton of Vaud, Suva, SiL, tl), **injected into the context** (no retrieval) |
| Tool 1 | `calculate_deadline` – deterministic date arithmetic; notice period looked up per form |
| Tool 2 | `get_site_weather` – weather go/no-go: live Lausanne forecast (Open-Meteo, backup MET Norway), a GO / CAUTION / NO-GO verdict per activity (crane, concrete, roofing, scaffolding, excavation, facade) and the next GO day |
| Interface | Gradio chat: streams its progress and shows each tool result as it happens; follow-up questions update the report; last report downloadable as Markdown; API endpoints `/chat` and `/generate` |

**Why context injection and not RAG:** the whole library is 12 short entries (~25 KB). It fits easily in the context window, so the model always sees every form. Retrieval would add a failure mode (missing the right form) without any benefit at this size. We would switch to retrieval if the library grew to hundreds of regulations.

**Why Gemini and not Mistral (the course reference platform):** Gemini's free tier and function calling worked reliably from a plain web page during development, and the course allows any platform that meets the requirements. The architecture (instructions + context knowledge + custom function tools) is the same as a Mistral Agent with function calling.

## Limits
- Covers only the City of Lausanne (+ Vaud and Suva obligations there). Other communes get the daily log only.
- Items marked **TO VERIFY** (F10 threshold, F12 noise hours, the submission channel of F13, F16 lead time, F22 and F27 channels) are flagged as such in answers.
- Weather thresholds are indicative, not legal limits. Forecasts cover 16 days.
- The model can still misread a note: the site manager reviews every draft; the app never submits anything.

## Security
- The Gemini key is stored as a Space **secret** (`GEMINI_API_KEY`) and used only on the server.
- The note is passed as data inside `<site_note>` tags; instructions inside the note are ignored.
- Notes are capped at 5,000 characters; 30 reports per visitor per hour (env `RATE_LIMIT_PER_HOUR`).

## Hosting
Deployed free on Render (web service, free instance) from this GitHub repo; the key is the `GEMINI_API_KEY` environment variable. Optional backup: if `MISTRAL_API_KEY` is set, Mistral (`mistral-small-latest`) answers when the main Gemini models are overloaded. Free instances sleep after 15 minutes without traffic and take about a minute to wake.

## Run locally
```bash
pip install -r requirements.txt
export GEMINI_API_KEY=...   # never commit the key
python app.py
```
