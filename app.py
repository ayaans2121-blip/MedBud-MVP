# app.py — Enzo (ENSO): Clinical Judgment Trainer — v5
# Changes:
# - Block picker on Home (Cardio enabled; others shown disabled for roadmap)
# - Judgment-first case structure (rank history -> priority -> focused exam ->
#   ECG read -> choose initial order-set (checkbox) -> labs review -> imaging panel with overlays ->
#   investigations -> next-best-step -> escalation/hand-off)
# - Deeper feedback with explicit AU cues + curriculum LO mapping (Cardio LOs)
# - Personalized session header (name + rotation)
# - Spaced resurfacing (same as v4)
# - Imaging overlay scaffold (drop /static/cxr_sample.jpg; CSS overlays toggled in UI)

from flask import Flask, render_template_string, request, redirect, url_for, session, make_response, send_from_directory
import os, time, uuid, sqlite3
from datetime import datetime, date

app = Flask(__name__, static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")

# ======================= Analytics (SQLite) =======================
DB_PATH = os.path.join(os.path.dirname(__file__), "analytics.db")

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, session_id TEXT, event TEXT, topic TEXT, qid INTEGER,
        correct INTEGER, from_review INTEGER, from_anchor INTEGER, variant TEXT,
        score INTEGER, total INTEGER, percent INTEGER
    )""")
    conn.execute("""
    CREATE TABLE IF NOT EXISTS spaced (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT, tag TEXT, interval_idx INTEGER,
        next_due_ts REAL, last_result INTEGER, created_ts REAL, updated_ts REAL
    )""")
    return conn

def _now_ts(): return time.time()
def _days_from_now(d): return _now_ts() + d*86400

def log_event(event, topic=None, qid=None, correct=None, score=None, total=None, percent=None, from_review=0):
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO events (ts,session_id,event,topic,qid,correct,from_review,from_anchor,variant,score,total,percent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), session.get("sid"), event, topic, qid,
             int(correct) if correct is not None else None, int(from_review or 0), None, "EnzoMVPv5",
             score, total, percent)
        )
        conn.commit(); conn.close()
    except Exception as e:
        print("analytics error:", e)

# --------- Spaced resurfacing (lightweight) ----------
INTERVALS_DAYS = [1, 3, 7, 14, 30]

def upsert_spaced_tag(tag: str, success: bool):
    try:
        conn = _db()
        cur = conn.execute("SELECT id, interval_idx FROM spaced WHERE session_id=? AND tag=?",
                           (session.get("sid"), tag))
        row = cur.fetchone()
        if not row:
            idx = 0 if not success else 1
            next_due = _days_from_now(INTERVALS_DAYS[idx])
            conn.execute("INSERT INTO spaced (session_id,tag,interval_idx,next_due_ts,last_result,created_ts,updated_ts) VALUES (?,?,?,?,?,?,?)",
                         (session.get("sid"), tag, idx, next_due, int(success), _now_ts(), _now_ts()))
        else:
            _id, idx = row
            idx = (min(idx + 1, len(INTERVALS_DAYS)-1) if success else max(0, idx - 1))
            next_due = _days_from_now(INTERVALS_DAYS[idx])
            conn.execute("UPDATE spaced SET interval_idx=?, next_due_ts=?, last_result=?, updated_ts=? WHERE id=?",
                         (idx, next_due, int(success), _now_ts(), _id))
        conn.commit(); conn.close()
    except Exception as e:
        print("spaced error:", e)

def due_spaced_tags(limit=4):
    try:
        conn = _db()
        cur = conn.execute("""SELECT tag FROM spaced 
                              WHERE session_id=? AND next_due_ts<=?
                              ORDER BY next_due_ts ASC LIMIT ?""",
                           (session.get("sid"), _now_ts(), limit))
        tags = [r[0] for r in cur.fetchall()]
        conn.close()
        return tags
    except:
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
    if access_code and request.endpoint not in ("gate","static_file","static"):
        if not request.cookies.get("access_ok"):
            return redirect(url_for("gate"))
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    if "xp" not in session:
        session.update(dict(xp=0, streak=0, last_streak_day=None, cases_completed_today=0))
    # personalize defaults
    session.setdefault("user_name", "")
    session.setdefault("rotation", "Cardiology")
    session.setdefault("block", "Cardiology")

def today_str(): return date.today().isoformat()

def maybe_increment_streak_once_today():
    t = today_str()
    if session.get("last_streak_day") != t:
        session["streak"] = session.get("streak", 0) + 1
        session["last_streak_day"] = t
        session["cases_completed_today"] = 1
    else:
        session["cases_completed_today"] = session.get("cases_completed_today", 0) + 1

# ======================= Scoring / XP policy =======================
XP_POLICY = {
    "priority_correct": 35,
    "investigations_correct": 20,
    "nbs_correct": 30,
    "history_rank_points": {1: 6, 2: 4, 3: 2},
    "history_max": 12,
    "history_dup_malus": 4,
    "exam_participation": 6,
    "ecg_interpretation_full": 12,  # structured checklist
    "orders_points_required_each": 5,  # per required order in initial order-set
    "orders_malus_contra": 8,         # penalty for contra/unsafe order
    "labs_reasoning_full": 12,        # lab interpretation points
    "calibration_max_per_decision": 10,
    "hint_costs": [2,3,5],
    "speed_bonus_fast": 5,
    "speed_bonus_ok": 3,
    "safety_wrong_cap": 70,
    "one_wrong_cap": 95,
    "two_wrong_cap": 88,
    "dangerous_choice_malus": 15
}

def calibration_points(correct, confidence_pct):
    try:
        c = max(0, min(100, int(confidence_pct)))
    except:
        c = 50
    return c // 10 if correct else (100 - c) // 10

# ======================= Cardio Case (Judgment-first) =======================
# NOTE: imaging overlay uses /static/cxr_sample.jpg; add your own file there.
CASE_CARDIO = {
    "block": "Cardiology",
    "id": 4001,
    "systems": ["ED", "Cardio"],
    "title": "Acute Chest Pain at Triage (AUS)",
    "level": "MD3–4 / Intern-ready",
    "flow": [
        "presenting", "priority", "history_rank", "focused_exam",
        "ecg_read", "initial_orders", "labs_review", "imaging_panel",
        "investigations", "nbs", "handoff"
    ],
    "presenting": "A 54-year-old presents with 40 minutes of central, pressure-like chest pain radiating to the left arm with diaphoresis and nausea. Pain 8/10, non-pleuritic, not positional.",
    "vitals_initial": {"HR": 102, "BP": "146/88", "RR": 20, "SpO2": "96% RA", "Temp": "36.9°C"},
    "curriculum_outcomes": [
        # From your CV block LOs (selection)
        "Interpret a simple chest X-ray and CT (Cardio block LO).",
        "Describe the normal ECG and identify major changes.",
        "Discuss the pharmacology of anti-platelet agents, heparins, thrombolytics.",
        "Discuss the pathology of coronary atherosclerosis and acute coronary syndromes.",
        "Discuss differential diagnoses of acute chest pain.",
        "Describe the coronary circulation and factors determining myocardial O2 demand/supply."
    ],
    "escalation_cues": [
        "New ST deviation or dynamic changes on ECG",
        "Hemodynamic compromise (hypotension, syncope, arrhythmia)",
        "Ongoing pain despite initial measures",
        "High-risk features (e.g., GRACE high-risk) or rising troponins"
    ],
    "priority": {
        "prompt": "Immediate priority?",
        "options": [
            {"id":"A","text":"Wait for initial troponin before ECG"},
            {"id":"B","text":"12-lead ECG within 10 minutes of arrival","correct":True,"safety_critical":True,"review_tag":"ACS_ECG_10MIN"},
            {"id":"C","text":"Discharge with next-day stress test"},
            {"id":"D","text":"CT pulmonary angiogram first"}
        ],
        "hints": [
            "Nudge (AUS): Which step is immediate and decision-changing for ACS?",
            "Clue: ED chest pain pathways mandate an immediate ECG."
        ],
        "state_if_correct": {"note":"ECG performed promptly shows 1 mm ST depression V4–V6.", "vitals_delta":{"HR":-2}},
        "state_if_wrong": {"note":"ECG delayed; patient more distressed.", "vitals_delta":{"HR":+10}},
        "dangerous_choices": ["C"]
    },
    "history_items": [
        "Red flags: diaphoresis/SOB/syncope", "Character: radiation/exertion/relief",
        "Risk: CAD risks/family hx"
    ],
    "history_desired_order": [
        "Red flags: diaphoresis/SOB/syncope", "Character: radiation/exertion/relief",
        "Risk: CAD risks/family hx"
    ],
    "history_review_tag": "ACS_RED_FLAGS_FIRST",

    "focused_exam": "Gen: clammy, anxious. JVP not elevated. Lungs: bibasal crackles. CVS: tachy, no loud murmur. Periphery: cool.",
    
    # ECG interpretation (structured checklist → points; confidence too)
    "ecg_read": {
        "prompt": "ECG interpretation (tick all that apply):",
        "checklist": [
            {"id":"rate", "text":"Sinus tachycardia (~100–110 bpm)", "required": True},
            {"id":"stdep", "text":"ST depression in V4–V6", "required": True},
            {"id":"stemi", "text":"ST elevation in contiguous leads", "contra": True},
            {"id":"lbbb", "text":"New LBBB likely", "contra": True},
            {"id":"normal", "text":"Normal ECG", "contra": True}
        ],
        "review_tag_correct": "ECG_ISCHAEMIA_RECOGNITION"
    },

    # Initial order-set (judgment: include all appropriate; exclude unsafe)
    "initial_orders": {
        "prompt": "Initial orders (select all appropriate now):",
        "orders": [
            {"id":"asp", "text":"Aspirin loading dose (if no contraindication)", "required": True},
            {"id":"nitr", "text":"Sublingual GTN if pain and BP adequate", "required": True},
            {"id":"ox", "text":"Oxygen to target SpO₂ > 94% only if hypoxic", "required": False},
            {"id":"cxr", "text":"Portable CXR (within 30–60 min)", "required": True},
            {"id":"morph", "text":"Morphine for refractory pain (judicious)", "required": False},
            {"id":"throm", "text":"Empiric thrombolysis immediately for all", "contra": True},
            {"id":"dsch", "text":"Discharge now with GP follow-up", "contra": True}
        ],
        "review_tags": {
            "required":"ACS_INITIAL_BUNDLE",
            "contra":"ACS_AVOID_UNSAFE"
        }
    },

    # Labs review (reasoning about what matters now)
    "labs_review": {
        "table": [
            ("Hb", "141 g/L"), ("Plt", "220 x10^9/L"), ("Na", "139 mmol/L"), ("K", "3.6 mmol/L"),
            ("Cr", "82 µmol/L"), ("eGFR", ">90 mL/min"), ("Glucose", "5.8 mmol/L"),
            ("hs-TnT (0h)", "12 ng/L (ULN ~14)"), ("D-dimer", "Not indicated")
        ],
        "question": "Which statements are most appropriate now?",
        "options": [
            {"id":"trend", "text":"Serial troponins are required despite non-diagnostic baseline", "correct": True, "tag":"TROPONIN_SERIAL"},
            {"id":"ddimer", "text":"D-dimer should be sent first-line in chest pain", "contra": True},
            {"id":"renal", "text":"Renal function acceptable for contrast if needed", "correct": True, "tag":"RENAL_OK"},
            {"id":"glucose", "text":"Glucose 5.8 suggests DKA driving pain", "contra": True}
        ],
        "points_full": XP_POLICY["labs_reasoning_full"]
    },

    # Imaging panel (overlay toggles; requires /static/cxr_sample.jpg)
    "imaging_panel": {
        "prompt": "Review the portable CXR (toggle overlays):\n- Silhouette borders\n- Costophrenic angles\n- Cardiomediastinal contour",
        "checklist": [
            {"id":"no_wide", "text":"No widened mediastinum or pneumothorax signs", "required": True},
            {"id":"mild_ce", "text":"Mild interstitial markings (possible early congestion)", "required": False},
            {"id":"gross_norm", "text":"Grossly normal CXR", "required": False},
            {"id":"big_pnx", "text":"Large pneumothorax present", "contra": True}
        ],
        "review_tag_correct":"CXR_SYSTEMATIC_READ"
    },

    "investigations": {
        "prompt": "Best next investigation to complement ECG and guide pathway?",
        "options": [
            {"id":"A","text":"Serial hs-troponins at appropriate intervals","correct":True,"review_tag":"ACS_TROPONIN_SERIAL"},
            {"id":"B","text":"D-dimer first line"},
            {"id":"C","text":"CT brain"},
            {"id":"D","text":"ESR and bone profile only"}
        ],
        "state_if_correct": {"note":"Serial troponins ordered per pathway.","vitals_delta":{"HR":-3, "RR":-1}},
        "state_if_wrong": {"note":"Work-up less targeted; risk persists.","vitals_delta":{"HR":+4, "RR":+1}},
        "dangerous_choices": []
    },

    "nbs": {
        "prompt": "Next best step now?",
        "options": [
            {"id":"A","text":"Start oral antibiotics"},
            {"id":"B","text":"Pathway-based ACS risk stratification + antiplatelet","correct":True,"review_tag":"ACS_PATHWAY_ANTIPLATELET"},
            {"id":"C","text":"Immediate discharge with GP follow-up"},
            {"id":"D","text":"MRI for everyone urgently"}
        ],
        "state_if_correct": {"note":"Antiplatelet given; telemetry monitoring.","vitals_delta":{"HR":-5,"RR":-1}},
        "state_if_wrong": {"note":"Management delayed; risk increases.","vitals_delta":{"HR":+6,"RR":+2}},
        "dangerous_choices": ["C"]
    },

    "handoff": {
        "prompt": "Registrar hand-off essentials (select all you would communicate):",
        "items": [
            {"id":"time", "text":"Time of symptom onset & ECG timing", "required": True},
            {"id":"red", "text":"Current red flags/hemodynamics", "required": True},
            {"id":"tx", "text":"Therapies given (aspirin/GTN) & response", "required": True},
            {"id":"risk", "text":"Pretest risk & plan for serial troponins", "required": True},
            {"id":"dsch", "text":"Plan for immediate discharge now", "contra": True}
        ],
        "review_tags": {"required":"HANDOFF_STRUCTURE", "contra":"DISCHARGE_UNSAFE"}
    },

    "feedback": {
        "rationale_html": """
        <p><b>Judgment priorities in AUS ED chest pain:</b> ECG within 10 minutes; symptom-to-ECG & symptom-to-treatment times matter.
        Interpret ECG for ischaemia (ST depression/anterior leads here). Apply a pathway with serial high-sensitivity troponins, give indicated antiplatelet therapy, and monitor. 
        Oxygen only if hypoxic. Use a structured CXR read to exclude immediate threats (PTX, wide mediastinum) and assess for congestion.</p>
        """,
        "takeaways": [
            "ECG ≤10 min; document times; escalate if dynamic changes.",
            "ACS ≠ STEMI only: ST-depression + symptoms are high-risk.",
            "Order-set thinking: aspirin, GTN (if BP ok), telemetry, serial troponins, CXR.",
            "Use systematic CXR/ECG reads; avoid shotgun tests with low yield (e.g., routine D-dimer).",
            "Clear handoff: onset time, red flags, therapies, pathway plan."
        ],
        "anz_ref": "Aligned with common AU ED/ACS pathways (ECG ≤10 min; pathway-based assessment)."
    }
}

# ======================= Templates =======================
BASE_HEAD = """
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
<style>
  /* Imaging overlay scaffolding */
  .img-wrap { position: relative; width: 100%; max-width: 520px; margin: 0 auto; }
  .img-wrap img { width: 100%; display:block; border-radius: 12px; }
  .overlay { position:absolute; inset:0; pointer-events:none; opacity:0; transition:opacity .2s ease; }
  .overlay.show { opacity:0.85; }
  .overlay.silhouette { background: repeating-linear-gradient( 90deg, rgba(0,255,255,.12), rgba(0,255,255,.12) 2px, transparent 2px, transparent 12px ); }
  .overlay.angles { background: repeating-linear-gradient( 0deg, rgba(255,255,0,.15), rgba(255,255,0,.15) 2px, transparent 2px, transparent 20px ); }
  .overlay.contour { box-shadow: inset 0 0 0 4px rgba(255,0,128,.5); }
</style>
"""

HOME_HTML = """
<!doctype html><html><head>""" + BASE_HEAD + """<title>Enzo — Clinical Judgment Trainer</title></head>
<body class="min-h-screen bg-gradient-to-br from-sky-500 via-indigo-500 to-emerald-500 text-white p-4">
  <div class="w-full max-w-4xl mx-auto bg-white/15 backdrop-blur-md rounded-2xl p-6 shadow-xl">
    <div class="flex items-center justify-between">
      <h1 class="text-3xl font-extrabold">Enzo — Clinical Judgment Trainer</h1>
      <form method="post" action="{{ url_for('save_profile') }}" class="flex gap-2 items-center">
        <input name="user_name" value="{{ user_name }}" placeholder="Your name" class="text-black rounded-lg px-2 py-1" />
        <input name="rotation" value="{{ rotation }}" placeholder="Rotation" class="text-black rounded-lg px-2 py-1" />
        <button class="px-3 py-1 rounded-lg bg-emerald-500 hover:bg-emerald-600">Save</button>
      </form>
    </div>
    <p class="opacity-90 mt-1">Short, realistic reps that train <b>what you do next</b>. Confidence-calibrated. Coached with hints. AUS context.</p>

    <div class="flex flex-wrap gap-2 my-4">
      <span class="px-3 py-1 rounded-full bg-white/20">👤 {{ user_name or "Guest" }} • {{ rotation }}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">🔥 Streak: {{ streak }}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">⭐ XP: {{ xp }}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">📅 Today: {{ cases_today }} case(s)</span>
      <span class="px-3 py-1 rounded-full {% if due_count>0 %}bg-rose-400/80{% else %}bg-white/20{% endif %}">
        🧩 Weak spots due: {{ due_count }}
      </span>
    </div>

    <form method="post" action="{{ url_for('start_case') }}" class="grid md:grid-cols-3 gap-3">
      <div class="bg-white/10 rounded-xl p-4 col-span-2">
        <h3 class="font-bold mb-2">Pick a block</h3>
        <label class="flex items-center gap-2 mb-2">
          <input type="radio" name="block" value="Cardiology" checked class="accent-emerald-500" />
          <span>Cardiology</span>
        </label>
        <label class="flex items-center gap-2 mb-2 opacity-50">
          <input type="radio" disabled class="accent-emerald-500" />
          <span>Neurology (coming soon)</span>
        </label>
        <label class="flex items-center gap-2 mb-2 opacity-50">
          <input type="radio" disabled class="accent-emerald-500" />
          <span>Geriatrics (coming soon)</span>
        </label>
        <input type="hidden" name="review_prefill" value="{{ (due_tags|join('|')) if due_tags else '' }}">
        <button class="mt-2 px-5 py-3 rounded-xl font-bold bg-emerald-500 hover:bg-emerald-600">Start Case</button>
        <p class="mt-2 text-sm opacity-80">We’ll prioritise any due weak-spot tags: {{ due_tags|join(', ') if due_tags else 'none due' }}.</p>
      </div>

      <div class="bg-white/10 rounded-xl p-4">
        <h3 class="font-bold">Scoring at a glance</h3>
        <ul class="list-disc ml-5 opacity-90 text-sm">
          <li>Judgment > MCQ: structured ECG & order-set, labs reasoning, ranking</li>
          <li>Confidence calibration (0–10 per decision)</li>
          <li>Speed bonus (+5 ≤8m; +3 ≤12m), safety caps, dangerous malus</li>
        </ul>
      </div>
    </form>

    <div class="mt-6 bg-white/10 rounded-xl p-4">
      <h3 class="font-bold">Cardio Learning Outcomes this case can cover</h3>
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
  <div class="max-w-5xl mx-auto">
    <div class="flex items-center justify-between mb-3">
      <h1 class="text-2xl font-extrabold">{{ title }}</h1>
      <div class="text-sm opacity-80">{{ level }} • {{ systems }}</div>
    </div>

    <div class="grid md:grid-cols-3 gap-3 mb-3">
      <div class="md:col-span-2 bg-slate-800/70 rounded-xl p-4">
        <div class="text-sm">Stage {{ stage_num }} / {{ stage_total }}</div>
        <div class="mt-1 text-slate-200 font-semibold">{{ stage_label }}</div>
        {% if review_targets %}<div class="mt-2 text-xs text-amber-300">🎯 Targeting weak spot(s): {{ review_targets|join(', ') }}</div>{% endif %}
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

      {% if show_imaging %}
      <div class="bg-slate-800/70 rounded-xl p-4">
        <h3 class="font-bold mb-2">Portable CXR (toggle overlays)</h3>
        <div class="img-wrap">
          <img src="{{ url_for('static', filename='cxr_sample.jpg') }}" onerror="this.outerHTML='<div class=\\'p-4 bg-slate-700 rounded-lg text-sm\\'>Place a file at <b>static/cxr_sample.jpg</b> to enable the imaging panel overlays.</div>'">
          <div id="ov-sil" class="overlay silhouette"></div>
          <div id="ov-ang" class="overlay angles"></div>
          <div id="ov-con" class="overlay contour"></div>
        </div>
        <div class="flex gap-2 mt-3">
          <button name="toggle_overlay" value="sil" class="px-3 py-1 rounded bg-indigo-600">Silhouette</button>
          <button name="toggle_overlay" value="ang" class="px-3 py-1 rounded bg-indigo-600">Costophrenic</button>
          <button name="toggle_overlay" value="con" class="px-3 py-1 rounded bg-indigo-600">Contour</button>
        </div>
        <script>
          document.addEventListener('click', (e)=>{
            if(e.target.name==='toggle_overlay'){
              e.preventDefault();
              const id = 'ov-'+e.target.value;
              const el = document.getElementById(id);
              if(el){ el.classList.toggle('show'); }
            }
          });
        </script>
      </div>
      {% endif %}

      <div class="bg-rose-900/30 border border-rose-700/50 rounded-lg p-3">
        <div class="font-semibold mb-1">AUS escalation cues — when to call the registrar</div>
        <ul class="list-disc ml-5 text-sm">
          {% for e in escalation_cues %}<li>{{ e }}</li>{% endfor %}
        </ul>
      </div>

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
  <div class="max-w-4xl mx-auto bg-white rounded-2xl shadow p-5">
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
          <li>ECG checklist: {{ calib.ecg }} / 10</li>
          <li>Investigations: {{ calib.investigations }} / 10</li>
          <li>NBS: {{ calib.nbs }} / 10</li>
          <li><b>Avg:</b> {{ calib_avg }} / 10</li>
        </ul>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Learning Outcomes covered (Cardio)</h3>
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
        <h3 class="font-bold mb-1">XP Breakdown</h3>
        <ul class="list-disc ml-6 text-sm">
          {% for line in xp_breakdown %}<li>{{ line }}</li>{% endfor %}
        </ul>
      </div>

      {% if review_suggestions %}
      <div class="bg-amber-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Next resurfacing</h3>
        <p class="text-sm">Queued weak spots: <b>{{ review_suggestions|join(', ') }}</b></p>
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

# ======================= Helpers =======================
def _stage_name(key):
    return {
        "presenting":"Presenting Problem",
        "priority":"Immediate Priority",
        "history_rank":"Targeted History (Prioritise 1→3)",
        "focused_exam":"Focused Exam",
        "ecg_read":"ECG Interpretation",
        "initial_orders":"Initial Orders",
        "labs_review":"Lab Review & Reasoning",
        "imaging_panel":"Imaging Panel (CXR overlays)",
        "investigations":"Investigations",
        "nbs":"Next Best Step",
        "handoff":"Registrar Handoff"
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
        out += f"<button name='action' value='hint_{stage_key}' class='mt-2 px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 font-bold'>Get Hint (–{cost} XP)</button>"
    else:
        out += "<div class='text-indigo-300 opacity-80'>No more hints.</div>"
    out += "</div>"
    return out

def _checklist(name, items, prev):
    html = ""
    for it in items:
        checked = "checked" if prev and it["id"] in prev else ""
        html += f"""
        <label class="block bg-slate-800 p-3 rounded-lg mb-2">
          <input type="checkbox" name="{name}" value="{it['id']}" class="mr-2 accent-indigo-500" {checked}> {it['text']}
        </label>"""
    return html

def _table_html(rows):
    h = "<table class='w-full text-sm'><tbody>"
    for k,v in rows:
        h += f"<tr class='border-b border-slate-700/40'><td class='py-1 pr-3 text-slate-300'>{k}</td><td class='py-1'>{v}</td></tr>"
    h += "</tbody></table>"
    return h

# ======================= Routes =======================
@app.route("/", methods=["GET"])
def home():
    case = CASE_CARDIO
    return render_template_string(
        HOME_HTML,
        user_name=session.get("user_name",""),
        rotation=session.get("rotation","Cardiology"),
        streak=session.get("streak",0),
        xp=session.get("xp",0),
        cases_today=session.get("cases_completed_today",0),
        due_count=queued_spaced_count(),
        due_tags=due_spaced_tags(limit=4),
        curriculum_outcomes=case["curriculum_outcomes"]
    )

@app.route("/profile", methods=["POST"])
def save_profile():
    session["user_name"] = request.form.get("user_name","").strip()
    session["rotation"]  = request.form.get("rotation","").strip() or "Cardiology"
    return redirect(url_for("home"))

@app.route("/start", methods=["POST"])
def start_case():
    session["block"] = request.form.get("block","Cardiology")
    review_prefill = request.form.get("review_prefill","").strip()
    review_targets = [t for t in review_prefill.split("|") if t] if review_prefill else due_spaced_tags(limit=4)
    case = CASE_CARDIO  # only cardio defined for now

    session["case"] = {
        "id": case["id"], "flow": case["flow"][:], "stage_idx": 0,
        "score": 0, "xp_earned": 0, "start_ts": time.time(),
        "hints_used": {"priority":0, "investigations":0, "nbs":0, "ecg_read":0, "initial_orders":0, "labs_review":0},
        "decisions": {}, "vitals": dict(case["vitals_initial"]),
        "xp_lines": [], "wrong_mcq_count": 0, "review_targets": review_targets or []
    }
    log_event("start_case", topic=",".join(case["systems"]), qid=case["id"], from_review=1 if review_targets else 0)
    return redirect(url_for("stage"))

def _render_stage(state):
    case = CASE_CARDIO
    key = state["flow"][state["stage_idx"]]
    body = ""
    if key == "presenting":
        body = f"<div class='bg-slate-800/60 rounded-xl p-4'><p class='text-lg'>{case['presenting']}</p></div>"
    elif key == "priority":
        data = case["priority"]; opts = ""
        for o in data["options"]:
            opts += f"<label class='block bg-slate-800 p-3 rounded-lg mb-2'><input required type='radio' name='choice' value='{o['id']}' class='mr-2 accent-indigo-500'> {o['id']}) {o['text']}</label>"
        used = state["hints_used"]["priority"]; hint_block = _hint_block("priority", used, data.get("hints",[]))
        prev_conf = state["decisions"].get("priority",{}).get("conf",50)
        body = f"<p class='mb-2'>{data['prompt']}</p>{opts}<div class='mt-3 p-3 bg-slate-800 rounded-lg'><label class='block font-semibold mb-1'>Confidence (0–100%)</label><input type='range' min='0' max='100' value='{prev_conf}' name='confidence' class='w-full'></div>{hint_block}"
    elif key == "history_rank":
        items = case["history_items"]
        def dd(name):
            s = f"<select required name='{name}' class='text-black rounded-lg p-2 mr-2'>"
            s += "<option value=''>-- select --</option>"
            for it in items: s += f"<option value='{it}'>{it}</option>"
            s += "</select>"; return s
        body = f"""
        <p class='mb-2'>Prioritise your first 3 history questions (1 = most urgent/impactful):</p>
        <div class='bg-slate-800 p-3 rounded-lg'>
          <div class='mb-2'><b>Rank 1:</b> {dd('rank1')}</div>
          <div class='mb-2'><b>Rank 2:</b> {dd('rank2')}</div>
          <div class='mb-2'><b>Rank 3:</b> {dd('rank3')}</div>
          <div class='text-sm opacity-80 mt-2'>Tip: red flags → character → risk context.</div>
        </div>"""
    elif key == "focused_exam":
        body = "<p class='mb-2'>Focused exam & context:</p><div class='bg-slate-800 p-3 rounded-lg'>" + case.get('focused_exam','') + "</div>"
        pr = state["decisions"].get("priority")
        if pr: body += "<div class='mt-3 bg-indigo-900/40 p-3 rounded-lg'><b>Update:</b> " + pr.get("note","") + "</div>"
    elif key == "ecg_read":
        ecg = case["ecg_read"]
        prev = state["decisions"].get("ecg_read",{}).get("ticks",[])
        body = f"<p class='mb-2'>{ecg['prompt']}</p>" + _checklist("ecg_tick", ecg["checklist"], prev)
        used = state["hints_used"]["ecg_read"]
        body += _hint_block("ecg_read", used, ["Nudge: focus on rate, rhythm, ST segments in V4–V6 here."])
        body += "<div class='text-xs opacity-70 mt-2'>We score by required picks chosen and contraindicated picks avoided.</div>"
    elif key == "initial_orders":
        io = case["initial_orders"]; prev = state["decisions"].get("initial_orders",{}).get("ticks",[])
        body = f"<p class='mb-2'>{io['prompt']}</p>" + _checklist("orders_tick", io["orders"], prev)
        body += "<div class='text-xs opacity-70 mt-2'>Include what helps immediately; avoid unsafe blanket thrombolysis or premature discharge.</div>"
        body += _hint_block("initial_orders", state["hints_used"]["initial_orders"], ["Clue: aspirin, GTN (if BP ok), telemetry, serial trops, CXR."])
    elif key == "labs_review":
        lr = case["labs_review"]; prev = state["decisions"].get("labs_review",{}).get("ticks",[])
        body = "<h3 class='font-bold'>Key labs</h3>" + _table_html(lr["table"])
        body += f"<p class='mt-3 mb-2'>{lr['question']}</p>" + _checklist("labs_tick", lr["options"], prev)
        body += _hint_block("labs_review", state["hints_used"]["labs_review"], ["Nudge: non-diagnostic baseline hs-trop still needs serials."])
    elif key == "imaging_panel":
        ip = case["imaging_panel"]; prev = state["decisions"].get("imaging_panel",{}).get("ticks",[])
        body = f"<p class='mb-2 whitespace-pre-line'>{ip['prompt']}</p>" + _checklist("img_tick", ip["checklist"], prev)
    elif key == "investigations":
        inv = case["investigations"]; opts=""
        for o in inv["options"]:
            opts += f"<label class='block bg-slate-800 p-3 rounded-lg mb-2'><input required type='radio' name='choice' value='{o['id']}' class='mr-2 accent-indigo-500'> {o['id']}) {o['text']}</label>"
        used = state["hints_used"]["investigations"]; hint_block = _hint_block("investigations", used, ["Clue: pathway-based serial hs-troponins."])
        prev_conf = state["decisions"].get("investigations",{}).get("conf",50)
        body = f"<p class='mb-2'>{inv['prompt']}</p>{opts}<div class='mt-3 p-3 bg-slate-800 rounded-lg'><label class='block font-semibold mb-1'>Confidence (0–100%)</label><input type='range' min='0' max='100' value='{prev_conf}' name='confidence' class='w-full'></div>{hint_block}"
    elif key == "nbs":
        nbs = case["nbs"]; opts=""
        for o in nbs["options"]:
            opts += f"<label class='block bg-slate-800 p-3 rounded-lg mb-2'><input required type='radio' name='choice' value='{o['id']}' class='mr-2 accent-indigo-500'> {o['id']}) {o['text']}</label>"
        used = state["hints_used"]["nbs"]; hint_block = _hint_block("nbs", used, ["Clue: antiplatelet + telemetry + risk stratification."])
        prev_conf = state["decisions"].get("nbs",{}).get("conf",50)
        body = f"<p class='mb-2'>{nbs['prompt']}</p>{opts}<div class='mt-3 p-3 bg-slate-800 rounded-lg'><label class='block font-semibold mb-1'>Confidence (0–100%)</label><input type='range' min='0' max='100' value='{prev_conf}' name='confidence' class='w-full'></div>{hint_block}"
    elif key == "handoff":
        ho = case["handoff"]; prev = state["decisions"].get("handoff",{}).get("ticks",[])
        body = f"<p class='mb-2'>Handoff content (AUS registrar): select all essentials.</p>" + _checklist("handoff_tick", ho["items"], prev)
    return key, body

@app.route("/stage", methods=["GET","POST"])
def stage():
    state = session.get("case")
    if not state: return redirect(url_for("home"))
    case = CASE_CARDIO
    flow = state["flow"]

    if request.method == "POST":
        action = request.form.get("action","continue")
        key = flow[state["stage_idx"]]

        # Hints
        if action.startswith("hint_"):
            stage_key = action.split("hint_")[-1]
            if stage_key in state["hints_used"]:
                used = state["hints_used"][stage_key]
                costs = XP_POLICY["hint_costs"]
                if used < len(costs):
                    cost = costs[min(used, len(costs)-1)]
                    session["xp"] = max(0, session.get("xp",0) - cost)
                    state["xp_earned"] -= cost
                    state["xp_lines"].append(f"-{cost} XP: Hint used ({stage_key}, level {used+1})")
                    state["hints_used"][stage_key] += 1
            session["case"] = state
            return redirect(url_for("stage"))

        # Process per stage
        if key == "priority":
            choice = request.form.get("choice"); conf = int(request.form.get("confidence",50))
            data = case["priority"]
            correct_id = next((o["id"] for o in data["options"] if o.get("correct")), None)
            correct = (choice == correct_id)
            if correct:
                pts = XP_POLICY["priority_correct"]
                state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} XP: Correct priority")
                state["vitals"] = _apply_vitals_delta(state["vitals"], data["state_if_correct"]["vitals_delta"])
                tag = next((o.get("review_tag") for o in data["options"] if o.get("correct")), None)
                if tag: upsert_spaced_tag(tag, True)
                note = data["state_if_correct"]["note"]
            else:
                state["decisions"]["safety_cap"] = True
                state["wrong_mcq_count"] += 1
                state["xp_lines"].append(f"⚠️ Safety-critical missed: cap {XP_POLICY['safety_wrong_cap']}")
                state["vitals"] = _apply_vitals_delta(state["vitals"], data["state_if_wrong"]["vitals_delta"])
                if choice in data.get("dangerous_choices", []):
                    mal = XP_POLICY["dangerous_choice_malus"]; state["score"] -= mal; state["xp_earned"] -= mal
                    state["xp_lines"].append(f"-{mal} XP: Dangerous choice")
                tag = next((o.get("review_tag") for o in data["options"] if o.get("correct")), None)
                if tag: upsert_spaced_tag(tag, False)
                note = data["state_if_wrong"]["note"]
            cal = calibration_points(correct, conf); state["score"] += cal; state["xp_earned"] += cal
            state["xp_lines"].append(f"+{cal} XP: Calibration (priority)")
            state["decisions"]["priority"] = {"choice": choice, "correct": correct, "conf": conf, "note": note}
            log_event("priority_decision", topic=",".join(case["systems"]), qid=case["id"], correct=int(correct), score=state["score"])

        elif key == "history_rank":
            r1, r2, r3 = request.form.get("rank1"), request.form.get("rank2"), request.form.get("rank3")
            chosen = [r1,r2,r3]; desired = case["history_desired_order"]
            pts = 0
            pts += XP_POLICY["history_rank_points"][1] if r1 == desired[0] else 0
            pts += XP_POLICY["history_rank_points"][2] if r2 == desired[1] else 0
            pts += XP_POLICY["history_rank_points"][3] if r3 == desired[2] else 0
            seen = [c for c in chosen if c]
            if len(set(seen)) != len(seen):
                pts -= XP_POLICY["history_dup_malus"]; state["xp_lines"].append(f"-{XP_POLICY['history_dup_malus']} XP: Duplicate/missing history ranking")
            pts = max(0, min(XP_POLICY["history_max"], pts))
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} XP: History prioritisation")
            state["decisions"]["history_rank"] = {"rank1":r1,"rank2":r2,"rank3":r3}
            upsert_spaced_tag(case["history_review_tag"], r1 == desired[0])

        elif key == "ecg_read":
            ticks = request.form.getlist("ecg_tick")
            reqs = [i["id"] for i in case["ecg_read"]["checklist"] if i.get("required")]
            contras = [i["id"] for i in case["ecg_read"]["checklist"] if i.get("contra")]
            got_reqs = all(r in ticks for r in reqs)
            picked_contra = any(c in ticks for c in contras)
            pts = 0
            if got_reqs: pts += XP_POLICY["ecg_interpretation_full"]
            if picked_contra: pts -= 5
            pts = max(0, pts)
            state["score"] += pts; state["xp_earned"] += pts
            state["xp_lines"].append(f"+{pts} XP: ECG structured read")
            upsert_spaced_tag(case["ecg_read"]["review_tag_correct"], got_reqs and not picked_contra)
            state["decisions"]["ecg_read"] = {"ticks": ticks}

        elif key == "initial_orders":
            ticks = request.form.getlist("orders_tick")
            orders = case["initial_orders"]["orders"]
            required = [o["id"] for o in orders if o.get("required")]
            contra   = [o["id"] for o in orders if o.get("contra")]
            pts = 0
            # reward each required order chosen
            for r in required:
                if r in ticks: pts += XP_POLICY["orders_points_required_each"]
            # penalise unsafe
            for c in contra:
                if c in ticks:
                    pts -= XP_POLICY["orders_malus_contra"]
                    state["xp_lines"].append(f"-{XP_POLICY['orders_malus_contra']} XP: Unsafe order ({c})")
            pts = max(0, pts); state["score"] += pts; state["xp_earned"] += pts
            state["xp_lines"].append(f"+{pts} XP: Initial order-set")
            upsert_spaced_tag(case["initial_orders"]["review_tags"]["required"], all(r in ticks for r in required))
            upsert_spaced_tag(case["initial_orders"]["review_tags"]["contra"], not any(c in ticks for c in contra))
            state["decisions"]["initial_orders"] = {"ticks": ticks}

        elif key == "labs_review":
            ticks = request.form.getlist("labs_tick")
            opts = case["labs_review"]["options"]
            correct_ids = [o["id"] for o in opts if o.get("correct")]
            contra_ids  = [o["id"] for o in opts if o.get("contra")]
            pts = 0
            if all(cid in ticks for cid in correct_ids): pts += case["labs_review"]["points_full"]
            if any(c in ticks for c in contra_ids): pts -= 5
            pts = max(0, pts)
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} XP: Labs reasoning")
            # spaced tags
            for o in opts:
                if o.get("tag"):
                    upsert_spaced_tag(o["tag"], o["id"] in ticks and o.get("correct",False))
            state["decisions"]["labs_review"] = {"ticks": ticks}

        elif key == "imaging_panel":
            ticks = request.form.getlist("img_tick")
            ip = case["imaging_panel"]
            req = [i["id"] for i in ip["checklist"] if i.get("required")]
            contra = [i["id"] for i in ip["checklist"] if i.get("contra")]
            got = all(r in ticks for r in req)
            pts = 6 if got else 0
            if any(c in ticks for c in contra): pts -= 4
            pts = max(0, pts)
            state["score"] += pts; state["xp_earned"] += pts
            state["xp_lines"].append(f"+{pts} XP: CXR panel reasoning")
            upsert_spaced_tag(ip["review_tag_correct"], got and not any(c in ticks for c in contra))
            state["decisions"]["imaging_panel"] = {"ticks": ticks}

        elif key == "investigations":
            inv = case["investigations"]; choice = request.form.get("choice"); conf = int(request.form.get("confidence",50))
            corr = next((o["id"] for o in inv["options"] if o.get("correct")), None)
            correct = (choice == corr)
            if correct:
                pts = XP_POLICY["investigations_correct"]
                state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} XP: Correct investigation")
                state["vitals"] = _apply_vitals_delta(state["vitals"], inv["state_if_correct"]["vitals_delta"])
                upsert_spaced_tag("ACS_TROPONIN_SERIAL", True)
            else:
                state["wrong_mcq_count"] += 1
                state["vitals"] = _apply_vitals_delta(state["vitals"], inv["state_if_wrong"]["vitals_delta"])
                if choice in inv.get("dangerous_choices", []):
                    mal = XP_POLICY["dangerous_choice_malus"]; state["score"] -= mal; state["xp_earned"] -= mal
                    state["xp_lines"].append(f"-{mal} XP: Dangerous investigation")
                upsert_spaced_tag("ACS_TROPONIN_SERIAL", False)
            cal = calibration_points(correct, conf); state["score"] += cal; state["xp_earned"] += cal
            state["xp_lines"].append(f"+{cal} XP: Calibration (investigations)")
            state["decisions"]["investigations"] = {"choice": choice, "correct": correct, "conf": conf}

        elif key == "nbs":
            nbs = case["nbs"]; choice = request.form.get("choice"); conf = int(request.form.get("confidence",50))
            corr = next((o["id"] for o in nbs["options"] if o.get("correct")), None)
            correct = (choice == corr)
            if correct:
                pts = XP_POLICY["nbs_correct"]; state["score"] += pts; state["xp_earned"] += pts
                state["xp_lines"].append(f"+{pts} XP: Correct next step")
                state["vitals"] = _apply_vitals_delta(state["vitals"], nbs["state_if_correct"]["vitals_delta"])
                upsert_spaced_tag("ACS_PATHWAY_ANTIPLATELET", True)
            else:
                state["wrong_mcq_count"] += 1
                state["vitals"] = _apply_vitals_delta(state["vitals"], nbs["state_if_wrong"]["vitals_delta"])
                if choice in nbs.get("dangerous_choices", []):
                    mal = XP_POLICY["dangerous_choice_malus"]; state["score"] -= mal; state["xp_earned"] -= mal
                    state["xp_lines"].append(f"-{mal} XP: Dangerous next step")
                upsert_spaced_tag("ACS_PATHWAY_ANTIPLATELET", False)
            cal = calibration_points(correct, conf); state["score"] += cal; state["xp_earned"] += cal
            state["xp_lines"].append(f"+{cal} XP: Calibration (NBS)")
            state["decisions"]["nbs"] = {"choice": choice, "correct": correct, "conf": conf}

        elif key == "handoff":
            ticks = request.form.getlist("handoff_tick")
            items = case["handoff"]["items"]
            required = [i["id"] for i in items if i.get("required")]
            contra   = [i["id"] for i in items if i.get("contra")]
            good = all(r in ticks for r in required)
            bad  = any(c in ticks for c in contra)
            pts = 6 if good else 0
            if bad: pts -= 4
            pts = max(0, pts)
            state["score"] += pts; state["xp_earned"] += pts
            state["xp_lines"].append(f"+{pts} XP: Handoff structure")
            upsert_spaced_tag(case["handoff"]["review_tags"]["required"], good)
            upsert_spaced_tag(case["handoff"]["review_tags"]["contra"], not bad)
            state["decisions"]["handoff"] = {"ticks": ticks}

        # advance
        state["stage_idx"] += 1
        session["case"] = state

        # If finished flow -> feedback
        if state["stage_idx"] >= len(flow):
            # Speed bonus
            elapsed = time.time() - state["start_ts"]
            if elapsed <= 8*60: sb = XP_POLICY["speed_bonus_fast"]
            elif elapsed <= 12*60: sb = XP_POLICY["speed_bonus_ok"]
            else: sb = 0
            if sb:
                state["score"] += sb; state["xp_earned"] += sb; state["xp_lines"].append(f"+{sb} XP: Speed bonus")

            # Apply caps
            if state["decisions"].get("safety_cap"):
                state["xp_lines"].append(f"Score capped at {XP_POLICY['safety_wrong_cap']} (safety-critical miss)")
                state["score"] = min(state["score"], XP_POLICY["safety_wrong_cap"])
            else:
                wc = state["wrong_mcq_count"]
                if wc >= 2:
                    cap = XP_POLICY["two_wrong_cap"]; state["xp_lines"].append(f"Score capped at {cap} (two wrong MCQs)")
                    state["score"] = min(state["score"], cap)
                elif wc == 1:
                    cap = XP_POLICY["one_wrong_cap"]; state["xp_lines"].append(f"Score capped at {cap} (one wrong MCQ)")
                    state["score"] = min(state["score"], cap)

            session["case"] = state
            return redirect(url_for("feedback"))

    # Render stage
    key, body = _render_stage(state)
    return render_template_string(
        CASE_SHELL,
        title=CASE_CARDIO["title"], level=CASE_CARDIO["level"], systems=", ".join(CASE_CARDIO["systems"]),
        stage_num=state["stage_idx"]+1, stage_total=len(state["flow"]), stage_label=_stage_name(key),
        vitals=state["vitals"], body=body, show_imaging=(key=="imaging_panel"),
        escalation_cues=CASE_CARDIO["escalation_cues"], review_targets=state.get("review_targets", [])
    )

@app.route("/feedback", methods=["GET"])
def feedback():
    state = session.get("case")
    if not state: return redirect(url_for("home"))
    score = max(0, min(100, int(round(state["score"]))))

    # Calibration proxies (priority/investigations/nbs) + ECG heuristic (based on got reqs)
    def cal_of(dec_key):
        d = state["decisions"].get(dec_key, {})
        if not d: return 0
        if dec_key in ("priority", "investigations", "nbs"):
            return calibration_points(d.get("correct",False), d.get("conf",50))
        if dec_key == "ecg_read":
            ticks = d.get("ticks",[])
            req = [i["id"] for i in CASE_CARDIO["ecg_read"]["checklist"] if i.get("required")]
            contras = [i["id"] for i in CASE_CARDIO["ecg_read"]["checklist"] if i.get("contra")]
            got = all(r in ticks for r in req) and not any(c in ticks for c in contras)
            return 8 if got else 3
        return 0

    calib = {
        "priority": cal_of("priority"),
        "ecg": cal_of("ecg_read"),
        "investigations": cal_of("investigations"),
        "nbs": cal_of("nbs")
    }
    calib_avg = round(sum(calib.values())/4.0, 1)

    # Badges
    total_hints = sum(state["hints_used"].values())
    badges = []
    if total_hints == 0: badges.append("🏅 No Hints")
    if (time.time() - state["start_ts"]) <= 8*60: badges.append("⚡ Fast Finish (<8 min)")
    if calib_avg >= 8: badges.append("🎯 Well-Calibrated")
    if state["decisions"].get("priority",{}).get("correct"): badges.append("✅ Perfect Priority")

    session["last_run"] = {
        "score": score, "calib": calib, "calib_avg": calib_avg,
        "badges": badges, "xp_lines": state["xp_lines"][:], "xp_case": state["xp_earned"]
    }

    # Suggest review tags from wrong decisions
    review_suggestions = []
    if state["decisions"].get("priority",{}).get("correct") is False:
        review_suggestions.append("ACS_ECG_10MIN")
    if state["decisions"].get("investigations",{}).get("correct") is False:
        review_suggestions.append("ACS_TROPONIN_SERIAL")
    if state["decisions"].get("nbs",{}).get("correct") is False:
        review_suggestions.append("ACS_PATHWAY_ANTIPLATELET")

    fb = CASE_CARDIO["feedback"]
    log_event("case_feedback", topic=",".join(CASE_CARDIO["systems"]), qid=CASE_CARDIO["id"], score=score, total=100, percent=score,
              from_review=1 if state.get("review_targets") else 0)

    return render_template_string(
        FEEDBACK_HTML,
        score=score, streak=session.get("streak",0), xp=session.get("xp",0),
        rationale=fb["rationale_html"], takeaways=fb["takeaways"], anz_ref=fb["anz_ref"],
        calib=type("Obj",(object,),calib)(), calib_avg=calib_avg, badges=badges,
        xp_breakdown=session["last_run"]["xp_lines"], curriculum_outcomes=CASE_CARDIO["curriculum_outcomes"],
        escalation_cues=CASE_CARDIO["escalation_cues"], review_suggestions=review_suggestions
    )

@app.route("/finish", methods=["POST"])
def finish_feedback():
    last = session.get("last_run", {"score":0, "xp_case":0})
    session["xp"] = max(0, session.get("xp",0) + int(last.get("xp_case",0)))
    maybe_increment_streak_once_today()
    log_event("case_done", topic=",".join(CASE_CARDIO["systems"]), qid=CASE_CARDIO["id"], score=last.get("score",0), total=100, percent=last.get("score",0))
    session.pop("case", None)
    return redirect(url_for("home"))

# ======================= Gate (optional) =======================
@app.route("/gate", methods=["GET","POST"])
def gate():
    access_code = os.getenv("ACCESS_CODE")
    if not access_code: return redirect(url_for("home"))
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

# ======================= Static serving helper (optional) ======
@app.route('/static/<path:filename>')
def static_file(filename):
    return send_from_directory(app.static_folder, filename)

# ======================= Export analytics ======================
@app.route("/export.csv")
def export_csv():
    conn = _db()
    cur = conn.execute("SELECT ts,session_id,event,topic,qid,correct,from_review,from_anchor,variant,score,total,percent FROM events ORDER BY id DESC")
    rows = cur.fetchall(); conn.close()
    csv = "ts,session_id,event,topic,qid,correct,from_review,from_anchor,variant,score,total,percent\n"
    for r in rows:
        csv += ",".join("" if v is None else str(v) for v in r) + "\n"
    from flask import make_response as _mr
    resp = _mr(csv); resp.headers["Content-Type"]="text/csv"
    resp.headers["Content-Disposition"]="attachment; filename=events.csv"
    return resp

# ======================= Local run =======================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")), debug=True)
