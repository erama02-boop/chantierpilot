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
# Model order (revision log #9): Google retired gemini-2.5-* for new keys (HTTP 404, recommending
# gemini-3.8-flash / gemini-3.5-flash-lite), and the "-latest" aliases were overloaded (timeouts / HTTP 503).
# Full model first: the lite models skip tools and the language rule more often.
MODELS = ["gemini-3.8-flash", "gemini-flash-latest", "gemini-3.5-flash-lite", "gemini-flash-lite-latest"]
INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text(encoding="utf-8")
MAX_NOTE_CHARS = 5000          # longer notes are refused
MAX_TOOL_ROUNDS = 8            # same limit as v1
RATE_LIMIT = (int(os.environ.get("RATE_LIMIT_PER_HOUR", "30")), 3600)  # reports per visitor per hour
TIMEOUT_S = 30
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


_WEATHER_CACHE = {"at": 0.0, "days": {}}  # one 16-day fetch, reused for 1 hour (free APIs have daily limits)
METNO_UA = "ChantierPilot/2.0 (course project; github.com/erama02-boop/chantierpilot)"


def _open_meteo_all():
    url = (
        "https://api.open-meteo.com/v1/forecast?latitude=46.5197&longitude=6.6323"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max"
        "&timezone=Europe%2FZurich&forecast_days=16"
    )
    r = requests.get(url, timeout=15)
    j = r.json()
    if not r.ok or j.get("error"):
        raise RuntimeError(j.get("reason", r.status_code))
    d = j["daily"]
    out = {}
    for i, iso in enumerate(d["time"]):
        out[date.fromisoformat(iso)] = {
            "temp_min_c": d["temperature_2m_min"][i], "temp_max_c": d["temperature_2m_max"][i],
            "rain_mm": d["precipitation_sum"][i], "wind_max_kmh": d["wind_speed_10m_max"][i],
            "gusts_max_kmh": d["wind_gusts_10m_max"][i], "source": "Open-Meteo",
        }
    return out


def _met_norway_all():
    """Backup: MET Norway locationforecast (free, ~9 days), aggregated per local day."""
    r = requests.get("https://api.met.no/weatherapi/locationforecast/2.0/complete?lat=46.5197&lon=6.6323",
                     headers={"User-Agent": METNO_UA}, timeout=15)
    r.raise_for_status()
    acc = defaultdict(lambda: {"temps": [], "winds": [], "gusts": [], "rain": 0.0})
    for step in r.json()["properties"]["timeseries"]:
        t = datetime.fromisoformat(step["time"].replace("Z", "+00:00")).astimezone(TZ)
        a, det = acc[t.date()], step["data"]["instant"]["details"]
        if det.get("air_temperature") is not None:
            a["temps"].append(det["air_temperature"])
        if det.get("wind_speed") is not None:
            a["winds"].append(det["wind_speed"] * 3.6)
        if det.get("wind_speed_of_gust") is not None:
            a["gusts"].append(det["wind_speed_of_gust"] * 3.6)
        n1, n6 = step["data"].get("next_1_hours"), step["data"].get("next_6_hours")
        if n1:
            a["rain"] += n1["details"].get("precipitation_amount", 0)
        elif n6:
            a["rain"] += n6["details"].get("precipitation_amount", 0)
            a["temps"] += [x for x in (n6["details"].get("air_temperature_min"), n6["details"].get("air_temperature_max")) if x is not None]
    out = {}
    for day, a in acc.items():
        if a["temps"]:
            out[day] = {"temp_min_c": round(min(a["temps"]), 1), "temp_max_c": round(max(a["temps"]), 1),
                        "rain_mm": round(a["rain"], 1),
                        "wind_max_kmh": round(max(a["winds"]), 1) if a["winds"] else None,
                        "gusts_max_kmh": round(max(a["gusts"]), 1) if a["gusts"] else None, "source": "MET Norway"}
    if not out:
        raise RuntimeError("no MET Norway data")
    return out


def _forecast_all():
    if _WEATHER_CACHE["days"] and time.time() - _WEATHER_CACHE["at"] < 3600:
        return _WEATHER_CACHE["days"]
    errors = []
    for provider in (_open_meteo_all, _met_norway_all):
        try:
            days = provider()
            _WEATHER_CACHE.update(at=time.time(), days=days)
            return days
        except Exception as e:
            errors.append(f"{provider.__name__}: {e}")
            print(f"[ChantierPilot] weather fallback: {errors[-1]}"[:300], flush=True)
    raise RuntimeError("; ".join(errors))


# Indicative go / no-go thresholds per activity (NOT legal limits; manufacturer limits and the
# site safety plan always win). Each rule: (field, operator, value, verdict, reason).
WIND = "wind_or_gust_kmh"
RULES = {
    "crane":       [(WIND, ">=", 50, "NO-GO", "wind/gusts at or above 50 km/h"), (WIND, ">=", 35, "CAUTION", "wind/gusts at or above 35 km/h")],
    "concrete":    [("temp_min_c", "<", 0, "NO-GO", "frost (min below 0 °C)"), ("rain_mm", ">=", 10, "NO-GO", "heavy rain (10 mm or more)"),
                    ("temp_min_c", "<", 5, "CAUTION", "cold (min below 5 °C): cold-weather measures"), ("rain_mm", ">=", 2, "CAUTION", "rain (2 mm or more): cover fresh concrete"),
                    ("temp_max_c", ">=", 30, "CAUTION", "heat (30 °C or more): fast setting")],
    "roofing":     [("rain_mm", ">=", 5, "NO-GO", "rain (5 mm or more)"), (WIND, ">=", 50, "NO-GO", "wind/gusts at or above 50 km/h"),
                    ("temp_min_c", "<=", 0, "CAUTION", "frost: icy surfaces"), ("rain_mm", ">=", 1, "CAUTION", "some rain"),
                    (WIND, ">=", 35, "CAUTION", "wind/gusts at or above 35 km/h")],
    "scaffolding": [(WIND, ">=", 60, "NO-GO", "wind/gusts at or above 60 km/h"), (WIND, ">=", 40, "CAUTION", "wind/gusts at or above 40 km/h"),
                    ("temp_min_c", "<=", 0, "CAUTION", "frost: icy platforms")],
    "excavation":  [("rain_mm", ">=", 20, "NO-GO", "very heavy rain (20 mm or more)"), ("rain_mm", ">=", 10, "CAUTION", "heavy rain: drainage and trench stability"),
                    ("temp_min_c", "<=", 0, "CAUTION", "frozen ground")],
    "facade":      [("rain_mm", ">=", 1, "NO-GO", "rain (painting/rendering needs dry weather)"), ("temp_min_c", "<", 5, "CAUTION", "cold (below 5 °C): check product limits"),
                    (WIND, ">=", 40, "CAUTION", "wind/gusts at or above 40 km/h")],
    "general":     [(WIND, ">=", 75, "NO-GO", "storm (wind/gusts 75 km/h or more)"), ("rain_mm", ">=", 10, "CAUTION", "heavy rain"),
                    (WIND, ">=", 50, "CAUTION", "strong wind"), ("temp_min_c", "<=", -5, "CAUTION", "severe cold"), ("temp_max_c", ">=", 32, "CAUTION", "heat")],
}
_OPS = {">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
_RANK = {"GO": 0, "CAUTION": 1, "NO-GO": 2}


def _verdict(values, activity):
    v = dict(values)
    g, w = v.get("gusts_max_kmh"), v.get("wind_max_kmh")
    v[WIND] = max(x for x in (g, w, 0) if x is not None)
    verdict, reasons, hit_fields = "GO", [], set()
    for field, op, limit, level, reason in RULES[activity]:  # rules are listed NO-GO first
        x = v.get(field)
        if x is not None and _OPS[op](x, limit) and not (level == "CAUTION" and (field, op) in hit_fields):
            hit_fields.add((field, op))
            reasons.append(f"{level}: {reason}")
            if _RANK[level] > _RANK[verdict]:
                verdict = level
    return verdict, reasons


def get_site_weather(date=None, activity="general", **_ignored):
    """Weather go/no-go for one planned outdoor task in Lausanne (Open-Meteo, backup MET Norway)."""
    activity = activity if activity in RULES else "general"
    day = _parse_date(date)
    if day is None:
        return {"error": "date is missing or not a valid date (YYYY-MM-DD)."}
    today = datetime.now(TZ).date()
    if day < today:
        return {"date": day.isoformat(), "error": "This date is in the past, so there is no forecast. Weather is only checked for planned work."}
    if day > today + timedelta(days=15):
        return {"date": day.isoformat(), "error": "Forecasts only cover the next 16 days. Check again closer to the date (e.g. MeteoSwiss)."}
    try:
        days = _forecast_all()
    except Exception:
        return {"date": day.isoformat(), "error": "Weather service not reachable right now. Check the forecast manually (e.g. MeteoSwiss)."}
    if day not in days:
        return {"date": day.isoformat(), "error": "No forecast for this date yet (the backup service covers about 9 days). Check MeteoSwiss closer to the date."}
    verdict, reasons = _verdict(days[day], activity)
    result = {"date": day.isoformat(), "activity": activity, "location": "Lausanne", **days[day],
              "verdict": verdict, "reasons": reasons or ["No weather limits reached for this activity"]}
    if verdict != "GO":
        later = sorted(d for d in days if d > day)
        result["next_go_day"] = next((d.isoformat() for d in later if _verdict(days[d], activity)[0] == "GO"), None) \
            or "No GO day in the available forecast"
    result["note"] = ("Indicative thresholds only, not legal limits. The equipment manufacturer's limits and the "
                      "site's safety plan always apply. Re-check the forecast the day before.")
    return result


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
            "Checks whether a planned outdoor task in Lausanne can go ahead on a given day (up to 16 days ahead). "
            "Returns the forecast (min/max temperature, rain, wind, gusts), a verdict GO / CAUTION / NO-GO for that "
            "activity with the reasons, and, if not GO, the next day in the forecast that is GO. Call it once for each "
            "day on which the note plans outdoor work (crane lift, concrete pour, roofing, scaffolding, excavation, "
            "facade painting/rendering), or when the user asks whether a task can be done on a given day."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Day of the planned work, format YYYY-MM-DD (e.g. 'tomorrow' = report date + 1 day)"},
                "activity": {
                    "type": "string",
                    "enum": ["crane", "concrete", "roofing", "scaffolding", "excavation", "facade", "general"],
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
            # Free-tier quota (HTTP 429) is per minute: wait longer before the second round.
            time.sleep(20 if "429" in str(last_err) or "quota" in str(last_err).lower() else 4)
    if "quota" in str(last_err).lower():
        raise RuntimeError("The free AI quota is used up for the moment. Please wait about a minute and try again.")
    raise RuntimeError("All models are busy or unavailable right now. Try again in a few minutes.")



# ---------------------------------------------------------------- deterministic pre-checks (revision log #13)
# The free-tier lite model sometimes ignores the language rule and the trigger rules, so the code
# does the parts where the rules are known and hands the model explicit hints.
_STOP = {
    "English": "the and of to a in is was on for with will be we at from tomorrow today worker site".split(),
    "French": "le la les des du de et un une est sur pour avec demain aujourd on chantier travaux pas".split(),
    "German": "der die das und ist mit auf für ein eine morgen heute baustelle nicht wir".split(),
    "Italian": "il lo gli della di e un una è con per domani oggi cantiere lavori non".split(),
    "Portuguese": "o os as da do de e um uma com para amanhã hoje obra vamos não no na".split(),
    "Spanish": "el los las del de y un una es con para mañana hoy obra vamos no en".split(),
}


def detect_language(note):
    words = re.findall(r"[a-zàâäçéèêëîïôöùûüñãõœ']+", note.lower())
    scores = {lang: sum(w in stop for w in words) for lang, stop in _STOP.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else None


# Trigger words per form (lower-case, accents kept). Matching any of them makes the form a candidate.
TRIGGERS = {
    "F01": ["benne", "trottoir", "voie publique", "domaine public", "places de parc", "place de parc", "fermeture de voie",
            "palissade", "skip on the street", "sidewalk", "pavement", "public road", "parking space", "lane closure"],
    "F02": ["ouverture du chantier", "ouverture de chantier", "début des travaux", "debut des travaux", "installation de chantier",
            "site opening", "works start", "first day on site", "new site starting"],
    "F03": ["abattre", "abattage", "élagage", "elagage", "couper un arbre", "arbre", "cut down a tree", "remove a tree", "fell a tree",
            "árvore", "albero", "baum", "árbol"],
    "F05": ["désamiantage", "desamiantage", "asbestos removal", "assainissement amiante"],
    "F06": ["amiante", "asbestos", "fibrociment", "matériau suspect"],
    "F07": ["désamiantage terminé", "fin du désamiantage", "asbestos removal finished"],
    "F08": ["démolition", "demolition", "déconstruction", "deconstruction", "travaux souterrains", "tunnel", "minage", "abattage de roches",
            "rock blasting", "underground work", "abbruch", "demolição", "demolizione", "demolición"],
    "F09": ["fin des travaux", "permis d'habiter", "emménagement", "réception de l'ouvrage", "works finished", "handover", "move in"],
    "F10": ["modification du projet", "changement des plans", "changement d'affectation", "change the plans", "extra floor", "change of use"],
    "F11": ["accident", "blessé", "blessure", "chute", "ambulance", "hôpital", "hopital", "injured", "fell", "hospital", "cut his", "cut her",
            "s'est coupé", "verletzt", "ferito", "ferido", "herido"],
    "F12": ["travaux de nuit", "travail de nuit", "dimanche", "jour férié", "night work", "sunday work", "overnight"],
}


def candidate_forms(note):
    text = note.lower()
    found = {}
    for form, words in TRIGGERS.items():
        hits = [w for w in words if w in text]
        if hits:
            found[form] = hits[0]
    if "F05" in found:
        found.pop("F06", None)  # planned removal (F05), not an unexpected discovery (F06)
    if "F07" in found:
        found.pop("F05", None)
    return found

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
    lang = detect_language(note)
    cands = candidate_forms(note)
    hint = ", ".join(f"{f} (\"{w}\")" for f, w in cands.items()) or "none"
    user_text = (
        f"Report date: {report_date}\n"
        + (f"Answer language: {lang}. Write the whole answer in {lang}.\n" if lang else
           "Answer in the language of the text inside <site_note>.\n")
        + f"Trigger words found by code (candidate forms): {hint}. Flag only these forms, and only if the note "
          "really matches the form's rule; a form not in this list needs a clear match in the note.\n\n"
        + f"<site_note>\n{note}\n</site_note>"
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
