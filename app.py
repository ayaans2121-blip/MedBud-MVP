# app.py — Enzo: Clinical Judgment Trainer (text-only)
# v2: Explicit XP rules, accurate rubric-based scoring, live vitals, realistic flow, no avatar.
#
# Highlights:
# - Variable flow per case (priority → history → exam → investigations → NBS → free text)
# - Confidence calibration scoring on each decision
# - Explicit XP policy (see XP_POLICY) with transparent deductions for hints
# - Rule-based rubric with safety-critical gates (prevents “99/100 when wrong”)
# - Live vitals panel that changes as your decisions affect the patient
# - Streak increments once per calendar day on FIRST completion; multiple cases allowed/day
# - Simple badges for motivation
# - Optional invite gate via ACCESS_CODE
#
# To add more cases later, duplicate CASE and/or wire in a cases.json loader (schema-compatible).

from flask import Flask, render_template_string, request, redirect, url_for, session, make_response
import os, time, uuid, sqlite3, json
from datetime import datetime, date

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")

# ======================= Analytics (SQLite) =======================
DB_PATH = os.path.join(os.path.dirname(__file__), "analytics.db")
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        session_id TEXT,
        event TEXT,
        topic TEXT,
        qid INTEGER,
        correct INTEGER,
        from_review INTEGER,
        from_anchor INTEGER,
        variant TEXT,
        score INTEGER,
        total INTEGER,
        percent INTEGER
    )
    """)
    return conn

def log_event(event, topic=None, qid=None, correct=None, score=None, total=None, percent=None):
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO events (ts, session_id, event, topic, qid, correct, from_review, from_anchor, variant, score, total, percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), session.get("sid"), event, topic, qid,
             int(correct) if correct is not None else None, None, None, "EnzoMVPv2",
             score, total, percent)
        )
        conn.commit(); conn.close()
    except Exception as e:
        print("analytics error:", e)

# ======================= Session bootstrap + optional gate =======================
@app.before_request
def ensure_session_and_gate():
    access_code = os.getenv("ACCESS_CODE")
    if access_code and request.endpoint not in ("gate","static"):
        if not request.cookies.get("access_ok"):
            return redirect(url_for("gate"))

    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    if "xp" not in session:
        session.update(dict(xp=0, streak=0, last_streak_day=None, cases_completed_today=0))

def today_str():
    # If you want strict AU timezone handling, swap to pytz/zoneinfo; for MVP we use server date.
    return date.today().isoformat()

def maybe_increment_streak_once_today():
    # Increment streak ONCE per calendar day on FIRST completion only.
    t = today_str()
    if session.get("last_streak_day") != t:
        session["streak"] = session.get("streak", 0) + 1
        session["last_streak_day"] = t
        session["cases_completed_today"] = 1
    else:
        # already incremented today; allow multiple cases without changing streak again
        session["cases_completed_today"] = session.get("cases_completed_today", 0) + 1

# ======================= XP policy (explicit & transparent) =======================
XP_POLICY = {
    # Base points when correct per stage
    "priority_correct": 30,          # safety-critical
    "investigations_correct": 18,
    "nbs_correct": 28,
    "history_selection": 12,         # full if 3 targeted prompts chosen (4 each, capped)
    "exam_participation": 6,
    "free_text_keywords_cap": 24,    # 6 groups * 4 each
    # Calibration on each decision (0..10 points)
    "calibration_max_per_decision": 10,
    # Hint costs (escalating)
    "hint_costs": [2, 3, 5],         # Nudge, Clue, Teaching
    # Speed bonus for overall run
    "speed_bonus_fast": 8,           # <= 8 minutes
    "speed_bonus_ok": 5,             # <= 12 minutes
    # Safety-critical penalty / gating (if wrong, cap max achievable)
    "safety_wrong_cap": 70,          # if priority safety step missed, total is capped
    # Dangerous-choice malus
    "dangerous_choice_malus": 10     # subtract if a clearly harmful option is chosen
}

def calibration_points(correct, confidence_pct):
    """Reward accurate confidence, penalize miscalibration. Yields 0..10 points."""
    try:
        c = max(0, min(100, int(confidence_pct)))
    except:
        c = 50
    return c // 10 if correct else (100 - c) // 10

def keyword_points(text, keyword_groups, per_group=4, cap=24):
    """Free-text scoring by keyword groups (allow synonyms with |)."""
    if not text: return 0
    t = text.lower()
    score = 0
    for group in keyword_groups:
        if any(k.strip() in t for k in group.split("|")):
            score += per_group
    return min(cap, score)

# ======================= One high-fidelity case (ACS-style) =======================
# More realism: vitals change after key decisions; investigations & NBS aligned to common AU pathways.

CASE = {
    "id": 2001,
    "systems": ["ED", "Cardio"],
    "title": "Chest pain in triage",
    "level": "Intern",
    "flow": ["presenting", "priority", "history", "exam", "investigations", "nbs", "free_text"],

    "presenting": "A 45-year-old presents with 30 minutes of central, pressure-like chest pain, nausea, and diaphoresis.",
    "vitals_initial": {"HR": 98, "BP": "138/84", "RR": 18, "SpO2": "98% RA", "Temp": "36.8°C"},

    "priority": {
        "prompt": "Immediate priority?",
        "options": [
            {"id":"A","text":"CT pulmonary angiogram first"},
            {"id":"B","text":"12-lead ECG within 10 minutes of arrival","correct":True,"safety_critical":True},
            {"id":"C","text":"Wait for troponin before ECG"},
            {"id":"D","text":"Discharge with outpatient stress test"}
        ],
        "hints": [
            "Nudge: Which step is both fast and changes immediate management?",
            "Clue: Red-flag chest pain needs a bedside test within minutes.",
            "Teaching: Suspected ACS → obtain/read an ECG within 10 minutes."
        ],
        "state_if_correct": {
            "note": "ECG obtained promptly; shows 1 mm ST depression in V4–V6.",
            "vitals_delta": {}  # no deterioration
        },
        "state_if_wrong": {
            "note": "Delay to ECG. Patient more distressed.",
            "vitals_delta": {"HR": +12}  # HR rises, rest unchanged for simplicity
        },
        "dangerous_choices": ["D"]  # dangerously wrong for chest pain
    },

    "history_tips": [
        "Ask radiation/exertion/relief",
        "Ask risk factors/family history",
        "Ask diaphoresis/SOB/red flags"
    ],

    "exam": "General: anxious but alert. Chest clear, S1/S2 normal. No focal neurology.",

    "investigations": {
        "prompt": "Best next investigation to complement ECG and guide pathway?",
        "options": [
            {"id":"A","text":"Serial troponin at appropriate intervals","correct":True},
            {"id":"B","text":"D-dimer first line"},
            {"id":"C","text":"CT brain"},
            {"id":"D","text":"ESR and bone profile only"}
        ],
        "hints": [
            "Nudge: Which biomarker rises with myocardial injury but may be normal very early?",
            "Clue: Use it serially within pathway-based risk stratification."
        ],
        "dangerous_choices": []
    },

    "nbs": {
        "prompt": "Next best step now?",
        "options": [
            {"id":"A","text":"Start oral antibiotics"},
            {"id":"B","text":"Aspirin + pathway-based ACS risk stratification","correct":True},
            {"id":"C","text":"Immediate discharge with GP follow-up"},
            {"id":"D","text":"MRI heart urgently for everyone"}
        ],
        "hints": [
            "Nudge: Treat the dangerous possibility first while refining risk.",
            "Clue: Antiplatelet + pathway-based risk tools are paired."
        ],
        "dangerous_choices": ["C"]  # unsafe discharge
    },

    "free_text_prompt": "Free text (2–4 lines): likely diagnosis and immediate plan (include monitoring/escalation).",
    "free_text_keywords": [
        "ecg|twelve-lead", "aspirin|antiplatelet", "troponin",
        "acs|nstemi|stemi", "risk|pathway|stratification", "monitor|telemetry|reassess"
    ],

    "feedback": {
        "rationale_html": "<p><b>ECG first (≤10 min)</b> for suspected ACS; complement with serial troponins and pathway-based risk stratification. Start antiplatelet when indicated. Do not delay ECG for labs/imaging.</p>",
        "takeaways": [
            "Red-flag chest pain → ECG within 10 minutes.",
            "Use serial troponin and ACS pathways; treat as ACS until ruled out.",
            "Prioritise time-critical actions before downstream imaging."
        ],
        "anz_ref": "Aligned with common AU ED/ACS pathways (ECG ≤10 min; pathway-based assessment)."
    }
}

# --- Optional loader (future): uncomment to load case from cases.json (same schema) ---
# try:
#     with open(os.path.join(os.path.dirname(__file__), "cases.json"), "r", encoding="utf-8") as f:
#         CASE = json.load(f)
# except Exception as e:
#     print("cases.json not loaded (using built-in case):", e)

# ======================= Templates (plain strings; avoid f-strings with Jinja) =======================
BASE_HEAD = """
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
"""

HOME_HTML = """
<!doctype html><html><head>""" + BASE_HEAD + """<title>Enzo — Clinical Judgment Trainer</title></head>
<body class="min-h-screen bg-gradient-to-br from-sky-500 via-indigo-500 to-emerald-500 text-white flex items-center justify-center p-4">
  <div class="w-full max-w-3xl bg-white/15 backdrop-blur-md rounded-2xl p-6 shadow-xl">
    <h1 class="text-3xl font-extrabold">Enzo — Clinical Judgment Trainer</h1>
    <p class="opacity-90 mt-1">Short, realistic reps that train <b>what you do next</b>. Text-only. Confidence-calibrated. Coached with hints.</p>

    <div class="flex flex-wrap gap-2 my-4">
      <span class="px-3 py-1 rounded-full bg-white/20">🔥 Streak: {{ streak }}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">⭐ XP: {{ xp }}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">📅 Today: {{ cases_today }} case(s) completed</span>
    </div>

    <div class="grid gap-3 sm:grid-cols-2">
      <div class="bg-white/10 rounded-xl p-4">
        <h3 class="font-bold">Today’s Case</h3>
        <p class="opacity-90">ACS-style chest pain (Intern level). Live vitals, variable flow, hints, calibration, rubric-based scoring.</p>
      </div>
      <div class="bg-white/10 rounded-xl p-4">
        <h3 class="font-bold">How scoring works</h3>
        <ul class="list-disc ml-5 opacity-90">
          <li>Base points per decision (priority/investigations/NBS)</li>
          <li>Confidence calibration (0–10 each decision)</li>
          <li>Free-text keywords (max 24) + history/exam points</li>
          <li>Hint costs (2/3/5), speed bonus (+8/+5)</li>
          <li>Safety-critical gating & dangerous-choice penalties</li>
        </ul>
      </div>
    </div>

    <form method="post" action="{{ url_for('start_case') }}" class="mt-4">
      <button class="px-5 py-3 rounded-xl font-bold bg-emerald-500 hover:bg-emerald-600">Start Case</button>
    </form>
    <p class="mt-3 text-sm opacity-90">Multiple cases allowed per day. Streak increases once per calendar day on your first completion.</p>
  </div>
</body></html>
"""

CASE_SHELL = """
<!doctype html><html><head>""" + BASE_HEAD + """<title>{{ title }}</title></head>
<body class="min-h-screen bg-slate-900 text-slate-100 p-4">
  <div class="max-w-4xl mx-auto">
    <div class="flex items-center justify-between mb-3">
      <h1 class="text-2xl font-extrabold">{{ title }}</h1>
      <div class="text-sm opacity-80">{{ level }} • {{ systems }}</div>
    </div>

    <!-- Vitals panel -->
    <div class="grid md:grid-cols-3 gap-3 mb-3">
      <div class="md:col-span-2 bg-slate-800/70 rounded-xl p-4">
        <div class="text-sm">Stage {{ stage_num }} / {{ stage_total }}</div>
        <div class="mt-1 text-slate-200 font-semibold">{{ stage_label }}</div>
      </div>
      <div class="bg-slate-800/70 rounded-xl p-4">
        <div class="font-bold mb-1">Vitals</div>
        <div class="grid grid-cols-2 gap-x-2 text-sm">
          <div>HR</div><div>{{ vitals.HR }}</div>
          <div>BP</div><div>{{ vitals.BP }}</div>
          <div>RR</div><div>{{ vitals.RR }}</div>
          <div>SpO₂</div><div>{{ vitals.SpO2 }}</div>
          <div>Temp</div><div>{{ vitals.Temp }}</div>
        </div>
      </div>
    </div>

    <form method="post" class="space-y-4">
      {{ body|safe }}
      <div class="flex gap-2">
        <a href="{{ url_for('home') }}" class="px-4 py-2 rounded-lg bg-slate-700">Quit</a>
        <button name="action" value="continue" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 font-bold">Continue</button>
      </div>
    </form>
  </div>
</body></html>
"""

FEEDBACK_HTML = """
<!doctype html><html><head>""" + BASE_HEAD + """<title>Feedback</title></head>
<body class="min-h-screen bg-gradient-to-br from-emerald-50 to-sky-50 text-slate-900 p-4">
  <div class="max-w-3xl mx-auto bg-white rounded-2xl shadow p-5">
    <h2 class="text-2xl font-extrabold">Case Feedback</h2>
    <div class="flex flex-wrap gap-2 my-3">
      <span class="px-3 py-1 rounded-full bg-emerald-100 text-emerald-900">Score: {{ score }} / 100</span>
      <span class="px-3 py-1 rounded-full bg-indigo-100 text-indigo-900">🔥 Streak: {{ streak }}</span>
      <span class="px-3 py-1 rounded-full bg-amber-100 text-amber-900">⭐ XP: {{ xp }}</span>
    </div>

    <div class="grid gap-4">
      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Why this matters</h3>
        <div class="prose max-w-none">{{ rationale|safe }}</div>
        <p class="text-sm text-slate-600 mt-2 italic">{{ anz_ref }}</p>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Takeaways</h3>
        <ul class="list-disc ml-6">
          {% for t in takeaways %}<li>{{ t }}</li>{% endfor %}
        </ul>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Calibration</h3>
        <p class="text-sm text-slate-700">We reward accurate confidence. High+correct or low+wrong wins; high+wrong loses.</p>
        <ul class="list-disc ml-6">
          <li>Priority: {{ calib.priority }} / 10</li>
          <li>Investigations: {{ calib.investigations }} / 10</li>
          <li>NBS: {{ calib.nbs }} / 10</li>
          <li><b>Avg:</b> {{ calib_avg }} / 10</li>
        </ul>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Badges</h3>
        <div class="flex flex-wrap gap-2">
          {% for b in badges %}<span class="px-3 py-1 rounded-full bg-indigo-100 text-indigo-900 font-semibold">{{ b }}</span>{% endfor %}
          {% if not badges %}<span class="text-slate-600">No badges this time—try a run with no hints, finish <8 min, and stay well-calibrated.</span>{% endif %}
        </div>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">XP Breakdown</h3>
        <ul class="list-disc ml-6 text-sm">
          {% for line in xp_breakdown %}<li>{{ line }}</li>{% endfor %}
        </ul>
      </div>

      <form method="post" action="{{ url_for('finish_feedback') }}">
        <button class="px-5 py-3 rounded-xl font-bold bg-indigo-600 text-white hover:bg-indigo-700">Finish</button>
        <a href="{{ url_for('home') }}" class="ml-2 px-4 py-3 rounded-xl bg-slate-200">Back Home</a>
      </form>
    </div>
  </div>
</body></html>
"""

# ======================= Routes + Case Engine =======================
@app.route("/", methods=["GET"])
def home():
    return render_template_string(
        HOME_HTML,
        streak=session.get("streak",0),
        xp=session.get("xp",0),
        cases_today=session.get("cases_completed_today", 0)
    )

@app.route("/start", methods=["POST"])
def start_case():
    # initialise case state
    session["case"] = {
        "id": CASE["id"],
        "flow": CASE["flow"][:],
        "stage_idx": 0,
        "score": 0,
        "xp_earned": 0,  # running XP for this case
        "start_ts": time.time(),
        "hints_used": {"priority":0, "investigations":0, "nbs":0},
        "decisions": {},      # e.g., {"priority": {"choice":"B","correct":True,"conf":80}}
        "free_text": "",
        "badges": [],
        "vitals": dict(CASE["vitals_initial"]),  # live vitals
        "xp_lines": []        # breakdown strings
    }
    log_event("start_case", topic=",".join(CASE["systems"]), qid=CASE["id"])
    return redirect(url_for("stage"))

def _stage_name(key):
    return {
        "presenting":"Presenting Problem",
        "priority":"Immediate Priority",
        "history":"Targeted History",
        "exam":"Focused Exam/Vitals",
        "investigations":"Investigations",
        "nbs":"Next Best Step",
        "free_text":"Free-text Summary"
    }.get(key, key)

def _apply_vitals_delta(vitals: dict, delta: dict):
    # Apply numeric deltas only; strings like BP remain unchanged for MVP simplicity
    out = dict(vitals)
    for k,v in (delta or {}).items():
        if isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
            out[k] = out.get(k, 0) + v
    return out

def _hint_block(stage_key, used, hints):
    out = "<div class='mt-3 p-3 bg-indigo-950/40 rounded-lg'>"
    for i in range(used):
        out += f"<div class='text-indigo-200 mb-1'>💡 {hints[i]}</div>"
    if used < len(hints):
        cost = XP_POLICY["hint_costs"][min(used, len(XP_POLICY["hint_costs"])-1)]
        out += f"""
        <button name="action" value="hint_{stage_key}" class="mt-2 px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 font-bold">
          Get Hint (–{cost} XP)
        </button>
        """
    else:
        out += "<div class='text-indigo-300 opacity-80'>No more hints.</div>"
    out += "</div>"
    return out

def _render_stage(case_state):
    key = case_state["flow"][case_state["stage_idx"]]
    body = ""
    if key == "presenting":
        body = """
        <div class="bg-slate-800/60 rounded-xl p-4">
          <p class="text-lg">""" + CASE['presenting'] + """</p>
        </div>
        """
    elif key == "priority":
        data = CASE["priority"]
        opts = ""
        for o in data["options"]:
            opts += """
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value='""" + o["id"] + """' class="mr-2 accent-indigo-500"> """ + o["id"] + """) """ + o["text"] + """
            </label>"""
        used = case_state["hints_used"]["priority"]
        hint_block = _hint_block("priority", used, data["hints"])
        prev_conf = case_state["decisions"].get("priority",{}).get("conf",50)
        body = """
        <p class="mb-2">""" + data["prompt"] + """</p>
        """ + opts + """
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value='""" + str(prev_conf) + """' name="confidence" class="w-full">
          <div class="text-sm mt-1 opacity-80">Be honest—calibration matters.</div>
        </div>
        """ + hint_block
    elif key == "history":
        chips = ""
        for h in CASE.get("history_tips", []):
            chips += "<label class='inline-flex items-center gap-2 bg-slate-800 rounded-full px-3 py-2 mr-2 mb-2'><input type='checkbox' name='hx' value='" + h + "' class='accent-emerald-400'><span>" + h + "</span></label>"
        body = "<p class='mb-2'>Pick up to 3 targeted history questions (prioritise):</p>" + chips
    elif key == "exam":
        body = "<p class='mb-2'>Focused exam & vitals:</p><div class='bg-slate-800 p-3 rounded-lg'>" + CASE.get('exam','') + "</div>"
        pr = case_state["decisions"].get("priority")
        if pr:
            evo = CASE["priority"]["state_if_correct"]["note"] if pr["correct"] else CASE["priority"]["state_if_wrong"]["note"]
            body += "<div class='mt-3 bg-indigo-900/40 p-3 rounded-lg'><b>Update:</b> " + evo + "</div>"
    elif key == "investigations":
        inv = CASE.get("investigations")
        opts = ""
        for o in inv["options"]:
            opts += """
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value='""" + o["id"] + """' class="mr-2 accent-indigo-500"> """ + o["id"] + """) """ + o["text"] + """
            </label>"""
        used = case_state["hints_used"]["investigations"]
        hint_block = _hint_block("investigations", used, inv["hints"])
        prev_conf = case_state["decisions"].get("investigations",{}).get("conf",50)
        body = "<p class='mb-2'>" + inv['prompt'] + "</p>" + opts + """
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value='""" + str(prev_conf) + """' name="confidence" class="w-full">
        </div>
        """ + hint_block
    elif key == "nbs":
        nbs = CASE.get("nbs")
        opts = ""
        for o in nbs["options"]:
            opts += """
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value='""" + o["id"] + """' class="mr-2 accent-indigo-500"> """ + o["id"] + """) """ + o["text"] + """
            </label>"""
        used = case_state["hints_used"]["nbs"]
        hint_block = _hint_block("nbs", used, nbs["hints"])
        prev_conf = case_state["decisions"].get("nbs",{}).get("conf",50)
        body = "<p class='mb-2'>" + nbs['prompt'] + "</p>" + opts + """
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value='""" + str(prev_conf) + """' name="confidence" class="w-full">
        </div>
        """ + hint_block
    elif key == "free_text":
        prev = case_state.get("free_text","")
        body = "<p class='mb-2'>" + CASE.get('free_text_prompt','Free text: summary & plan') + "</p>" + \
               "<textarea name='free_text' rows='4' class='w-full rounded-lg p-3 text-black' placeholder='2–4 lines...'>" + prev + "</textarea>" + \
               "<div class='text-sm opacity-80 mt-1'>Include: likely dx, immediate plan, monitoring/escalation.</div>"
    return key, body

@app.route("/stage", methods=["GET","POST"])
def stage():
    case_state = session.get("case")
    if not case_state: return redirect(url_for("home"))
    flow = case_state["flow"]

    # Handle actions
    if request.method == "POST":
        action = request.form.get("action","continue")
        key = flow[case_state["stage_idx"]]

        # Handle Hints (stay on stage)
        if action.startswith("hint_"):
            stage_key = action.split("hint_")[-1]
            if stage_key in case_state["hints_used"]:
                used = case_state["hints_used"][stage_key]
                try:
                    max_hints = len(CASE[stage_key]["hints"])
                except KeyError:
                    max_hints = 0
                if used < max_hints:
                    # XP deduction
                    cost = XP_POLICY["hint_costs"][min(used, len(XP_POLICY["hint_costs"])-1)]
                    session["xp"] = max(0, session.get("xp",0) - cost)
                    case_state["xp_earned"] -= cost
                    case_state["xp_lines"].append(f"-{cost} XP: Hint used ({stage_key}, level {used+1})")
                    case_state["hints_used"][stage_key] += 1
            session["case"] = case_state
            return redirect(url_for("stage"))

        # Otherwise process stage and advance
        # PRIORITY
        if key == "priority":
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            data = CASE["priority"]
            correct_id = next((o["id"] for o in data["options"] if o.get("correct")), None)
            correct = (choice == correct_id)

            # base points + calibration
            if correct:
                case_state["score"] += XP_POLICY["priority_correct"]
                case_state["xp_earned"] += XP_POLICY["priority_correct"]
                case_state["xp_lines"].append(f"+{XP_POLICY['priority_correct']} XP: Correct priority")
                case_state["badges"].append("✅ Perfect Priority")
                # vitals: apply "correct" delta (often none; early stabilisation prevents deterioration)
                case_state["vitals"] = _apply_vitals_delta(case_state["vitals"], CASE["priority"]["state_if_correct"]["vitals_delta"])
            else:
                # safety-critical gate: cap maximum achievable score
                case_state["decisions"]["safety_cap"] = True
                case_state["xp_lines"].append(f"⚠️ Safety-critical missed: score capped at {XP_POLICY['safety_wrong_cap']}")
                case_state["vitals"] = _apply_vitals_delta(case_state["vitals"], CASE["priority"]["state_if_wrong"]["vitals_delta"])
                # dangerous choice penalty
                if choice in CASE["priority"].get("dangerous_choices", []):
                    case_state["score"] -= XP_POLICY["dangerous_choice_malus"]
                    case_state["xp_earned"] -= XP_POLICY["dangerous_choice_malus"]
                    case_state["xp_lines"].append(f"-{XP_POLICY['dangerous_choice_malus']} XP: Dangerous choice")

            cal = calibration_points(correct, conf)
            case_state["score"] += cal
            case_state["xp_earned"] += cal
            case_state["xp_lines"].append(f"+{cal} XP: Calibration (priority)")

            case_state["decisions"]["priority"] = {"choice": choice, "correct": correct, "conf": conf}

            # Add a textual state note for exam stage
            case_state["decisions"]["priority_note"] = CASE["priority"]["state_if_correct"]["note"] if correct else CASE["priority"]["state_if_wrong"]["note"]

            log_event("priority_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"])

        # HISTORY
        elif key == "history":
            chosen = request.form.getlist("hx")[:3]
            pts = min(XP_POLICY["history_selection"], 4*len(chosen))
            case_state["score"] += pts
            case_state["xp_earned"] += pts
            case_state["xp_lines"].append(f"+{pts} XP: Targeted history selection")
            case_state["decisions"]["history"] = {"chosen": chosen}

        # EXAM
        elif key == "exam":
            pts = XP_POLICY["exam_participation"]
            case_state["score"] += pts
            case_state["xp_earned"] += pts
            case_state["xp_lines"].append(f"+{pts} XP: Exam participation")

        # INVESTIGATIONS
        elif key == "investigations":
            inv = CASE["investigations"]
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            corr = next((o["id"] for o in inv["options"] if o.get("correct")), None)
            correct = (choice == corr)

            if correct:
                case_state["score"] += XP_POLICY["investigations_correct"]
                case_state["xp_earned"] += XP_POLICY["investigations_correct"]
                case_state["xp_lines"].append(f"+{XP_POLICY['investigations_correct']} XP: Correct investigation")
            else:
                if choice in inv.get("dangerous_choices", []):
                    case_state["score"] -= XP_POLICY["dangerous_choice_malus"]
                    case_state["xp_earned"] -= XP_POLICY["dangerous_choice_malus"]
                    case_state["xp_lines"].append(f"-{XP_POLICY['dangerous_choice_malus']} XP: Dangerous investigation")

            cal = calibration_points(correct, conf)
            case_state["score"] += cal
            case_state["xp_earned"] += cal
            case_state["xp_lines"].append(f"+{cal} XP: Calibration (investigations)")

            case_state["decisions"]["investigations"] = {"choice": choice, "correct": correct, "conf": conf}
            log_event("investigation_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"])

        # NBS
        elif key == "nbs":
            nbs = CASE["nbs"]
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            corr = next((o["id"] for o in nbs["options"] if o.get("correct")), None)
            correct = (choice == corr)

            if correct:
                case_state["score"] += XP_POLICY["nbs_correct"]
                case_state["xp_earned"] += XP_POLICY["nbs_correct"]
                case_state["xp_lines"].append(f"+{XP_POLICY['nbs_correct']} XP: Correct next step")
            else:
                if choice in nbs.get("dangerous_choices", []):
                    case_state["score"] -= XP_POLICY["dangerous_choice_malus"]
                    case_state["xp_earned"] -= XP_POLICY["dangerous_choice_malus"]
                    case_state["xp_lines"].append(f"-{XP_POLICY['dangerous_choice_malus']} XP: Dangerous next step")

            cal = calibration_points(correct, conf)
            case_state["score"] += cal
            case_state["xp_earned"] += cal
            case_state["xp_lines"].append(f"+{cal} XP: Calibration (NBS)")

            case_state["decisions"]["nbs"] = {"choice": choice, "correct": correct, "conf": conf}
            log_event("nbs_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"])

        # FREE TEXT
        elif key == "free_text":
            ft = (request.form.get("free_text","") or "").strip()
            case_state["free_text"] = ft
            kw_pts = keyword_points(ft, CASE.get("free_text_keywords", []), per_group=4, cap=XP_POLICY["free_text_keywords_cap"])
            case_state["score"] += kw_pts
            case_state["xp_earned"] += kw_pts
            case_state["xp_lines"].append(f"+{kw_pts} XP: Free-text clinical reasoning")

        # Advance stage
        case_state["stage_idx"] += 1
        session["case"] = case_state

        if case_state["stage_idx"] >= len(flow):
            # Speed bonus
            elapsed = time.time() - case_state["start_ts"]
            if elapsed <= 8*60:
                sb = XP_POLICY["speed_bonus_fast"]
            elif elapsed <= 12*60:
                sb = XP_POLICY["speed_bonus_ok"]
            else:
                sb = 0
            if sb:
                case_state["score"] += sb
                case_state["xp_earned"] += sb
                case_state["xp_lines"].append(f"+{sb} XP: Speed bonus")

            # Apply safety cap if missed safety-critical priority
            if case_state["decisions"].get("safety_cap"):
                if case_state["score"] > XP_POLICY["safety_wrong_cap"]:
                    case_state["xp_lines"].append(f"Score capped at {XP_POLICY['safety_wrong_cap']} due to missed safety-critical step")
                case_state["score"] = min(case_state["score"], XP_POLICY["safety_wrong_cap"])

            session["case"] = case_state
            return redirect(url_for("feedback"))

    # Render current stage
    key, body = _render_stage(session["case"])
    return render_template_string(
        CASE_SHELL,
        title=CASE["title"],
        level=CASE["level"],
        systems=", ".join(CASE["systems"]),
        stage_num=session["case"]["stage_idx"]+1,
        stage_total=len(session["case"]["flow"]),
        stage_label=_stage_name(key),
        vitals=session["case"]["vitals"],
        body=body
    )

@app.route("/feedback", methods=["GET"])
def feedback():
    case_state = session.get("case")
    if not case_state: return redirect(url_for("home"))

    # Final rounded score 0..100
    score = max(0, min(100, int(round(case_state["score"]))))

    # Calibration breakdown
    calib = {
        "priority": calibration_points(case_state["decisions"].get("priority",{}).get("correct",False),
                                       case_state["decisions"].get("priority",{}).get("conf",50)) if "priority" in case_state["decisions"] else 0,
        "investigations": calibration_points(case_state["decisions"].get("investigations",{}).get("correct",False),
                                             case_state["decisions"].get("investigations",{}).get("conf",50)) if "investigations" in case_state["decisions"] else 0,
        "nbs": calibration_points(case_state["decisions"].get("nbs",{}).get("correct",False),
                                  case_state["decisions"].get("nbs",{}).get("conf",50)) if "nbs" in case_state["decisions"] else 0,
    }
    calib_avg = round(sum(calib.values())/max(1,len(calib)),1)

    # Badges
    total_hints = sum(case_state["hints_used"].values())
    badges = []
    if total_hints == 0: badges.append("🏅 No Hints")
    if (time.time() - case_state["start_ts"]) <= 8*60: badges.append("⚡ Fast Finish (<8 min)")
    if calib_avg >= 8: badges.append("🎯 Well-Calibrated")
    if case_state["decisions"].get("priority",{}).get("correct"): badges.append("✅ Perfect Priority")

    # Save run details for finish step
    session["last_run"] = {
        "score": score,
        "calib": calib,
        "calib_avg": calib_avg,
        "badges": badges,
        "xp_lines": case_state["xp_lines"][:],
        "xp_case": case_state["xp_earned"]
    }

    fb = CASE["feedback"]
    log_event("case_feedback", topic=",".join(CASE["systems"]), qid=CASE["id"], score=score, total=100, percent=score)

    # XP shown currently is account total; the breakdown list shows what contributed.
    return render_template_string(
        FEEDBACK_HTML,
        score=score,
        streak=session.get("streak",0),
        xp=session.get("xp",0),
        rationale=fb["rationale_html"],
        takeaways=fb["takeaways"],
        anz_ref=fb["anz_ref"],
        calib=type("Obj",(object,),calib)(),  # dot-access in Jinja
        calib_avg=calib_avg,
        badges=badges,
        xp_breakdown=session["last_run"]["xp_lines"]
    )

@app.route("/finish", methods=["POST"])
def finish_feedback():
    # Award XP earned this case to account total (hint costs already deducted during case)
    last = session.get("last_run", {"score":0, "xp_case":0})
    session["xp"] = max(0, session.get("xp",0) + int(last.get("xp_case",0)))
    maybe_increment_streak_once_today()
    log_event("case_done", topic=",".join(CASE["systems"]), qid=CASE["id"], score=last.get("score",0), total=100, percent=last.get("score",0))
    # reset case so user can replay freely
    session.pop("case", None)
    return redirect(url_for("home"))

# ======================= Gate (optional) =======================
@app.route("/gate", methods=["GET","POST"])
def gate():
    access_code = os.getenv("ACCESS_CODE")
    if not access_code:
        return redirect(url_for("home"))
    err = None
    if request.method == "POST":
        if request.form.get("code","").strip() == access_code:
            resp = make_response(redirect(url_for("home")))
            resp.set_cookie("access_ok","1", max_age=60*60*24*60)
            return resp
        err = "Incorrect code."
    return render_template_string("""
    <html><head>""" + BASE_HEAD + """<title>Access</title></head>
    <body class="min-h-screen flex items-center justify-center bg-gradient-to-br from-sky-500 to-indigo-600 text-white">
      <form method="post" class="bg-white/15 backdrop-blur-md p-6 rounded-2xl">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-xl font-extrabold">Enter Invite Code</h2>
        </div>
        <input name="code" class="text-black p-2 rounded-lg mr-2" placeholder="Access code">
        <button class="px-4 py-2 rounded-lg bg-emerald-500 font-bold">Enter</button>
        {% if err %}<div class="text-rose-200 mt-2">{{ err }}</div>{% endif %}
      </form>
    </body></html>
    """, err=err)

# ======================= Export analytics ======================
@app.route("/export.csv")
def export_csv():
    conn = _db()
    cur = conn.execute("SELECT ts,session_id,event,topic,qid,correct,from_review,from_anchor,variant,score,total,percent FROM events ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    csv = "ts,session_id,event,topic,qid,correct,from_review,from_anchor,variant,score,total,percent\n"
    for r in rows:
        csv += ",".join("" if v is None else str(v) for v in r) + "\n"
    resp = make_response(csv)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = "attachment; filename=events.csv"
    return resp

# ======================= Local dev run (Render uses Gunicorn) ======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")), debug=True)
