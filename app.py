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

from urllib.parse import quote

import gradio as gr
import requests

# ---------------------------------------------------------------- configuration
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# Model order (revision log #9): Google retired gemini-2.5-* for new keys (HTTP 404, recommending
# gemini-3.8-flash / gemini-3.5-flash-lite), and the "-latest" aliases were overloaded (timeouts / HTTP 503).
# Full model first; then the lite model, which answers fast (the code guards keep it accurate), before the
# often-overloaded "-latest" alias (revision log #16).
MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY", "").strip()
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
# Backup provider (revision log #23): Mistral is tried after the two main Gemini models when it has a key.
MODELS = (["gemini-3.8-flash", "gemini-3.5-flash-lite"] + ([MISTRAL_MODEL] if MISTRAL_KEY else [])
          + ["gemini-flash-latest", "gemini-flash-lite-latest"])
INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text(encoding="utf-8")
MAX_NOTE_CHARS = 5000          # longer notes are refused
MAX_TOOL_ROUNDS = 8            # same limit as v1
RATE_LIMIT = (int(os.environ.get("RATE_LIMIT_PER_HOUR", "30")), 3600)  # reports per visitor per hour
TIMEOUT_S = 30
CALL_BUDGET_S = 90  # max time one model call may take, retries included
TZ = ZoneInfo("Europe/Zurich")

# Notice periods (calendar days before the start date). Single source of truth:
# the model no longer chooses this number.
NOTICE_DAYS = {"F02": 10, "F05": 14, "F08": 14, "F13": 10, "F14": 2, "F15": 7, "F25": 30, "F26": 30, "F30": 8}
WORKING_DAYS = {"F01": 10, "F29": 14}  # counted Monday-Friday (public holidays not removed: check them)
# Due on a fixed day of the FOLLOWING month (F31: weather notice by the 5th of the next month).
NEXT_MONTH_DAY = {"F31": 5}
KNOWN_FORMS = {f"F{i:02d}" for i in range(1, 33)}
FORM_RE = r"F(?:0[1-9]|[12]\d|3[0-2])"  # F01 to F32

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
        return {"form_id": form_id or "unknown", "error": "Unknown form ID. Use an ID from the library (F01 to F32)."}
    today = _parse_date(report_date)
    if today is None:
        return {"form_id": form_id, "error": "report_date is missing or not a valid date (YYYY-MM-DD)."}
    start = _parse_date(work_start_date)
    if start is None:
        return {"form_id": form_id, "error": "work_start_date is missing or not a valid date (YYYY-MM-DD). Ask the site manager for the exact start date."}
    if form_id in NEXT_MONTH_DAY:
        nxt = date(start.year + (start.month == 12), start.month % 12 + 1, NEXT_MONTH_DAY[form_id])
        notice, deadline = f"by day {NEXT_MONTH_DAY[form_id]} of the following month", nxt
    elif form_id in WORKING_DAYS:
        notice, deadline, left = f"{WORKING_DAYS[form_id]} working days", start, WORKING_DAYS[form_id]
        while left:
            deadline -= timedelta(days=1)
            if deadline.weekday() < 5:
                left -= 1
    else:
        notice = NOTICE_DAYS.get(form_id, 0)
        deadline = start - timedelta(days=notice)
    days_left = (deadline - today).days
    priority = "🔴 URGENT" if days_left <= 2 else "🟠 SOON" if days_left <= 14 else "🟢 PLANNED"
    n = abs(days_left)
    status = ("Due today" if days_left == 0 else
              f"Deadline passed {n} day{'s' if n != 1 else ''} ago" if days_left < 0 else
              f"{n} day{'s' if n != 1 else ''} left")
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
                "form_id": {"type": "string", "description": "Form ID from the library (F01 to F32), e.g. F01, F02, F13"},
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
_SKIP_SIG = "skip_thought_signature_validator"  # Google's documented value for history made by another model


def _for_model(contents, m, made_by):
    """Gemini 3 attaches thought signatures to its function calls. If the turn switches to another model
    (because the first one is overloaded), those signatures are replaced so the new model accepts the history."""
    if made_by is None or (made_by == m and made_by != "mixed"):
        return contents
    out = json.loads(json.dumps(contents))
    for c in out:
        for p in c.get("parts", []):
            if "thoughtSignature" in p or "functionCall" in p:  # calls made by Mistral have no signature
                p["thoughtSignature"] = _SKIP_SIG
    return out


def _to_mistral(contents):
    """Gemini 'contents' -> Mistral chat messages. Tool-call ids are made up in order (Mistral wants 9 letters/digits)."""
    msgs, pending, n = [{"role": "system", "content": INSTRUCTIONS}], [], 0
    for c in contents:
        parts = c.get("parts", [])
        if c.get("role") == "model":
            calls = []
            for p in parts:
                if "functionCall" in p:
                    n += 1
                    cid = f"c{n:08d}"
                    calls.append({"id": cid, "type": "function", "function": {
                        "name": p["functionCall"]["name"], "arguments": json.dumps(p["functionCall"].get("args", {}))}})
                    pending.append(cid)
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            msgs.append(msg)
        else:
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            for p in parts:
                if "functionResponse" in p:
                    fr = p["functionResponse"]
                    msgs.append({"role": "tool", "name": fr["name"], "tool_call_id": pending.pop(0) if pending else "c00000000",
                                 "content": json.dumps(fr.get("response", {}), ensure_ascii=False)})
            if text:
                msgs.append({"role": "user", "content": text})
    return msgs


def _from_mistral(j):
    """Mistral answer -> the Gemini answer shape the rest of the app reads."""
    msg = ((j.get("choices") or [{}])[0]).get("message") or {}
    parts = []
    if msg.get("content"):
        content = msg["content"]
        if isinstance(content, list):
            content = "".join(x.get("text", "") for x in content if isinstance(x, dict))
        parts.append({"text": content})
    for tc in msg.get("tool_calls") or []:
        try:
            args = json.loads(tc["function"].get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        parts.append({"functionCall": {"name": tc["function"]["name"], "args": args}})
    return {"candidates": [{"content": {"role": "model", "parts": parts}}]} if parts else {}


def _post(m, contents, made_by, timeout):
    """One HTTP call to the model m (Gemini or Mistral). Returns (response, json in Gemini shape)."""
    if m.startswith("mistral"):
        r = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {MISTRAL_KEY}"},
            json={"model": m, "messages": _to_mistral(contents), "temperature": 0.2, "max_tokens": 8192,
                  "tools": [{"type": "function", "function": d} for d in TOOL_DECLARATIONS], "tool_choice": "auto"},
            timeout=timeout,
        )
        j = r.json()
        if r.ok:
            return r, _from_mistral(j)
        err = j.get("message") or j.get("detail") or j.get("error") or f"HTTP {r.status_code}"
        return r, {"error": {"message": str(err)}}
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY},
        json={
            "systemInstruction": {"parts": [{"text": INSTRUCTIONS}]},
            "contents": _for_model(contents, m, made_by),
            "tools": [{"functionDeclarations": TOOL_DECLARATIONS}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
        },
        timeout=timeout,
    )
    return r, r.json()


def call_gemini(contents, models, made_by=None):
    """One call to Gemini. Tries the models in order until one answers; overloads (503) and timeouts
    are usually short spikes, so it makes 3 rounds with growing waits (revision log #19)."""
    last_err = ""
    waits = [3, 8]
    t0 = time.time()
    for attempt in range(3):
        for m in models:
            left = CALL_BUDGET_S - (time.time() - t0)
            if left < 5:  # do not keep visitors waiting for minutes when Google is overloaded everywhere
                break
            try:
                r, j = _post(m, contents, made_by, min(TIMEOUT_S, left))
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
            key_problem = (r.status_code in (401, 403) or (r.status_code == 400 and "api key" in str(last_err).lower())) \
                and not m.startswith("mistral")  # a bad backup key only skips the backup
            if key_problem:
                # Do not show Google's raw message to visitors; log it for the maintainer.
                print(f"[ChantierPilot] Gemini refused the request ({r.status_code}): {last_err}", flush=True)
                raise RuntimeError("The service is not configured correctly (API key). Please tell the maintainers.")
        if time.time() - t0 > CALL_BUDGET_S - 5:
            break
        if attempt < 2:
            # Free-tier quota (HTTP 429) is per minute: wait longer before the next round.
            quota = "429" in str(last_err) or "quota" in str(last_err).lower()
            time.sleep(20 if quota else waits[attempt])
    if "quota" in str(last_err).lower():
        raise RuntimeError("The free AI quota is used up for the moment. Please wait about a minute and try again.")
    raise RuntimeError("Google's free AI models are all overloaded right now (high demand on Google's side). "
                       "Your note is still in the chat: press 🔄 Retry in a minute or two.")



# ---------------------------------------------------------------- deterministic pre-checks (revision log #13)
# The free-tier lite model sometimes ignores the language rule and the trigger rules, so the code
# does the parts where the rules are known and hands the model explicit hints.
_STOP = {
    "English": ("the and of to a in is was on for with will be we at from tomorrow today worker site painting "
                "workers floor windows crane you can what how do does my me i it this who which when about please").split(),
    "French": ("le la les des du de et un une est sur pour avec demain aujourd on chantier travaux pas peinture "
               "intérieure extérieure bureaux peintres ouvriers pose étage fenêtres grue levage benne trottoir "
               "je vous qui quoi comment quel quelle est-ce mon ma nous").split(),
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


# Explicit language requests ("answer in English", "réponds en anglais") win over detection, and stay
# in force for the rest of the chat until the user asks for another language.
_LANG_NAMES = {
    "English": ["english", "anglais", "englisch", "inglese", "inglês", "ingles", "inglés"],
    "French": ["french", "français", "francais", "französisch", "francese", "francês", "francés"],
    "German": ["german", "allemand", "deutsch", "tedesco", "alemão", "alemán"],
    "Italian": ["italian", "italien", "italienisch", "italiano"],
    "Portuguese": ["portuguese", "portugais", "portugiesisch", "portoghese", "português", "portugués"],
    "Spanish": ["spanish", "espagnol", "spanisch", "spagnolo", "espanhol", "español", "espanol"],
}
_LANG_REQ = re.compile(
    r"\b(?:in|en|auf|em)\s+(" + "|".join(sorted({n for v in _LANG_NAMES.values() for n in v}, key=len, reverse=True)) + r")\b",
    re.I)


def requested_language(texts):
    """Language the user explicitly asked for, most recent request first; None if never asked."""
    for t in reversed(texts):
        m = None
        for m in _LANG_REQ.finditer(t.lower()):
            pass  # keep the last request inside one message
        if m:
            word = m.group(1).lower()
            return next(lang for lang, names in _LANG_NAMES.items() if word in names)
    return None


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
    "F13": ["montage de la grue", "montage grue", "grue à tour", "monter l'échafaudage", "montage de l'échafaudage", "montage d'échafaudage",
            "pose de l'échafaudage", "crane assembly", "tower crane", "erect the scaffold", "scaffolding goes up", "put up scaffolding"],
    "F14": ["borne cadastrale", "borne de géomètre", "borne déplacée", "borne arrachée", "point cadastral", "point fixe", "survey marker", "boundary stone", "cadastral point"],
    "F15": ["fouille", "tranchée", "creuser", "terrassement", "excavation", "sondage", "forage", "trench", "digging", "dig ", "test pit"],
    "F16": ["tram", "métro", "metro", " m1", " m2", "leb", "rails", "ligne de contact", "trolleybus", "arrêt de bus", "bus stop", "tram tracks", "railway tracks"],
    "F17": ["ancrage", "tirant", "paroi berlinoise", "paroi clouée", "ground anchor", "tie-back", "tieback", "soil nail", "berliner wall"],
    "F18": ["courant de chantier", "électricité de chantier", "coffret provisoire", "tableau de chantier", "raccordement provisoire",
            "site power", "temporary electricity", "power box", "distribution board"],
    "F19": ["eau de chantier", "alimentation provisoire", "raccordement d'eau", "compteur d'eau", "branchement d'eau", "site water",
            "temporary water", "water meter", "water connection", "hydrant"],
    "F20": ["eaux de chantier", "pompage", "pomper", "eau dans la fouille", "eaux de lavage", "laitance", "boues", "égout",
            "dewatering", "pumping", "pump out", "wash water", "slurry", "muddy water"],
    "F21": ["nappe", "eaux souterraines", "rabattement", "groundwater", "water table"],
    "F22": ["terre polluée", "terres polluées", "pollution du sol", "hydrocarbure", "mazout", "déchets enfouis", "ancienne décharge",
            "citerne à mazout", "polluted soil", "contaminated", "smell of fuel", "hydrocarbon", "buried waste", "oil tank"],
    "F23": ["terre végétale", "décapage", "matériaux terreux", "mouvement de terres", "évacuation des terres", "dioxine",
            "topsoil", "strip the soil", "dioxin"],
    "F24": ["déchets de chantier", "gravats", "tri des déchets", "bons d'évacuation", "construction waste", "rubble", "demolition waste"],
    "F25": ["changement des fenêtres", "changer les fenêtres", "panneaux solaires", "pompe à chaleur", "cabane de jardin", "repeindre la façade",
            "borne de recharge", "replace the windows", "solar panels", "heat pump", "garden shed", "ev charger"],
    "F26": ["nouvelle construction", "agrandissement", "surélévation", "démolition du bâtiment", "démolir le bâtiment", "permis de construire",
            "new building", "extension", "extra storey", "extra floor", "demolish the building", "building permit"],
    "F27": ["début des travaux", "fin des travaux", "réception de l'ouvrage", "works finished", "handover"],
    "F28": ["macaron", "amende de stationnement", "amendes de stationnement", "parking fine", "parking permit"],
    "F29": ["blécherette", "blecherette", "héliport", "heliport", "aérodrome", "aerodrome", "airfield", "grue de grande hauteur",
            "obstacle à la navigation", "tall crane"],
    "F30": ["travailleurs détachés", "détachés", "sous-traitant français", "sous-traitant étranger", "entreprise étrangère",
            "main-d'œuvre étrangère", "posted workers", "foreign subcontractor", "french subcontractor", "foreign company",
            "workers from abroad", "workers from france", "workers from portugal"],
    "F31": ["intempérie", "intemperie", "arrêté à cause de la pluie", "arrêté à cause de la neige", "arrêté à cause du gel",
            "arrêt pour la pluie", "trop de pluie", "chantier arrêté", "travaux interrompus", "renvoyé les ouvriers", "canicule",
            "stopped because of the rain", "stopped because of the snow", "rained out", "sent the crew home", "work stopped due to",
            "too hot to work", "heatwave"],
    "F32": ["zone piétonne", "livraison en zone", "hors case", "hors des cases", "décharger en zone", "pedestrian zone",
            "unload in the old town", "outside a parking bay", "delivery truck in"],
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
            if role == "assistant":  # links and email drafts added by code are not sent back to the model
                text = re.split(r"\n\n\*\*📝", text)[0]
                text = re.sub(r"\]\((?:https?|mailto):[^)]*\)", "]", text)
            turns.append((role, text))
    return turns[-MAX_HISTORY_MESSAGES:]


def build_contents(message, history, report_date):
    """Gemini contents for this turn. Code pre-checks run over everything the user wrote in the chat."""
    past = _past_turns(history)
    user_texts = [t for r, t in past if r == "user"] + [message]
    all_text = "\n".join(user_texts)
    lang = requested_language(user_texts) or detect_language(message) or detect_language(all_text)
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
    tool_log, models, nudged, made_by = [], MODELS, False, None
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            j, m = call_gemini(contents, models, made_by)
            if models is MODELS:
                yield ("model", m)
            elif m != made_by and made_by != "mixed":
                yield ("model", f"{m} (switched: {made_by} overloaded)")
            # After a switch the history holds turns from two models: always convert it from then on.
            made_by = m if made_by in (None, m) else "mixed"
            # Prefer the same model for the rest of the turn, but fall back to the others if it gets overloaded.
            models = [m] + [x for x in MODELS if x != m]
            content = j["candidates"][0]["content"]
            parts = content.get("parts", [])
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                text = "\n\n".join(p["text"] for p in parts if p.get("text") and not p.get("thought")).strip()
                # Guards (revision log #7 and #14): never accept deadlines or weather verdicts the tools did not compute.
                # If the answer needs a tool that was never called, send the model back once to call it.
                used = {t["name"] for t in tool_log}
                missing = []
                if re.search(rf"\b{FORM_RE}\b", text or "") and "calculate_deadline" not in used:
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


def _fix_dates(md, results):
    """The model sometimes miscopies a tool date (e.g. 2025-11-25 for 2026-11-25).
    Any date in the answer with the same month-day as a tool date but another year is set back to the tool date."""
    for r in results:
        for key in ("date", "deadline", "work_start_date", "next_go_day"):
            d = r.get(key) if isinstance(r, dict) else None
            if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                md = re.sub(rf"\b(?!{d[:4]})\d{{4}}-{d[5:]}\b", d, md)
    return md


# Official page per form (from the Form Library "Source" lines; the page where the form is filed or downloaded).
FORM_LINKS = {
    "F01": "https://www.lausanne.ch/prestations/mobilite-et-amenagements-publics/demande-permis-utilisation-temporaire-domaine-public.html",
    "F02": "https://www-vali.lausanne.ch/dam/jcr:44d57b16-543c-4c61-b2d2-c0b66bc460b5/119_Avis_ouv_chantier_Rev_10_20251022.docx",
    "F03": "https://www.lausanne.ch/prestations/parcs-et-domaines/demande-abattage-arbres.html",
    "F04": "https://www.vd.ch/prestation/demander-une-autorisation-pour-un-abattage-ou-un-elagage-excedant-lentretien-courant-dun-arbre-remarquable-inscrit-a-linventaire-cantonal",
    "F05": "https://www.suva.ch/fr-ch/prevention/par-danger/materiaux-rayonnements-et-situations-a-risque/amiante/les-entreprises-de-desamiantage/annoncer-un-desamiantage",
    "F06": "https://www.suva.ch/fr-ch/prevention/par-danger/materiaux-rayonnements-et-situations-a-risque/amiante/les-entreprises-de-desamiantage/annoncer-un-desamiantage",
    "F07": "https://www.vd.ch/territoire-et-construction/amiante-1/categories/entreprises-dassainissement",
    "F08": "https://www.suva.ch/fr-ch/prevention/conseil-cours-et-offres/annoncer-les-travaux-de-construction",
    "F09": "https://www.lausanne.ch/prestations/urbanisme/permis-d-habiter-ou-utiliser.html",
    "F10": "https://www.vd.ch/dits/dgtl/dac/camac",
    "F11": "https://www.suva.ch/fr-ch/accident/apres-un-accident/declaration-de-sinistre-en-ligne",
    "F12": "https://www.lausanne.ch/vie-pratique/travail/protection-des-travailleurs/employeur/derogation-travail-nuit-ou-dimanche.html",
    "F13": "https://www.lausanne.ch/prestations/mobilite-et-amenagements-publics/demande-permis-utilisation-temporaire-domaine-public.html",
    "F14": "https://www.lausanne.ch/prestations/mobilite-et-amenagements-publics/demande-permis-utilisation-temporaire-domaine-public.html",
    "F15": "https://www.lausanne.ch/prestations/cadastre/commande-plan-cadastral-des-conduites.html",
    "F16": "https://www.lausanne.ch/dam/jcr:baa324f4-23e9-4170-b18b-b4f85ce95d5d/cadastre_adresses_bureaux_techniques.pdf",
    "F17": "https://www.lausanne.ch/prestations/cadastre/ancrages.html",
    "F18": "https://www.lausanne.ch/vie-pratique/energies-et-eau/services-industriels/partenaires/raccordements.html?tab=electricite",
    "F19": "https://www.lausanne.ch/prestations/eau/demande-raccordement-reseau-eau.html",
    "F20": "https://www.lausanne.ch/eauxdechantier",
    "F21": "https://www.vd.ch/environnement/eaux/eaux-souterraines/constructions-dans-les-secteurs-particulierement-menaces-au-s-pp",
    "F22": "https://www.lexfind.ch/tol/24003/de",
    "F23": "https://www.vd.ch/environnement/sols/pollution-des-sols-aux-dioxines/gestion-des-materiaux-terreux",
    "F24": "https://www.vd.ch/themes/environnement/dechets/boite-outils-dechets",
    "F25": "https://www.lausanne.ch/en/prestations/urbanisme/travaux-non-soumis-a-permis-de-construire.html",
    "F26": "https://www.lausanne.ch/prestations/urbanisme/demande-permis-de-construire.html",
    "F27": "https://www.vd.ch/territoire-et-construction/permis-de-construire/les-differentes-etapes-de-la-procedure-du-permis-construire",
    "F28": "https://www.lausanne.ch/macarons-stationnement-entreprises",
    "F29": "https://www.bazl.admin.ch/fr/obstacles-a-la-navigation-aerienne",
    "F30": "https://www.sem.admin.ch/content/sem/fr/home/themen/fza_schweiz-eu-efta/meldeverfahren.html",
    "F31": "https://www.vd.ch/economie/prestations-de-la-direction-generale-de-lemploi-et-du-marche-du-travail-dgem/demande-dindemnite-pour-intemperies",
    "F32": "https://www.lausanne.ch/prestations/police/autorisation-parquer-hors-cases.html",
}
# The actual form to fill in, per form (revision log #21): a document to download, an online form,
# or - when the authority publishes no form - an email to the right address, pre-filled from the report.
FORM_FILL = {
    "F01": ("email", "infopermis@lausanne.ch"),
    "F02": ("word", "https://www-vali.lausanne.ch/dam/jcr:44d57b16-543c-4c61-b2d2-c0b66bc460b5/119_Avis_ouv_chantier_Rev_10_20251022.docx"),
    "F03": ("pdf", "https://www.lausanne.ch/.binaryData/website/path/lausanne/prestations/parcs-et-domaines/demande-abattage-arbres/contentAutogenerated/autogeneratedContainer/col1/02/linkList/0/websitedownload/2_Formulaire_abattage_2024.2024-02-28-14-59-14.pdf"),
    "F04": ("word", "https://www.vd.ch/fileadmin/user_upload/themes/environnement/biodiversite/fichiers_pdf/Patrimoine_arbor%C3%A9/Patrimoine_arbor%C3%A9/MOD_form_derog_abattarbrem.docx"),
    "F05": ("online", "https://www.suva.ch/fr-ch/services/travail-a-declarer?#meld-location=%2Fmelden%2Fasbestsanierung"),
    "F06": ("online", "https://www.suva.ch/fr-ch/services/travail-a-declarer?#meld-location=%2Fmelden%2Fasbestsanierung"),
    "F08": ("online", "https://www.suva.ch/fr-ch/services/travail-a-declarer"),
    "F09": ("pdf", "https://www.lausanne.ch/dam/jcr:069b1192-52e3-43b0-ac57-45d1fecfb73d/Demande-visite-fin-de-travaux-Delivrance-PH-PU.PDF"),
    "F10": ("online", "https://www.vd.ch/territoire-et-construction/permis-de-construire/lacces-a-la-plateforme-actis-camac-passe-par-le-portail-securise"),
    "F11": ("online", "https://www.suva.ch/fr-ch/services/schadenmeldung"),
    "F12": ("online", "https://www.lausanne.ch/prestations/travail/autorisation-modification-temps-de-travail.html"),
    "F13": ("email", "prevacc@lausanne.ch"),
    "F14": ("email", "cadastre@lausanne.ch"),
    "F15": ("online", "https://plans-reseaux.ch"),
    "F16": ("email", "Approbation_travaux@t-l.ch"),
    "F17": ("email", "cadastre@lausanne.ch"),
    "F18": ("online", "https://www.elektroform.ch/online/lausanne/"),
    "F19": ("email", "eau@lausanne.ch"),
    "F20": ("word", "https://www.vd.ch/fileadmin/user_upload/themes/environnement/eau/fichiers_pdf/DIREV_ASS/02.12_PEE_Chantier_construction.docx"),
    "F21": ("online", "https://www.vd.ch/territoire-et-construction/permis-de-construire/lacces-a-la-plateforme-actis-camac-passe-par-le-portail-securise"),
    "F22": ("email", "info.dge@vd.ch"),
    "F24": ("word", "https://vd.ch/fileadmin/user_upload/themes/environnement/dechets/fichiers_doc/231213_Plan_%C3%A9limination_prot%C3%A9g%C3%A9.docx"),
    "F25": ("pdf", "https://www.lausanne.ch/dam/jcr:d6cdf7ac-b627-41dc-aa2e-63cdc49b1028/formulaire-demande-de-travaux.pdf"),
    "F26": ("online", "https://www.vd.ch/territoire-et-construction/permis-de-construire/lacces-a-la-plateforme-actis-camac-passe-par-le-portail-securise"),
    "F28": ("online", "https://www.lausanne.ch/de/prestations/police/demande-macaron-pour-entreprises/formulaire-macaron-multizones-entreprises.html"),
    "F29": ("online", "https://www.bazl.admin.ch/fr/obstacles-a-la-navigation-aerienne"),
    "F30": ("online", "https://www.sem.admin.ch/content/sem/fr/home/themen/fza_schweiz-eu-efta/meldeverfahren.html"),
    "F31": ("email", "intemperies.dgem@vd.ch"),
}
_KIND = {  # label per kind and language
    "English": {"online": "online form", "pdf": "PDF form", "word": "Word form", "email": "email (pre-filled)", "info": "info page",
                "app": "mail app", "copy": "Email text to copy"},
    "French": {"online": "formulaire en ligne", "pdf": "formulaire PDF", "word": "formulaire Word", "email": "e-mail (pré-rempli)",
               "info": "page d'information", "app": "app de messagerie", "copy": "Texte de l'e-mail à copier"},
}
_TITLES = {
    "English": ("📝 Fill in or send", "ℹ️ Official pages"),
    "French": ("📝 Remplir ou envoyer", "ℹ️ Pages officielles"),
    "German": ("📝 Ausfüllen oder senden", "ℹ️ Offizielle Seiten"),
    "Italian": ("📝 Compilare o inviare", "ℹ️ Pagine ufficiali"),
    "Portuguese": ("📝 Preencher ou enviar", "ℹ️ Páginas oficiais"),
    "Spanish": ("📝 Rellenar o enviar", "ℹ️ Páginas oficiales"),
}


def _form_title(fid):
    m = re.search(rf"^## {fid} – (.+)$", INSTRUCTIONS, re.M)
    return m.group(1).strip() if m else fid


def _draft_block(md, fid):
    """The part of the answer's draft section about one form (used as the email body)."""
    m = re.search(rf"(?:^|\n)[#*\s-]*{fid}\b[^\n]*\n(.*?)(?=\n[#*\s-]*{FORM_RE}\b|\n\s*\**(Questions|Fragen|Domande|Perguntas|Preguntas)|\Z)",
                  md, re.S)
    body = m.group(1).strip() if m else ""
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)           # links -> text
    body = re.sub(r"[*_`#>|]", "", body)                              # strip markdown
    return re.sub(r"\n{3,}", "\n\n", body)[:1500]


_ATTACH = {
    "F01": "Pièce jointe : plan d'installation de chantier détaillé (largeur de trottoir et de chaussée restante, mesures de sécurité).",
    "F17": "Pièces jointes (en deux exemplaires) : plans de détail, coupes et élévations montrant la limite du domaine public.",
    "F19": "Pièce jointe : extrait du Registre foncier de moins de 30 jours.",
    "F22": "Mesures prises : déplacement des matériaux suspects arrêté en attendant l'avis d'un spécialiste.",
}


def _email_parts(address, fid, md, site):
    subject = f"{_form_title(fid)}" + (f" – {site}" if site else "")
    body = ("Bonjour,\n\n"
            f"Nous vous adressons la demande / l'annonce suivante : {_form_title(fid)}.\n\n"
            + (_draft_block(md, fid) or "[compléter les informations]")
            + (f"\n\n{_ATTACH[fid]}" if fid in _ATTACH else "")
            + "\n\nMeilleures salutations,\n[nom, entreprise, téléphone]")
    return address, subject, body


def _compose_links(address, subject, body):
    """mailto: only works with a desktop mail app, so web-mail compose links come first (revision log #22)."""
    q = lambda t: quote(t, safe="")
    return {
        "Gmail": f"https://mail.google.com/mail/?view=cm&fs=1&to={q(address)}&su={q(subject)}&body={q(body)}",
        "Outlook": f"https://outlook.office.com/mail/deeplink/compose?to={q(address)}&subject={q(subject)}&body={q(body)}",
        "app": f"mailto:{address}?subject={q(subject)}&body={q(body)}",
    }


def _fill_link(fid, md, site):
    kind, target = FORM_FILL.get(fid, ("info", FORM_LINKS[fid]))
    if kind == "email":
        parts = _email_parts(target, fid, md, site)
        return kind, _compose_links(*parts)["Gmail"], parts
    return kind, target, None


def _add_form_links(md, lang=None):
    """Makes each form ID in the alerts table open the actual form (document, online form or pre-filled email)
    and adds two lines at the end: what to fill in, and the official information pages.
    Links come from code, not from the model, so they cannot be mistyped."""
    table = "\n".join(l for l in md.split("\n") if l.lstrip().startswith("|"))
    ids = [f for f in dict.fromkeys(re.findall(rf"\b{FORM_RE}\b", table)) if f in FORM_LINKS]
    if not ids:
        return md
    sm = re.search(r"(?:Site|Chantier|Baustelle|Cantiere|Obra)\s*:\s*\**\s*([^\n*]+)", md)
    site = sm.group(1).strip() if sm and "mention" not in sm.group(1).lower() else ""
    fills = {f: _fill_link(f, md, site) for f in ids}
    lines = []
    for line in md.split("\n"):
        if line.lstrip().startswith("|"):
            line = re.sub(rf"(?<![\[\w])({FORM_RE})\b(?!\])",
                          lambda m: f"[{m.group(1)}]({fills[m.group(1)][1]})" if m.group(1) in fills else m.group(1), line, count=1)
        lines.append(line)
    md = "\n".join(lines)
    fill_title, info_title = _TITLES.get(lang, _TITLES["English"])
    kinds = _KIND.get(lang, _KIND["English"])
    items, emails = [], []
    for f, (k, u, parts) in fills.items():
        if parts:
            links = _compose_links(*parts)
            items.append(f"{f} – ✉️ [Gmail]({links['Gmail']}) / [Outlook]({links['Outlook']}) / [{kinds['app']}]({links['app']})")
            emails.append((f, parts))
        else:
            items.append(f"[{f} – {kinds[k]}]({u})")
    md += f"\n\n**{fill_title}:** " + " · ".join(items)
    md += f"\n\n**{info_title}:** " + " · ".join(f"[{f}]({FORM_LINKS[f]})" for f in ids)
    for f, (to, subject, body) in emails:  # copy-ready text: works even without any mail account
        md += f"\n\n<details><summary>✉️ {kinds['copy']} – {f}</summary>\n\n```text\nÀ / To: {to}\nObjet / Subject: {subject}\n\n{body}\n```\n\n</details>"
    return md


def _fix_tables(md):
    """Rejoins a table row the model broke across lines (e.g. '| :--- | :---' + blank line + '| :--- |'),
    which otherwise makes the whole table show as raw text."""
    out = []
    for line in md.split("\n"):
        s = line.strip()
        if s.startswith("|") and out:
            j = len(out) - 1
            while j >= 0 and not out[j].strip():
                j -= 1
            prev = out[j].strip() if j >= 0 else ""
            if prev.startswith("|") and not prev.endswith("|"):
                out[j] = out[j].rstrip() + " " + s
                del out[j + 1:]
                continue
        out.append(line)
    # A separator row (| --- | --- |) must have as many cells as the header above it.
    for i in range(1, len(out)):
        if re.fullmatch(r"\s*\|[\s:\-|]+\|?\s*", out[i]) and "-" in out[i] and out[i - 1].strip().startswith("|"):
            cols = out[i - 1].strip().strip("|").count("|") + 1
            out[i] = "|" + " --- |" * cols
    return "\n".join(out)


def _tidy(md, results=(), lang=None):
    """Small markdown repairs for model glitches (e.g. a list item split before a comma), then form links."""
    md = _fix_tables(_fix_dates(md, results))
    md = re.sub(r"\b1 days\b", "1 day", md)
    md = re.sub(r"\b1 jours\b", "1 jour", md)
    md = re.sub(r"\n\s*\n(\s*[,;])", r"\1", md)   # "worker name\n\n, date" -> "worker name, date"
    md = re.sub(r"\n(\s*[,;] )", r"\1", md)
    return _add_form_links(md.strip(), lang)


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
    tools, results = [], []
    yield [status], gr.update(visible=False)
    for event in run_agent(contents, outdoor):
        if event[0] == "model":
            steps.append(f"Model: {event[1]}")
        elif event[0] == "tool":
            results.append(event[3])
            title, line = _tool_bubble(*event[1:])
            tools.append(gr.ChatMessage(role="assistant", content=line, metadata={"title": title, "status": "done"}))
            steps.append(title)
        elif event[0] in ("final", "error"):
            done = gr.ChatMessage(role="assistant", content="\n".join(f"· {s}" for s in steps),
                                  metadata={"title": (f"Used {len(tools)} tool" + ("s" if len(tools) > 1 else "")) if tools else "Done",
                                            "status": "done", "duration": round(time.time() - t0, 1)})
            if event[0] == "error":
                yield [done, *tools, gr.ChatMessage(role="assistant", content=f"⚠️ {event[1]}")], gr.update(visible=False)
                return
            answer = _tidy(event[1], results, detect_language(message))
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
            final = _tidy(event[1], [t[2] for t in tool_log], detect_language(note))
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
I'll turn it into a <b>daily log</b>, flag the <b>Lausanne, Vaud, Suva and federal forms</b> it triggers with their deadlines,
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
.message table, .bot table { display: block; overflow-x: auto; width: 100%; }
.message th, .message td, .bot th, .bot td { min-width: 7.5em; vertical-align: top; }
.message th:first-child, .message td:first-child { min-width: 6em; }
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
                           lines=1, max_lines=12, max_length=MAX_NOTE_CHARS, submit_btn=True, stop_btn=True,
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
