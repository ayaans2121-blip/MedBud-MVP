# app.py — Enzo (ENSO): Clinical Judgment Trainer (Text-only)
# v4:
# - AUS context & safety cues surfaced on-case and in feedback (“when to call registrar”)
# - Curriculum map: each case lists specific Flinders MD3–MD4 / AU practice outcomes it hits
# - Spaced resurfacing (lightweight): wrong decisions create/advance review tags with due dates
# - “Weak spots” panel on Home with due reviews; Start now prioritises due review tags
# - Kept confidence calibration, hints, strict scoring, vitals evolution, daily streak policy
# - Prep for rotation packs (Neurology first to ship) — structure ready but not enforced yet

from flask import Flask, render_template_string, request, redirect, url_for, session, make_response
import os, time, uuid, sqlite3, math
from datetime import datetime, date, timedelta

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
    # spaced resurfacing store (per-session for MVP)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS spaced (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT,
        tag TEXT,
        interval_idx INTEGER,
        next_due_ts REAL,
        last_result INTEGER,
        created_ts REAL,
        updated_ts REAL
    )
    """)
    return conn

def log_event(event, topic=None, qid=None, correct=None, score=None, total=None, percent=None, from_review=0):
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO events (ts, session_id, event, topic, qid, correct, from_review, from_anchor, variant, score, total, percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), session.get("sid"), event, topic, qid,
             int(correct) if correct is not None else None, int(from_review or 0), None, "EnzoMVPv4",
             score, total, percent)
        )
        conn.commit(); conn.close()
    except Exception as e:
        print("analytics error:", e)

# --------- Spaced resurfacing helpers (lightweight SM-2 style intervals) ----------
INTERVALS_DAYS = [1, 3, 7, 14, 30]  # MVP cadence

def _now_ts(): return time.time()
def _days_from_now(d): return _now_ts() + d*24*60*60

def upsert_spaced_tag(tag: str, success: bool):
    """Create or advance a review card for the given tag based on the result."""
    try:
        conn = _db()
        cur = conn.execute("SELECT id, interval_idx FROM spaced WHERE session_id=? AND tag=?", (session.get("sid"), tag))
        row = cur.fetchone()
        if not row:
            # Start at 0 (due tomorrow if wrong)
            idx = 0 if not success else 1  # if correct first time, push out slightly
            next_due = _days_from_now(INTERVALS_DAYS[idx] if not success else INTERVALS_DAYS[min(idx, len(INTERVALS_DAYS)-1)])
            conn.execute("INSERT INTO spaced (session_id, tag, interval_idx, next_due_ts, last_result, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (session.get("sid"), tag, idx, next_due, int(success), _now_ts(), _now_ts()))
        else:
            _id, idx = row
            if success:
                idx = min(idx + 1, len(INTERVALS_DAYS)-1)
            else:
                idx = max(0, idx - 1)  # make it come back sooner if wrong
            next_due = _days_from_now(INTERVALS_DAYS[idx])
            conn.execute("UPDATE spaced SET interval_idx=?, next_due_ts=?, last_result=?, updated_ts=? WHERE id=?",
                         (idx, next_due, int(success), _now_ts(), _id))
        conn.commit(); conn.close()
    except Exception as e:
        print("spaced error:", e)

def due_spaced_tags(limit=3):
    try:
        conn = _db()
        cur = conn.execute("""SELECT tag FROM spaced 
                              WHERE session_id=? AND next_due_ts<=? 
                              ORDER BY next_due_ts ASC LIMIT ?""",
                           (session.get("sid"), _now_ts(), limit))
        tags = [r[0] for r in cur.fetchall()]
        conn.close()
        return tags
    except Exception as e:
        print("due spaced error:", e)
        return []

def queued_spaced_count():
    try:
        conn = _db()
        cur = conn.execute("SELECT COUNT(*) FROM spaced WHERE session_id=? AND next_due_ts<=?", (session.get("sid"), _now_ts()))
        n = cur.fetchone()[0]; conn.close()
        return n
    except:
        return 0

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
    # MVP uses server day; for AU-specific day switch to zoneinfo later.
    return date.today().isoformat()

def maybe_increment_streak_once_today():
    t = today_str()
    if session.get("last_streak_day") != t:
        session["streak"] = session.get("streak", 0) + 1
        session["last_streak_day"] = t
        session["cases_completed_today"] = 1
    else:
        session["cases_completed_today"] = session.get("cases_completed_today", 0) + 1

# ======================= XP policy / scoring rules =======================
XP_POLICY = {
    "priority_correct": 35,        # safety-critical
    "investigations_correct": 20,
    "nbs_correct": 30,
    "history_rank_points": {1: 6, 2: 4, 3: 2},
    "history_max": 12,
    "history_dup_malus": 4,
    "exam_participation": 6,
    "calibration_max_per_decision": 10,
    "hint_costs": [2, 3, 5],
    "speed_bonus_fast": 5,         # <= 8 min
    "speed_bonus_ok": 3,           # <= 12 min
    "safety_wrong_cap": 70,        # miss safety-critical → cap total at 70
    "one_wrong_cap": 95,           # any wrong MCQ → cap at 95
    "two_wrong_cap": 88,           # two wrong MCQs → cap at 88
    "dangerous_choice_malus": 15
}

def calibration_points(correct, confidence_pct):
    """Reward accurate confidence, penalize miscalibration. Returns 0..10."""
    try:
        c = max(0, min(100, int(confidence_pct)))
    except:
        c = 50
    return c // 10 if correct else (100 - c) // 10

# ======================= Case definition (AUS cues + curriculum + review tags) =======================
CASE = {
    "id": 3001,
    "systems": ["ED", "Cardio"],   # Neurology pack planned as first rotation-wide pack
    "title": "Chest pain in triage",
    "level": "Intern/MD3–4",
    "flow": ["presenting", "priority", "history_rank", "exam", "investigations", "nbs"],

    "presenting": "A 45-year-old presents with 30 minutes of central, pressure-like chest pain, nausea, and diaphoresis.",
    "vitals_initial": {"HR": 98, "BP": "138/84", "RR": 18, "SpO2": "98% RA", "Temp": "36.8°C"},

    # Curriculum mapping (example tags)
    "curriculum_outcomes": [
        "Flinders MD3–4: Acute chest pain assessment & immediate investigations",
        "AUS practice: ECG ≤10 minutes for suspected ACS",
        "Safety & escalation: Recognise red flags and escalate early",
        "Communication: Clear handover & documentation of time-critical actions"
    ],

    # “When to call registrar” (AUS flavour)
    "escalation_cues": [
        "ECG shows ischaemia or persistent pain/hemodynamic compromise",
        "VT/VF, hypotension, syncope, or SpO₂ < 94% on air",
        "Ongoing chest pain not relieved after initial measures",
        "Rising troponins or high-risk features (e.g., GRACE high-risk)"
    ],

    # Priority (safety-critical)
    "priority": {
        "prompt": "Immediate priority?",
        "options": [
            {"id":"A","text":"CT pulmonary angiogram first"},
            {"id":"B","text":"12-lead ECG within 10 minutes of arrival","correct":True,"safety_critical":True, "review_tag":"ACS_ECG_10MIN"},
            {"id":"C","text":"Wait for troponin before ECG"},
            {"id":"D","text":"Discharge with outpatient stress test"}
        ],
        "hints": [
            "Nudge (AUS): Which bedside test is both fast and immediately changes ACS management?",
            "Clue: Australian ED chest pain pathways mandate an immediate ECG.",
            "Teaching: Suspected ACS → obtain/read an ECG within 10 minutes."
        ],
        "state_if_correct": {
            "note": "ECG obtained promptly; 1 mm ST depression in V4–V6.",
            "vitals_delta": {"HR": -2}
        },
        "state_if_wrong": {
            "note": "Delay to ECG. Patient more distressed.",
            "vitals_delta": {"HR": +12}
        },
        "dangerous_choices": ["D"]
    },

    # History ranking (rank 3 items 1→3)
    "history_items": [
        "Ask radiation/exertion/relief",
        "Ask risk factors/family history",
        "Ask diaphoresis/SOB/red flags"
    ],
    "history_desired_order": [
        "Ask diaphoresis/SOB/red flags",
        "Ask radiation/exertion/relief",
        "Ask risk factors/family history"
    ],
    "history_review_tag": "ACS_RED_FLAGS_FIRST",

    "exam": "General: anxious but alert. Chest clear, S1/S2 normal. No focal neurology.",

    "investigations": {
        "prompt": "Best next investigation to complement ECG and guide pathway?",
        "options": [
            {"id":"A","text":"Serial troponin at appropriate intervals","correct":True, "review_tag":"ACS_TROPONIN_SERIAL"},
            {"id":"B","text":"D-dimer first line"},
            {"id":"C","text":"CT brain"},
            {"id":"D","text":"ESR and bone profile only"}
        ],
        "hints": [
            "Nudge: Which biomarker rises with myocardial injury but may be normal very early?",
            "Clue: Use it serially within pathway-based risk stratification."
        ],
        "dangerous_choices": [],
        "state_if_correct": {"note":"You order serial troponins per pathway.","vitals_delta":{"HR": -4, "RR": -1}},
        "state_if_wrong":   {"note":"Work-up is less targeted; progression continues.","vitals_delta":{"HR": +6, "RR": +1}}
    },

    "nbs": {
        "prompt": "Next best step now?",
        "options": [
            {"id":"A","text":"Start oral antibiotics"},
            {"id":"B","text":"Aspirin + pathway-based ACS risk stratification","correct":True, "review_tag":"ACS_ANTIPLATELET_PATHWAY"},
            {"id":"C","text":"Immediate discharge with GP follow-up"},
            {"id":"D","text":"MRI heart urgently for everyone"}
        ],
        "hints": [
            "Nudge: Treat the dangerous possibility first while refining risk.",
            "Clue: Antiplatelet + pathway-based risk tools are paired."
        ],
        "dangerous_choices": ["C"],
        "state_if_correct": {"note":"Given antiplatelet; monitored on telemetry.","vitals_delta":{"HR": -6, "RR": -1}},
        "state_if_wrong":   {"note":"Management delayed; risk increases.","vitals_delta":{"HR": +8, "RR": +2}}
    },

    "feedback": {
        "rationale_html": "<p><b>ECG first (≤10 min)</b> for suspected ACS; complement with serial troponins and pathway-based risk stratification. Start antiplatelet when indicated. Do not delay ECG for labs/imaging.</p>",
        "takeaways": [
            "Red-flag chest pain → ECG within 10 minutes.",
            "Use serial troponin and ACS pathways; treat as ACS until ruled out.",
            "Prioritise time-critical actions before downstream imaging.",
            "Document decisions + escalation thresholds explicitly (AUS context)."
        ],
        "anz_ref": "Aligned with AU ED/ACS pathways (e.g., ECG ≤10 min; pathway-based assessment)."
    }
}

# ======================= Templates =======================
BASE_HEAD = """
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
"""

HOME_HTML = """
<!doctype html><html><head>""" + BASE_HEAD + """<title>Enzo — Clinical Judgment Trainer</title></head>
<body class="min-h-screen bg-gradient-to-br from-sky-500 via-indigo-500 to-emerald-500 text-white flex items-center justify-center p-4">
  <div class="w-full max-w-3xl bg-white/15 backdrop-blur-md rounded-2xl p-6 shadow-xl">
    <h1 class="text-3xl font-extrabold">Enzo — Clinical Judgment Trainer</h1>
    <p class="opacity-90 mt-1">Short, realistic reps that train <b>what you do next</b>. Confidence-calibrated. Coached with hints.</p>

    <div class="flex flex-wrap gap-2 my-4">
      <span class="px-3 py-1 rounded-full bg-white/20">🔥 Streak: {{ streak }}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">⭐ XP: {{ xp }}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">📅 Today: {{ cases_today }} case(s) completed</span>
      <span class="px-3 py-1 rounded-full {% if due_count>0 %}bg-rose-400/80{% else %}bg-white/20{% endif %}">
        🧩 Weak spots due: {{ due_count }}
      </span>
    </div>

    {% if due_tags %}
      <div class="bg-white/15 rounded-xl p-4 mb-3">
        <h3 class="font-bold">Due reviews</h3>
        <p class="opacity-90 text-sm">We'll prioritise a case targeting: {{ due_tags|join(', ') }}</p>
      </div>
    {% endif %}

    <div class="grid gap-3 sm:grid-cols-2">
      <div class="bg-white/10 rounded-xl p-4">
        <h3 class="font-bold">Today’s Case</h3>
        <p class="opacity-90">ACS-style chest pain (Intern/MD3–4). Live vitals, ranking-based history, hints, calibration, strict scoring. AUS safety cues + curriculum links.</p>
      </div>
      <div class="bg-white/10 rounded-xl p-4">
        <h3 class="font-bold">Scoring at a glance</h3>
        <ul class="list-disc ml-5 opacity-90">
          <li>Base points per correct decision</li>
          <li>Confidence calibration (0–10 per MCQ)</li>
          <li>History ranking (1→3: 6/4/2; penalties for duplicates)</li>
          <li>Speed bonus (+5 ≤8m; +3 ≤12m)</li>
          <li>Safety gate (cap 70), wrong caps (95/88), dangerous malus (–15)</li>
        </ul>
      </div>
    </div>

    <form method="post" action="{{ url_for('start_case') }}" class="mt-4">
      <input type="hidden" name="review_prefill" value="{{ (due_tags|join('|')) if due_tags else '' }}">
      <button class="px-5 py-3 rounded-xl font-bold bg-emerald-500 hover:bg-emerald-600">Start Case</button>
    </form>
    <p class="mt-3 text-sm opacity-90">Multiple cases allowed per day. Streak increases once per calendar day on your first completion.</p>

    <div class="mt-6 bg-white/10 rounded-xl p-4">
      <h3 class="font-bold">Curriculum coverage</h3>
      <ul class="list-disc ml-5 opacity-90">
        {% for o in curriculum_outcomes %}<li>{{ o }}</li>{% endfor %}
      </ul>
    </div>
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
        {% if review_targets %}
          <div class="mt-2 text-xs text-amber-300">🎯 Targeting weak spot(s): {{ review_targets|join(', ') }}</div>
        {% endif %}
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
      {% if escalation_cues %}
      <div class="bg-rose-900/30 border border-rose-700/50 rounded-lg p-3">
        <div class="font-semibold mb-1">AUS escalation cues — when to call the registrar</div>
        <ul class="list-disc ml-5 text-sm">
          {% for e in escalation_cues %}<li>{{ e }}</li>{% endfor %}
        </ul>
      </div>
      {% endif %}
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
        <ul class="list-disc ml-6">
          <li>Priority: {{ calib.priority }} / 10</li>
          <li>Investigations: {{ calib.investigations }} / 10</li>
          <li>NBS: {{ calib.nbs }} / 10</li>
          <li><b>Avg:</b> {{ calib_avg }} / 10</li>
        </ul>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Curriculum outcomes covered</h3>
        <ul class="list-disc ml-6">
          {% for o in curriculum_outcomes %}<li>{{ o }}</li>{% endfor %}
        </ul>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">AUS escalation cues — when to call the registrar</h3>
        <ul class="list-disc ml-6">
          {% for e in escalation_cues %}<li>{{ e }}</li>{% endfor %}
        </ul>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Badges</h3>
        <div class="flex flex-wrap gap-2">
          {% for b in badges %}<span class="px-3 py-1 rounded-full bg-indigo-100 text-indigo-900 font-semibold">{{ b }}</span>{% endfor %}
          {% if not badges %}<span class="text-slate-600">No badges this time—try no hints, and finish &lt;8 min, with strong calibration.</span>{% endif %}
        </div>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">XP Breakdown</h3>
        <ul class="list-disc ml-6 text-sm">
          {% for line in xp_breakdown %}<li>{{ line }}</li>{% endfor %}
        </ul>
      </div>

      {% if review_suggestions %}
      <div class="bg-amber-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Next resurfacing</h3>
        <p class="text-sm">We queued reviews for: <b>{{ review_suggestions|join(', ') }}</b>. They’ll reappear on your due list.</p>
      </div>
      {% endif %}

      <form method="post" action="{{ url_for('finish_feedback') }}">
        <button class="px-5 py-3 rounded-xl font-bold bg-indigo-600 text-white hover:bg-indigo-700">Finish</button>
        <a href="{{ url_for('home') }}" class="ml-2 px-4 py-3 rounded-xl bg-slate-200">Back Home</a>
      </form>
    </div>
  </div>
</body></html>
"""

# ======================= Engine helpers =======================
def _stage_name(key):
    return {
        "presenting":"Presenting Problem",
        "priority":"Immediate Priority",
        "history_rank":"Targeted History (Prioritise 1→3)",
        "exam":"Focused Exam/Vitals",
        "investigations":"Investigations",
        "nbs":"Next Best Step"
    }.get(key, key)

def _apply_vitals_delta(vitals: dict, delta: dict):
    out = dict(vitals)
    for k,v in (delta or {}).items():
        if isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
            out[k] = out.get(k, 0) + v
    return out

def _hint_block(stage_key, used, hints):
    costs = XP_POLICY["hint_costs"]
    out = "<div class='mt-3 p-3 bg-indigo-950/40 rounded-lg'>"
    for i in range(used):
        out += f"<div class='text-indigo-200 mb-1'>💡 {hints[i]}</div>"
    if used < len(hints):
        cost = costs[min(used, len(costs)-1)]
        out += f"""
        <button name="action" value="hint_{stage_key}" class="mt-2 px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 font-bold">
          Get Hint (–{cost} XP)
        </button>
        """
    else:
        out += "<div class='text-indigo-300 opacity-80'>No more hints.</div>"
    out += "</div>"
    return out

# ======================= Routes =======================
@app.route("/", methods=["GET"])
def home():
    return render_template_string(
        HOME_HTML,
        streak=session.get("streak",0),
        xp=session.get("xp",0),
        cases_today=session.get("cases_completed_today", 0),
        due_count=queued_spaced_count(),
        due_tags=due_spaced_tags(limit=3),
        curriculum_outcomes=CASE["curriculum_outcomes"]
    )

@app.route("/start", methods=["POST"])
def start_case():
    # pass through any due review tags we intend to target this run
    review_prefill = request.form.get("review_prefill","").strip()
    review_targets = [t for t in review_prefill.split("|") if t] if review_prefill else due_spaced_tags(limit=3)

    session["case"] = {
        "id": CASE["id"],
        "flow": CASE["flow"][:],
        "stage_idx": 0,
        "score": 0,
        "xp_earned": 0,
        "start_ts": time.time(),
        "hints_used": {"priority":0, "investigations":0, "nbs":0},
        "decisions": {},
        "vitals": dict(CASE["vitals_initial"]),
        "xp_lines": [],
        "wrong_mcq_count": 0,
        "review_targets": review_targets or []
    }
    log_event("start_case", topic=",".join(CASE["systems"]), qid=CASE["id"], from_review=1 if review_targets else 0)
    return redirect(url_for("stage"))

def _render_stage(case_state):
    key = case_state["flow"][case_state["stage_idx"]]
    body = ""
    if key == "presenting":
        body = "<div class='bg-slate-800/60 rounded-xl p-4'><p class='text-lg'>" + CASE['presenting'] + "</p></div>"
    elif key == "priority":
        data = CASE["priority"]
        opts = ""
        for o in data["options"]:
            opts += f"""
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value='{o["id"]}' class="mr-2 accent-indigo-500"> {o["id"]}) {o["text"]}
            </label>"""
        used = case_state["hints_used"]["priority"]
        hint_block = _hint_block("priority", used, data["hints"])
        prev_conf = case_state["decisions"].get("priority",{}).get("conf",50)
        body = "<p class='mb-2'>" + data["prompt"] + "</p>" + opts + f"""
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value='{prev_conf}' name="confidence" class="w-full">
        </div>""" + hint_block
    elif key == "history_rank":
        items = CASE["history_items"]
        def dd(name):
            s = f"<select required name='{name}' class='text-black rounded-lg p-2 mr-2'>"
            s += "<option value=''>-- select --</option>"
            for it in items:
                s += f"<option value='{it}'>{it}</option>"
            s += "</select>"
            return s
        body = """
        <p class='mb-2'>Prioritise your first 3 history questions (1 = most urgent/impactful):</p>
        <div class='bg-slate-800 p-3 rounded-lg'>
          <div class='mb-2'><b>Rank 1:</b> """ + dd("rank1") + """</div>
          <div class='mb-2'><b>Rank 2:</b> """ + dd("rank2") + """</div>
          <div class='mb-2'><b>Rank 3:</b> """ + dd("rank3") + """</div>
          <div class='text-sm opacity-80 mt-2'>Tip (AUS): screen red flags first, then characterise pain, then risk context.</div>
        </div>
        """
    elif key == "exam":
        body = "<p class='mb-2'>Focused exam & context:</p><div class='bg-slate-800 p-3 rounded-lg'>" + CASE.get('exam','') + "</div>"
        pr = case_state["decisions"].get("priority")
        if pr:
            evo = pr.get("note","")
            body += "<div class='mt-3 bg-indigo-900/40 p-3 rounded-lg'><b>Update:</b> " + evo + "</div>"
    elif key == "investigations":
        inv = CASE["investigations"]
        opts = ""
        for o in inv["options"]:
            opts += f"""
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value='{o["id"]}' class="mr-2 accent-indigo-500"> {o["id"]}) {o["text"]}
            </label>"""
        used = case_state["hints_used"]["investigations"]
        hint_block = _hint_block("investigations", used, inv["hints"])
        prev_conf = case_state["decisions"].get("investigations",{}).get("conf",50)
        body = "<p class='mb-2'>" + inv['prompt'] + "</p>" + opts + f"""
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value='{prev_conf}' name="confidence" class="w-full">
        </div>""" + hint_block
    elif key == "nbs":
        nbs = CASE["nbs"]
        opts = ""
        for o in nbs["options"]:
            opts += f"""
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value='{o["id"]}' class="mr-2 accent-indigo-500"> {o["id"]}) {o["text"]}
            </label>"""
        used = case_state["hints_used"]["nbs"]
        hint_block = _hint_block("nbs", used, nbs["hints"])
        prev_conf = case_state["decisions"].get("nbs",{}).get("conf",50)
        body = "<p class='mb-2'>" + nbs['prompt'] + "</p>" + opts + f"""
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value='{prev_conf}' name="confidence" class="w-full">
        </div>""" + hint_block
    return key, body

@app.route("/stage", methods=["GET","POST"])
def stage():
    case_state = session.get("case")
    if not case_state: return redirect(url_for("home"))
    flow = case_state["flow"]

    if request.method == "POST":
        action = request.form.get("action","continue")
        key = flow[case_state["stage_idx"]]

        # Hints
        if action.startswith("hint_"):
            stage_key = action.split("hint_")[-1]
            if stage_key in case_state["hints_used"]:
                used = case_state["hints_used"][stage_key]
                hints = CASE.get(stage_key,{}).get("hints",[])
                if used < len(hints):
                    cost = XP_POLICY["hint_costs"][min(used, len(XP_POLICY["hint_costs"])-1)]
                    session["xp"] = max(0, session.get("xp",0) - cost)
                    case_state["xp_earned"] -= cost
                    case_state["xp_lines"].append(f"-{cost} XP: Hint used ({stage_key}, level {used+1})")
                    case_state["hints_used"][stage_key] += 1
            session["case"] = case_state
            return redirect(url_for("stage"))

        # Process stage results
        if key == "priority":
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            data = CASE["priority"]
            correct_id = next((o["id"] for o in data["options"] if o.get("correct")), None)
            correct = (choice == correct_id)

            if correct:
                pts = XP_POLICY["priority_correct"]
                case_state["score"] += pts; case_state["xp_earned"] += pts
                case_state["xp_lines"].append(f"+{pts} XP: Correct priority")
                case_state["vitals"] = _apply_vitals_delta(case_state["vitals"], data["state_if_correct"]["vitals_delta"])
                note = data["state_if_correct"]["note"]
                # advance review tag positively
                tag = next((o.get("review_tag") for o in data["options"] if o.get("correct")), None)
                if tag: upsert_spaced_tag(tag, success=True)
            else:
                case_state["decisions"]["safety_cap"] = True
                case_state["xp_lines"].append(f"⚠️ Safety-critical missed: score capped at {XP_POLICY['safety_wrong_cap']}")
                case_state["vitals"] = _apply_vitals_delta(case_state["vitals"], data["state_if_wrong"]["vitals_delta"])
                note = data["state_if_wrong"]["note"]
                case_state["wrong_mcq_count"] += 1
                if choice in data.get("dangerous_choices", []):
                    mal = XP_POLICY["dangerous_choice_malus"]
                    case_state["score"] -= mal; case_state["xp_earned"] -= mal
                    case_state["xp_lines"].append(f"-{mal} XP: Dangerous choice")
                # schedule review tag negatively
                # identify chosen option's tag if any; else use correct tag
                tag = None
                for o in data["options"]:
                    if o["id"] == choice and o.get("review_tag"):
                        tag = o["review_tag"]
                if not tag:
                    tag = next((o.get("review_tag") for o in data["options"] if o.get("correct")), None)
                if tag: upsert_spaced_tag(tag, success=False)

            cal = calibration_points(correct, conf)
            case_state["score"] += cal; case_state["xp_earned"] += cal
            case_state["xp_lines"].append(f"+{cal} XP: Calibration (priority)")

            case_state["decisions"]["priority"] = {"choice": choice, "correct": correct, "conf": conf, "note": note}
            log_event("priority_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"],
                      from_review=1 if case_state.get("review_targets") else 0)

        elif key == "history_rank":
            r1 = request.form.get("rank1")
            r2 = request.form.get("rank2")
            r3 = request.form.get("rank3")
            chosen = [r1, r2, r3]
            desired = CASE["history_desired_order"]
            pts = 0
            if r1: pts += XP_POLICY["history_rank_points"].get(1,0) if r1 == desired[0] else 0
            if r2: pts += XP_POLICY["history_rank_points"].get(2,0) if r2 == desired[1] else 0
            if r3: pts += XP_POLICY["history_rank_points"].get(3,0) if r3 == desired[2] else 0
            seen = [c for c in chosen if c]
            if len(set(seen)) != len(seen):
                dup_pen = XP_POLICY["history_dup_malus"]
                pts -= dup_pen
                case_state["xp_lines"].append(f"-{dup_pen} XP: Duplicate/missing history ranking")
            pts = max(0, min(XP_POLICY["history_max"], pts))
            case_state["score"] += pts; case_state["xp_earned"] += pts
            case_state["xp_lines"].append(f"+{pts} XP: History prioritisation")
            case_state["decisions"]["history_rank"] = {"rank1": r1, "rank2": r2, "rank3": r3}
            # review tag: if top slot not red flags, schedule review
            top_correct = (r1 == desired[0])
            upsert_spaced_tag(CASE["history_review_tag"], success=top_correct)

        elif key == "exam":
            pts = XP_POLICY["exam_participation"]
            case_state["score"] += pts; case_state["xp_earned"] += pts
            case_state["xp_lines"].append(f"+{pts} XP: Exam participation")

        elif key == "investigations":
            inv = CASE["investigations"]
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            corr = next((o["id"] for o in inv["options"] if o.get("correct")), None)
            correct = (choice == corr)

            if correct:
                pts = XP_POLICY["investigations_correct"]
                case_state["score"] += pts; case_state["xp_earned"] += pts
                case_state["xp_lines"].append(f"+{pts} XP: Correct investigation")
                case_state["vitals"] = _apply_vitals_delta(case_state["vitals"], inv["state_if_correct"]["vitals_delta"])
                note = inv["state_if_correct"]["note"]
                tag = next((o.get("review_tag") for o in inv["options"] if o.get("correct")), None)
                if tag: upsert_spaced_tag(tag, success=True)
            else:
                case_state["wrong_mcq_count"] += 1
                case_state["vitals"] = _apply_vitals_delta(case_state["vitals"], inv["state_if_wrong"]["vitals_delta"])
                note = inv["state_if_wrong"]["note"]
                if choice in inv.get("dangerous_choices", []):
                    mal = XP_POLICY["dangerous_choice_malus"]
                    case_state["score"] -= mal; case_state["xp_earned"] -= mal
                    case_state["xp_lines"].append(f"-{mal} XP: Dangerous investigation")
                tag = next((o.get("review_tag") for o in inv["options"] if o.get("correct")), None)
                if tag: upsert_spaced_tag(tag, success=False)

            cal = calibration_points(correct, conf)
            case_state["score"] += cal; case_state["xp_earned"] += cal
            case_state["xp_lines"].append(f"+{cal} XP: Calibration (investigations)")
            case_state["decisions"]["investigations"] = {"choice": choice, "correct": correct, "conf": conf, "note": note}
            log_event("investigation_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"],
                      from_review=1 if case_state.get("review_targets") else 0)

        elif key == "nbs":
            nbs = CASE["nbs"]
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            corr = next((o["id"] for o in nbs["options"] if o.get("correct")), None)
            correct = (choice == corr)

            if correct:
                pts = XP_POLICY["nbs_correct"]
                case_state["score"] += pts; case_state["xp_earned"] += pts
                case_state["xp_lines"].append(f"+{pts} XP: Correct next step")
                case_state["vitals"] = _apply_vitals_delta(case_state["vitals"], nbs["state_if_correct"]["vitals_delta"])
                note = nbs["state_if_correct"]["note"]
                tag = next((o.get("review_tag") for o in nbs["options"] if o.get("correct")), None)
                if tag: upsert_spaced_tag(tag, success=True)
            else:
                case_state["wrong_mcq_count"] += 1
                case_state["vitals"] = _apply_vitals_delta(case_state["vitals"], nbs["state_if_wrong"]["vitals_delta"])
                note = nbs["state_if_wrong"]["note"]
                if choice in nbs.get("dangerous_choices", []):
                    mal = XP_POLICY["dangerous_choice_malus"]
                    case_state["score"] -= mal; case_state["xp_earned"] -= mal
                    case_state["xp_lines"].append(f"-{mal} XP: Dangerous next step")
                tag = next((o.get("review_tag") for o in nbs["options"] if o.get("correct")), None)
                if tag: upsert_spaced_tag(tag, success=False)

            cal = calibration_points(correct, conf)
            case_state["score"] += cal; case_state["xp_earned"] += cal
            case_state["xp_lines"].append(f"+{cal} XP: Calibration (NBS)")
            case_state["decisions"]["nbs"] = {"choice": choice, "correct": correct, "conf": conf, "note": note}
            log_event("nbs_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"],
                      from_review=1 if case_state.get("review_targets") else 0)

        # advance
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
                case_state["score"] += sb; case_state["xp_earned"] += sb
                case_state["xp_lines"].append(f"+{sb} XP: Speed bonus")

            # Apply caps
            if case_state["decisions"].get("safety_cap"):
                case_state["xp_lines"].append(f"Score capped at {XP_POLICY['safety_wrong_cap']} (safety-critical miss)")
                case_state["score"] = min(case_state["score"], XP_POLICY["safety_wrong_cap"])
            else:
                wc = case_state["wrong_mcq_count"]
                if wc >= 2:
                    cap = XP_POLICY["two_wrong_cap"]
                    case_state["xp_lines"].append(f"Score capped at {cap} (two wrong MCQs)")
                    case_state["score"] = min(case_state["score"], cap)
                elif wc == 1:
                    cap = XP_POLICY["one_wrong_cap"]
                    case_state["xp_lines"].append(f"Score capped at {cap} (one wrong MCQ)")
                    case_state["score"] = min(case_state["score"], cap)

            session["case"] = case_state
            return redirect(url_for("feedback"))

    # Render
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
        body=body,
        escalation_cues=CASE["escalation_cues"],
        review_targets=session["case"].get("review_targets", [])
    )

@app.route("/feedback", methods=["GET"])
def feedback():
    case_state = session.get("case")
    if not case_state: return redirect(url_for("home"))

    score = max(0, min(100, int(round(case_state["score"]))))

    # Calibration breakdown
    def c_of(key):
        d = case_state["decisions"].get(key, {})
        if not d: return 0
        return calibration_points(d.get("correct",False), d.get("conf",50))
    calib = {"priority": c_of("priority"), "investigations": c_of("investigations"), "nbs": c_of("nbs")}
    calib_avg = round(sum(calib.values())/3.0, 1)

    # Badges
    total_hints = sum(case_state["hints_used"].values())
    badges = []
    if total_hints == 0: badges.append("🏅 No Hints")
    if (time.time() - case_state["start_ts"]) <= 8*60: badges.append("⚡ Fast Finish (<8 min)")
    if calib_avg >= 8: badges.append("🎯 Well-Calibrated")
    if case_state["decisions"].get("priority",{}).get("correct"): badges.append("✅ Perfect Priority")

    session["last_run"] = {
        "score": score,
        "calib": calib,
        "calib_avg": calib_avg,
        "badges": badges,
        "xp_lines": case_state["xp_lines"][:],
        "xp_case": case_state["xp_earned"]
    }

    # Figure out which review tags to display as suggestions this run (wrong answers)
    review_suggestions = []
    for key in ("priority", "investigations", "nbs"):
        d = case_state["decisions"].get(key, {})
        if d and not d.get("correct"):
            # map to known tags
            if key == "priority":
                tag = next((o.get("review_tag") for o in CASE["priority"]["options"] if o.get("correct")), None)
            elif key == "investigations":
                tag = next((o.get("review_tag") for o in CASE["investigations"]["options"] if o.get("correct")), None)
            else:
                tag = next((o.get("review_tag") for o in CASE["nbs"]["options"] if o.get("correct")), None)
            if tag: review_suggestions.append(tag)

    fb = CASE["feedback"]
    log_event("case_feedback", topic=",".join(CASE["systems"]), qid=CASE["id"], score=score, total=100, percent=score,
              from_review=1 if case_state.get("review_targets") else 0)

    return render_template_string(
        FEEDBACK_HTML,
        score=score,
        streak=session.get("streak",0),
        xp=session.get("xp",0),
        rationale=fb["rationale_html"],
        takeaways=fb["takeaways"],
        anz_ref=fb["anz_ref"],
        calib=type("Obj",(object,),calib)(),
        calib_avg=calib_avg,
        badges=badges,
        xp_breakdown=session["last_run"]["xp_lines"],
        curriculum_outcomes=CASE["curriculum_outcomes"],
        escalation_cues=CASE["escalation_cues"],
        review_suggestions=review_suggestions
    )

@app.route("/finish", methods=["POST"])
def finish_feedback():
    last = session.get("last_run", {"score":0, "xp_case":0})
    session["xp"] = max(0, session.get("xp",0) + int(last.get("xp_case",0)))
    maybe_increment_streak_once_today()
    log_event("case_done", topic=",".join(CASE["systems"]), qid=CASE["id"], score=last.get("score",0), total=100, percent=last.get("score",0))
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
    from flask import make_response as _mr
    resp = _mr(csv)
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = "attachment; filename=events.csv"
    return resp

# ======================= Local run (Render uses Gunicorn) ======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")), debug=True)
