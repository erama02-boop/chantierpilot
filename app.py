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
# Full model first; then the lite model, which answers fast (the code guards keep it accurate), before the
# often-overloaded "-latest" alias (revision log #16).
MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-flash-lite-latest"]
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
    "Check before finishing: your answer needs tool results you did not get. Call now: {missing}. "
    "Estimate the earliest date if a date is vague. Then rewrite your COMPLETE answer using exactly what the tools "
    "return (deadline, days left, priority, weather verdict and values), in the language of the site note. "
    "If no date exists for a form, keep 'Date not known yet'."
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
            reasons.append((level, reason))
            if _RANK[level] > _RANK[verdict]:
                verdict = level
    # The verdict is shown separately, so only lesser levels keep a prefix ("rain 12 mm; CAUTION: wind").
    return verdict, [r if lvl == verdict else f"{lvl}: {r}" for lvl, r in reasons]


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
    "English": ("the and of to a in is was on for with will be we at from tomorrow today worker site painting "
                "workers floor windows crane").split(),
    "French": ("le la les des du de et un une est sur pour avec demain aujourd on chantier travaux pas peinture "
               "intérieure extérieure bureaux peintres ouvriers pose étage fenêtres grue levage benne trottoir").split(),
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


OUTDOOR = {
    "concrete": ["béton", "beton", "bétonnage", "coulage", "dalle", "concrete", "concrete pour", "calcestruzzo", "concreto", "hormigón"],
    "crane": ["grue", "levage", "camion-grue", "crane", "lift", "kran", "gru", "guindaste"],
    "roofing": ["toit", "toiture", "couverture", "roof", "dach", "tetto", "telhado", "tejado"],
    "scaffolding": ["échafaudage", "echafaudage", "scaffold", "gerüst", "ponteggio", "andaime", "andamio"],
    "excavation": ["fouille", "terrassement", "excavation", "escavação", "excavación", "aushub", "scavo"],
    "facade": ["façade", "facade", "crépi", "peinture extérieure", "fassade", "facciata", "fachada"],
}


def outdoor_work(note):
    text = note.lower()
    return [act for act, words in OUTDOOR.items() if any(re.search(r"\b" + re.escape(w), text) for w in words)]


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


# ---------------------------------------------------------------- conversation engine
MAX_HISTORY_MESSAGES = 12      # earlier turns sent back to the model (keeps requests small)
_DATE_LINE = re.compile(r"^\s*(report date|date du rapport|datum|data)\s*[:=]\s*(\d{4}-\d{2}-\d{2})\s*$", re.I | re.M)


def _today():
    return datetime.now(TZ).date().isoformat()


def _visitor(request):
    # Behind a proxy (Hugging Face, Render) the real visitor IP is in X-Forwarded-For.
    headers = getattr(request, "headers", None) or {}
    fwd = headers.get("x-forwarded-for", "") if request else ""
    return fwd.split(",")[0].strip() or getattr(getattr(request, "client", None), "host", None) or "anonymous"


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, (list, tuple)):
        return "\n".join(_text_of(c) for c in content)
    return ""


def _past_turns(history):
    """User messages and final answers from the chat history (tool/status bubbles are skipped)."""
    turns = []
    for msg in history or []:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        meta = (msg.get("metadata") if isinstance(msg, dict) else getattr(msg, "metadata", None)) or {}
        text = _text_of(msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")).strip()
        if role in ("user", "assistant") and text and not meta.get("title"):
            turns.append((role, text))
    return turns[-MAX_HISTORY_MESSAGES:]


def build_contents(message, history, report_date):
    """Gemini contents for this turn. Code pre-checks run over everything the user wrote in the chat."""
    past = _past_turns(history)
    user_texts = [t for r, t in past if r == "user"] + [message]
    all_text = "\n".join(user_texts)
    lang = detect_language(message) or detect_language(all_text)
    cands = candidate_forms(all_text)
    outdoor = outdoor_work(all_text)
    hint = ", ".join(f"{f} (\"{w}\")" for f, w in cands.items()) or "none"
    first = not any(r == "user" for r, _ in past)
    tag = "site_note" if first else "follow_up"
    # The control lines are in English on purpose: the answer language must follow the user, not these lines.
    text = (
        f"Report date: {report_date}\n"
        + (f"Answer language: {lang}. Write the whole answer in {lang}, headings and labels included.\n" if lang else
           f"Answer in the language of the text inside <{tag}>.\n")
        + f"Trigger words found by code in the user's messages (candidate forms): {hint}. Flag only these forms, and "
          "only if the notes really match the form's rule; a form not in this list needs a clear match.\n"
        + (f"Outdoor work found by code: {', '.join(outdoor)}. Call get_site_weather for each planned day of this work; "
           "never write a weather verdict or values without the tool.\n" if outdoor else
           "No outdoor work found by code: skip the weather section unless the user clearly plans outdoor work.\n")
        + ("This is a follow-up in the same conversation: answer it, and if it changes the report (new dates, new facts), "
           "give the updated report, recomputing deadlines and weather with the tools.\n" if not first else "")
        + f"\n<{tag}>\n{message}\n</{tag}>"
    )
    contents = [{"role": "user" if r == "user" else "model", "parts": [{"text": t}]} for r, t in past]
    contents.append({"role": "user", "parts": [{"text": text}]})
    return contents, outdoor


def run_agent(contents, outdoor):
    """Runs the model + tools loop. Yields ("model", name), ("tool", name, args, result) and finally
    ("final", text) or ("error", message)."""
    tool_log, models, nudged = [], MODELS, False
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            j, m = call_gemini(contents, models)
            if models is MODELS:
                yield ("model", m)
            models = [m]  # keep the same model for the whole turn
            content = j["candidates"][0]["content"]
            parts = content.get("parts", [])
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "\n\n".join(p["text"] for p in parts if p.get("text") and not p.get("thought")).strip()
                # Guards (revision log #7 and #14): never accept deadlines or weather verdicts the tools did not compute.
                # If the answer needs a tool that was never called, send the model back once to call it.
                used = {t["name"] for t in tool_log}
                missing = []
                if re.search(r"\bF(0[1-9]|1[0-2])\b", text or "") and "calculate_deadline" not in used:
                    missing.append("calculate_deadline for every flagged form with a start, felling, removal or occupation date")
                if outdoor and "get_site_weather" not in used:
                    missing.append("get_site_weather for each planned day of outdoor work (" + ", ".join(outdoor) + ")")
                if missing and not nudged:
                    nudged = True
                    contents.append(content)
                    contents.append({"role": "user", "parts": [{"text": NUDGE.format(missing="; ".join(missing))}]})
                    continue
                yield ("final", text or "I could not produce an answer. Please try again.")
                return
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
                yield ("tool", name, args, result)
                fr = {"name": name, "response": result}
                if fc.get("id"):
                    fr["id"] = fc["id"]
                responses.append({"functionResponse": fr})
            contents.append({"role": "user", "parts": responses})
        yield ("error", "Too many tool calls in a row. Please try again or shorten the note.")
    except RuntimeError as e:
        yield ("error", str(e))


def _tool_bubble(name, args, result):
    """Short, readable summary of one tool call for the chat."""
    if name == "calculate_deadline":
        title = f"🗓️ Deadline · {result.get('form_id') or args.get('form_id', '?')}"
        if "error" in result:
            return title, f"⚠️ {result['error']}"
        return title, (f"Work starts {args.get('work_start_date')} → file by **{result['deadline_date']}** · "
                       f"{result['status']} · {result['priority']}")
    if name == "get_site_weather":
        title = f"🌦️ Weather · {args.get('activity', 'general')} · {args.get('date', '?')}"
        if "error" in result:
            return title, f"⚠️ {result['error']}"
        icon = {"GO": "✅", "CAUTION": "⚠️", "NO-GO": "⛔"}.get(result.get("verdict"), "")
        wind = result.get("gusts_max_kmh") or result.get("wind_max_kmh")
        line = (f"{icon} **{result.get('verdict')}** · {result.get('temp_min_c')}–{result.get('temp_max_c')} °C · "
                f"rain {result.get('rain_mm')} mm · wind {wind} km/h ({result.get('source')})")
        if result.get("next_go_day"):
            line += f" · next GO day: {result['next_go_day']}"
        return title, line
    return f"🔧 {name}", json.dumps(result, ensure_ascii=False)[:300]


def _save_report(md):
    f = tempfile.NamedTemporaryFile("w", suffix="_chantierpilot_report.md", delete=False, encoding="utf-8")
    f.write(md)
    f.close()
    return f.name


def _check_input(message, request):
    message = (message or "").strip()
    if not message:
        return "Please type or paste a site note first."
    if len(message) > MAX_NOTE_CHARS:
        return f"This message is too long ({len(message)} characters, max {MAX_NOTE_CHARS}). Please shorten it."
    if not API_KEY:
        return "The app is not configured yet: the maintainer must add the GEMINI_API_KEY secret."
    if not _allowed(_visitor(request)):
        return f"Rate limit reached ({RATE_LIMIT[0]} messages per hour). Please try again later."
    return None


def chat(message, history, request: gr.Request = None):
    """Chat handler: streams progress (status + tool bubbles), then the answer."""
    problem = _check_input(message, request)
    if problem:
        yield [gr.ChatMessage(role="assistant", content=f"⚠️ {problem}")], gr.update(visible=False)
        return
    message = message.strip()
    m = _DATE_LINE.search(message)  # optional "Report date: YYYY-MM-DD" line overrides today
    report_date = m.group(2) if m and _parse_date(m.group(2)) else _today()
    contents, outdoor = build_contents(message, history, report_date)

    t0 = time.time()
    steps = ["Reading the note and checking it against the form library"]
    status = gr.ChatMessage(role="assistant", content="",
                            metadata={"title": "Working on it…", "status": "pending", "log": steps[0]})
    tools = []
    yield [status], gr.update(visible=False)
    for event in run_agent(contents, outdoor):
        if event[0] == "model":
            steps.append(f"Model: {event[1]}")
        elif event[0] == "tool":
            title, line = _tool_bubble(*event[1:])
            tools.append(gr.ChatMessage(role="assistant", content=line, metadata={"title": title, "status": "done"}))
            steps.append(title)
        elif event[0] in ("final", "error"):
            done = gr.ChatMessage(role="assistant", content="\n".join(f"· {s}" for s in steps),
                                  metadata={"title": f"Checked {len(tools)} tool result(s)" if tools else "Done",
                                            "status": "done", "duration": round(time.time() - t0, 1)})
            if event[0] == "error":
                yield [done, *tools, gr.ChatMessage(role="assistant", content=f"⚠️ {event[1]}")], gr.update(visible=False)
                return
            answer = event[1]
            yield [done, *tools, gr.ChatMessage(role="assistant", content=answer)], \
                gr.update(value=_save_report(f"Report date: {report_date}\n\n{answer}"), visible=True)
            return
        status.metadata["log"] = " → ".join(steps[-3:])
        yield [status, *tools], gr.update(visible=False)


def generate(note, report_date=None, request=None):
    """Single-shot report (no chat history). Used by the tests and the API."""
    problem = _check_input(note, request)
    if problem:
        return f"❌ {problem}", gr.update(visible=False)
    if report_date is not None and _parse_date(report_date) is None:
        return "❌ Report date must be a valid date in the format YYYY-MM-DD.", gr.update(visible=False)
    contents, outdoor = build_contents(note.strip(), [], report_date or _today())
    tool_log, final = [], None
    for event in run_agent(contents, outdoor):
        if event[0] == "tool":
            tool_log.append(event[1:])
        elif event[0] == "final":
            final = event[1]
        elif event[0] == "error":
            return f"❌ {event[1]}", gr.update(visible=False)
    md = final + f"\n\n<details><summary>🔧 Tool calls ({len(tool_log)})</summary>\n\n" + (
        "\n".join(f"{i + 1}. `{n}({json.dumps(a, ensure_ascii=False)})` → `{json.dumps(r, ensure_ascii=False)}`"
                  for i, (n, a, r) in enumerate(tool_log)) or "No tool was used for this note.") + "\n\n</details>"
    return md, gr.update(value=_save_report(md), visible=True)


def report_api(note: str, report_date: str = "", request: gr.Request = None) -> str:
    """API endpoint: one note in, one Markdown report out (with the tool calls listed)."""
    return generate(note, report_date or None, request)[0]


# ---------------------------------------------------------------- UI
WELCOME = """
<div class="cp-welcome">
<p style="font-size:1.05rem"><b>Send me your end-of-day site note</b>, in any language.<br>
I'll turn it into a <b>daily log</b>, flag the <b>Lausanne, Vaud and Suva forms</b> it triggers with their deadlines,
and check the <b>weather go / no-go</b> for planned outdoor work.</p>
<p class="cp-small">You can answer my questions afterwards and I'll update the report.
To use another report date, start your note with <code>Report date: 2026-11-21</code>.</p>
</div>
"""

EXAMPLES = [
    EXAMPLE,
    "Site at Chemin de Mornex 5, Lausanne. A worker fell from a ladder and was taken to hospital by ambulance.",
    "Place de la Riponne, Lausanne. Demain bétonnage de la dalle en extérieur, 6 ouvriers.",
    "Can we do the crane lift at Avenue de Beaulieu 9, Lausanne, next Wednesday?",
]

CSS = """
.gradio-container { max-width: 980px !important; margin: 0 auto !important; }
#cp-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 4px 4px 0; }
#cp-header h1 { font-size: 1.35rem; margin: 0; }
#cp-header p { margin: 0; opacity: .75; font-size: .9rem; }
.cp-welcome { text-align: center; max-width: 560px; margin: 0 auto; }
.cp-welcome h2 { margin-bottom: .4em; }
.cp-small { font-size: .85rem; opacity: .75; }
#cp-disclaimer { font-size: .78rem; opacity: .7; text-align: center; }
footer { display: none !important; }
"""

THEME = gr.themes.Soft(primary_hue="orange", secondary_hue="amber", neutral_hue="slate")

with gr.Blocks(title="ChantierPilot · site reports & permits for Lausanne", fill_height=True) as demo:
    gr.HTML("<div id='cp-header'><div><h1>🏗️ ChantierPilot</h1>"
            "<p>Site reports, permits and weather go / no-go for construction sites in Lausanne</p></div></div>")
    download = gr.DownloadButton("⬇️ Download the last report (.md)", visible=False, size="sm", render=False)
    gr.ChatInterface(
        fn=chat,
        chatbot=gr.Chatbot(
            placeholder=WELCOME, layout="panel", buttons=["copy_all"], scale=1, show_label=False,
            render_markdown=True, group_consecutive_messages=True,
        ),
        textbox=gr.Textbox(placeholder="Paste today's site note, or ask about a task and a date…",
                           lines=2, max_lines=12, max_length=MAX_NOTE_CHARS, submit_btn=True, stop_btn=True,
                           show_label=False),
        examples=EXAMPLES,
        example_labels=["🧱 Asbestos + crane on the sidewalk (FR)", "🚑 Worker injured (EN)",
                        "🌧️ Concrete pour tomorrow (FR)", "🏗️ Crane lift next Wednesday?"],
        additional_outputs=[download],
        fill_height=True,
        autofocus=True,
        concurrency_limit=2,
        api_name="chat",
    )
    download.render()
    gr.Markdown("⚠️ Demo tool for a university course (HEC Lausanne). Not legal advice: always check with the City of "
                "Lausanne, the Canton of Vaud or Suva before filing. Please use fictional notes only.",
                elem_id="cp-disclaimer")
    # Keep the single-shot report endpoint for testing and integrations.
    gr.api(report_api, api_name="generate")

if __name__ == "__main__":
    # Render (and most hosts) give the port in $PORT and need 0.0.0.0; Hugging Face uses 7860.
    demo.queue(max_size=20).launch(server_name="0.0.0.0", server_port=int(os.environ.get("PORT", 7860)),
                                   theme=THEME, css=CSS)
