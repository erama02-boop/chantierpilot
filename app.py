"""ChantierPilot v2 – server-side version (Gradio).

What changed from v1 (index.html):
- The Gemini API key is read from a Space secret (GEMINI_API_KEY) on the server.
  It is never sent to the browser.
- The instructions + form library live in instructions.md (edit without touching code).
- calculate_deadline looks up the notice period from the form ID itself, and rejects
  impossible dates such as 2026-02-30.
- get_site_weather rejects past dates (Open-Meteo would return historical data).
- Notes are capped in length, wrapped as data (prompt-injection defence),
  and each visitor is rate-limited.
- The report can be downloaded as a Markdown file.
"""

import json
import re
import os
import tempfile
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import gradio as gr
import requests

# ---------------------------------------------------------------- configuration
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODELS = ["gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite"]
INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text(encoding="utf-8")
MAX_NOTE_CHARS = 5000          # longer notes are refused
MAX_TOOL_ROUNDS = 8            # same limit as v1
RATE_LIMIT = (int(os.environ.get("RATE_LIMIT_PER_HOUR", "30")), 3600)  # reports per visitor per hour
TIMEOUT_S = 45
TZ = ZoneInfo("Europe/Zurich")

# Notice periods (calendar days before the start date). Single source of truth:
# the model no longer chooses this number.
NOTICE_DAYS = {"F01": 2, "F02": 10, "F05": 14, "F08": 14}
KNOWN_FORMS = {f"F{i:02d}" for i in range(1, 13)}

NUDGE = (
    "Check before finishing: your answer flags forms but you did not call calculate_deadline. "
    "Call it now for every flagged form that has a start, felling, removal or occupation date "
    "(estimate the earliest date if vague). Then rewrite your COMPLETE answer, using exactly the deadline, "
    "days left and priority the tool returns, in the language of the site note. "
    "If no flagged form has any date, answer again unchanged."
)

EXAMPLE = (
    "Chantier Avenue de Cour 48, Lausanne. Démolition de la cuisine au 2e étage terminée. "
    "En enlevant le faux plafond, l'équipe a trouvé des plaques en fibrociment qui pourraient "
    "contenir de l'amiante, travaux arrêtés dans cette zone. Demain on fait venir un camion-grue "
    "qui devra se mettre sur le trottoir devant l'immeuble de 7h à 11h. Un ouvrier s'est coupé "
    "la main avec une meuleuse, soigné sur place, il a repris le travail."
)


# ---------------------------------------------------------------- tools
def _parse_date(value):
    """Strict YYYY-MM-DD parsing: rejects '2026-02-30', 'next week', etc."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def calculate_deadline(form_id=None, report_date=None, work_start_date=None, **_ignored):
    """Filing deadline for a form. Labelled values, and an error instead of a crash."""
    form_id = str(form_id or "").strip().upper()
    if form_id not in KNOWN_FORMS:
        return {"form_id": form_id or "unknown", "error": "Unknown form ID. Use an ID from the library (F01 to F12)."}
    today = _parse_date(report_date)
    if today is None:
        return {"form_id": form_id, "error": "report_date is missing or not a valid date (YYYY-MM-DD)."}
    start = _parse_date(work_start_date)
    if start is None:
        return {"form_id": form_id, "error": "work_start_date is missing or not a valid date (YYYY-MM-DD). Ask the site manager for the exact start date."}
    notice = NOTICE_DAYS.get(form_id, 0)
    deadline = start - timedelta(days=notice)
    days_left = (deadline - today).days
    priority = "🔴 URGENT" if days_left <= 2 else "🟠 SOON" if days_left <= 14 else "🟢 PLANNED"
    status = f"Deadline passed {-days_left} days ago" if days_left < 0 else f"{days_left} days left"
    return {
        "form_id": form_id,
        "notice_days_used": notice,
        "deadline_date": deadline.isoformat(),
        "days_left": days_left,
        "priority": priority,
        "status": status,
    }


def get_site_weather(date=None, activity="general", **_ignored):
    """Live Lausanne forecast for one day (Open-Meteo, free, no key) + indicative flags."""
    activity = activity or "general"
    day = _parse_date(date)
    if day is None:
        return {"error": "date is missing or not a valid date (YYYY-MM-DD)."}
    today = datetime.now(TZ).date()
    if day < today:
        return {"date": day.isoformat(), "error": "This date is in the past, so there is no forecast. Weather is only checked for planned work."}
    if day > today + timedelta(days=15):
        return {"date": day.isoformat(), "error": "Forecasts only cover the next 16 days. Check again closer to the date (e.g. MeteoSwiss)."}
    url = (
        "https://api.open-meteo.com/v1/forecast?latitude=46.5197&longitude=6.6323"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max"
        f"&timezone=Europe%2FZurich&start_date={day}&end_date={day}"
    )
    try:
        r = requests.get(url, timeout=15)
        j = r.json()
    except Exception:
        return {"date": day.isoformat(), "error": "Weather service not reachable right now. Check the forecast manually (e.g. MeteoSwiss)."}
    if not r.ok or j.get("error"):
        return {"date": day.isoformat(), "error": f"Weather not available for this date: {j.get('reason', r.status_code)}"}
    d = j.get("daily", {})

    def v(key):
        vals = d.get(key) or [None]
        return vals[0]

    w = {
        "date": day.isoformat(), "activity": activity, "location": "Lausanne",
        "temp_min_c": v("temperature_2m_min"), "temp_max_c": v("temperature_2m_max"),
        "rain_mm": v("precipitation_sum"),
        "wind_max_kmh": v("wind_speed_10m_max"), "gusts_max_kmh": v("wind_gusts_10m_max"),
    }
    if w["temp_min_c"] is None and w["gusts_max_kmh"] is None:
        return {"date": day.isoformat(), "error": "Weather service returned no data for this date."}
    flags, g, tmin, tmax, rain = [], w["gusts_max_kmh"], w["temp_min_c"], w["temp_max_c"], w["rain_mm"]
    if g is not None and g >= 50:
        flags.append(f"Strong gusts ({g} km/h): check the wind limits of cranes and work at height before starting")
    elif g is not None and g >= 35 and activity in ("crane", "roofing", "scaffolding"):
        flags.append(f"Moderate gusts ({g} km/h): monitor wind during lifting / work at height")
    if tmin is not None and tmin < 5:
        flags.append(f"Cold (min {tmin} °C): concrete may need cold-weather measures (heating, covers)")
    if tmin is not None and tmin <= 0:
        flags.append(f"Frost risk (min {tmin} °C): slippery surfaces, protect fresh concrete")
    if rain is not None and rain >= 5:
        flags.append(f"Rain ({rain} mm): plan protection for concrete, roofing and excavations")
    if tmax is not None and tmax >= 30:
        flags.append(f"Heat (max {tmax} °C): hydration breaks, fast concrete setting")
    w["flags"] = flags or ["No weather concerns flagged"]
    w["note"] = "Indicative thresholds only, not legal limits. The equipment manufacturer's limits and the site's safety plan always apply."
    return w


TOOLS = {"calculate_deadline": calculate_deadline, "get_site_weather": get_site_weather}

TOOL_DECLARATIONS = [
    {
        "name": "calculate_deadline",
        "description": (
            "Calculates the filing deadline of a form and how many days are left. Call it once for every form "
            "that has a work start (or occupation) date. The notice period is looked up from the form ID. "
            "Returns the deadline date, days left, priority and a status text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "form_id": {"type": "string", "description": "Form ID from the library, e.g. F01, F02, F05, F08"},
                "report_date": {"type": "string", "description": "Report date (today), format YYYY-MM-DD"},
                "work_start_date": {"type": "string", "description": "Date the work starts (or occupation date), format YYYY-MM-DD"},
            },
            "required": ["form_id", "report_date", "work_start_date"],
        },
    },
    {
        "name": "get_site_weather",
        "description": (
            "Gets the live weather forecast for Lausanne for one day (up to 16 days ahead): min/max temperature, "
            "rain, max wind and max gusts, plus indicative flags. Call it for each day on which the note plans "
            "outdoor work (crane, concrete pour, roofing, scaffolding, excavation, facade work)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Day of the planned work, format YYYY-MM-DD (e.g. 'tomorrow' = report date + 1 day)"},
                "activity": {
                    "type": "string",
                    "enum": ["crane", "concrete", "roofing", "scaffolding", "excavation", "general"],
                    "description": "Main outdoor activity planned that day",
                },
            },
            "required": ["date", "activity"],
        },
    },
]


# ---------------------------------------------------------------- model call
def call_gemini(contents, models):
    """One call to Gemini. Tries the models in order until one answers."""
    last_err = ""
    for attempt in range(2):
        for m in models:
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                    headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY},
                    json={
                        "systemInstruction": {"parts": [{"text": INSTRUCTIONS}]},
                        "contents": contents,
                        "tools": [{"functionDeclarations": TOOL_DECLARATIONS}],
                        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
                    },
                    timeout=TIMEOUT_S,
                )
                j = r.json()
            except requests.Timeout:
                last_err = f"{m}: no answer after {TIMEOUT_S} s (model overloaded)"
                print(f"[ChantierPilot] fallback: {last_err}", flush=True)
                continue
            except Exception as e:  # network problems, invalid JSON
                last_err = f"{m}: {type(e).__name__}"
                print(f"[ChantierPilot] fallback: {last_err}", flush=True)
                continue
            cands = j.get("candidates") or []
            if r.ok and cands and cands[0].get("content"):
                return j, m
            last_err = (j.get("error") or {}).get("message") or f"HTTP {r.status_code}"
            print(f"[ChantierPilot] fallback: {m} -> HTTP {r.status_code}: {str(last_err)[:300]}", flush=True)
            if r.status_code in (400, 401, 403):
                # Do not show Google's raw message to visitors; log it for the maintainer.
                print(f"[ChantierPilot] Gemini refused the request ({r.status_code}): {last_err}", flush=True)
                raise RuntimeError("The service is not configured correctly (API key). Please tell the maintainers.")
        if attempt == 0:
            time.sleep(4)
    raise RuntimeError("All models are busy or unavailable right now. Try again in a few minutes.")


# ---------------------------------------------------------------- rate limit
_hits = defaultdict(deque)


def _allowed(visitor):
    limit, window = RATE_LIMIT
    q, now = _hits[visitor], time.time()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


# ---------------------------------------------------------------- main handler
def _save_report(md):
    f = tempfile.NamedTemporaryFile("w", suffix="_chantierpilot_report.md", delete=False, encoding="utf-8")
    f.write(md)
    f.close()
    return f.name


def generate(note, report_date, request: gr.Request = None):
    note = (note or "").strip()
    if not note:
        return "Please paste a site note first.", gr.update(visible=False)
    if len(note) > MAX_NOTE_CHARS:
        return f"❌ The note is too long ({len(note)} characters, max {MAX_NOTE_CHARS}). Please shorten it.", gr.update(visible=False)
    if _parse_date(report_date) is None:
        return "❌ Report date must be a valid date in the format YYYY-MM-DD.", gr.update(visible=False)
    if not API_KEY:
        return "❌ The app is not configured yet: the maintainer must add the GEMINI_API_KEY secret.", gr.update(visible=False)
    # Behind a proxy (Hugging Face, Render) the real visitor IP is in X-Forwarded-For.
    fwd = (getattr(request, "headers", None) or {}).get("x-forwarded-for", "") if request else ""
    visitor = fwd.split(",")[0].strip() or getattr(getattr(request, "client", None), "host", None) or "anonymous"
    if not _allowed(visitor):
        return f"⏳ Rate limit reached ({RATE_LIMIT[0]} reports per hour). Please try again later.", gr.update(visible=False)

    # The note is wrapped and labelled as data (see the Security rule in instructions.md).
    # The date line is in English on purpose: the answer language must follow the note, not this line.
    user_text = (
        f"Report date: {report_date}\n"
        "Answer in the language of the text inside <site_note> (not the language of these two lines).\n\n"
        f"<site_note>\n{note}\n</site_note>"
    )
    contents = [{"role": "user", "parts": [{"text": user_text}]}]
    tool_log, models, nudged = [], MODELS, False
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            j, m = call_gemini(contents, models)
            models = [m]  # keep the same model for the whole conversation
            content = j["candidates"][0]["content"]
            parts = content.get("parts", [])
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "\n\n".join(p["text"] for p in parts if p.get("text") and not p.get("thought")).strip()
                # Guard (revision log #7): forms flagged but deadlines never computed by the tool ->
                # send the model back once to call calculate_deadline instead of guessing dates/priorities.
                flagged = re.search(r"\bF(0[1-9]|1[0-2])\b", text or "")
                used_deadline_tool = any(t["name"] == "calculate_deadline" for t in tool_log)
                if flagged and not used_deadline_tool and not nudged:
                    nudged = True
                    contents.append(content)
                    contents.append({"role": "user", "parts": [{"text": NUDGE}]})
                    continue
                md = (text or "❌ Empty answer. Try again.") + f"\n\n---\n*Model: {m}*"
                md += f"\n\n<details><summary>🔧 Tool calls ({len(tool_log)})</summary>\n\n"
                md += "\n".join(
                    f"{i + 1}. `{t['name']}({json.dumps(t['args'], ensure_ascii=False)})` → `{json.dumps(t['result'], ensure_ascii=False)}`"
                    for i, t in enumerate(tool_log)
                ) or "No tool was used for this note."
                md += "\n\n</details>"
                return md, gr.update(value=_save_report(md), visible=True)
            # The model asked for tools: OUR code runs them and sends the results back
            contents.append(content)
            responses = []
            for fc in calls:
                name, args = fc.get("name"), fc.get("args") or {}
                fn = TOOLS.get(name)
                try:
                    result = fn(**args) if fn else {"error": f"Unknown tool: {name}"}
                except Exception as e:
                    result = {"error": f"Tool failed: {type(e).__name__}"}
                tool_log.append({"name": name, "args": args, "result": result})
                fr = {"name": name, "response": result}
                if fc.get("id"):
                    fr["id"] = fc["id"]
                responses.append({"functionResponse": fr})
            contents.append({"role": "user", "parts": responses})
        return "❌ Too many tool calls in a row. Try again or shorten the note.", gr.update(visible=False)
    except RuntimeError as e:
        return f"❌ {e}", gr.update(visible=False)


# ---------------------------------------------------------------- UI
with gr.Blocks(title="ChantierPilot") as demo:
    gr.Markdown(
        "# 🏗️ ChantierPilot\n"
        "Turn end-of-day site notes into a structured daily log, and see which Lausanne, Vaud or federal (Suva) "
        "forms may need to be filed.\n\n"
        "> ⚠️ **Demo tool for a university course.** Not legal advice. Always check requirements with the City of "
        "Lausanne, the Canton of Vaud or Suva before filing. Please only paste fictional notes."
    )
    with gr.Row():
        with gr.Column():
            note = gr.Textbox(label="Site note (any language)", lines=10, max_length=MAX_NOTE_CHARS,
                              placeholder="What happened on site today?")
            report_date = gr.Textbox(label="Report date (YYYY-MM-DD)", value=lambda: datetime.now(TZ).date().isoformat())
            with gr.Row():
                ex = gr.Button("Load example note")
                go = gr.Button("Generate report", variant="primary")
        with gr.Column():
            out = gr.Markdown("The report will appear here.")
            dl = gr.DownloadButton("⬇️ Download report (.md)", visible=False)
    ex.click(lambda: EXAMPLE, outputs=note)
    go.click(generate, inputs=[note, report_date], outputs=[out, dl], concurrency_limit=2)

if __name__ == "__main__":
    # Render (and most hosts) give the port in $PORT and need 0.0.0.0; Hugging Face uses 7860.
    demo.queue(max_size=20).launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)))
