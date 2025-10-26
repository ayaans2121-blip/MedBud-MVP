# app.py — MedBud: Clinical Judgment Trainer (AUS) — v6.1
# Patch: ensure DB dir exists; add /healthz; add robust error handler/logging

from flask import Flask, render_template_string, request, redirect, url_for, session, make_response, send_from_directory
import os, time, uuid, sqlite3, traceback, sys
from datetime import datetime, date

app = Flask(__name__, static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")

# ======================= Analytics (SQLite) =======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = BASE_DIR  # write to project dir (Render's /opt/render/project/src is writable at runtime)
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "analytics.db")

def _db():
    # New connection each time; safer under Gunicorn workers/threads
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
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
             int(correct) if correct is not None else None, int(from_review or 0), None, "MedBud_v6.1",
             score, total, percent)
        )
        conn.commit(); conn.close()
    except Exception as e:
        print("analytics error:", e, file=sys.stderr)

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
        print("spaced error:", e, file=sys.stderr)

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
    except Exception as e:
        print("due_spaced_tags error:", e, file=sys.stderr)
        return []

def queued_spaced_count():
    try:
        conn = _db()
        cur = conn.execute("SELECT COUNT(*) FROM spaced WHERE session_id=? AND next_due_ts<=?", (session.get("sid"), _now_ts()))
        n = cur.fetchone()[0]; conn.close()
        return n
    except Exception as e:
        print("queued_spaced_count error:", e, file=sys.stderr)
        return 0

# ======================= Session bootstrap + optional gate =======================
@app.before_request
def ensure_session_and_gate():
    access_code = os.getenv("ACCESS_CODE")
    # Allow gate/static even if gated
    if access_code and request.endpoint not in ("gate","static_file","static","healthz"):
        if not request.cookies.get("access_ok"):
            return redirect(url_for("gate"))
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    if "xp" not in session:
        session.update(dict(xp=0, streak=0, last_streak_day=None, cases_completed_today=0))

# Health check (Render pings this if you set it as a health check path)
@app.route("/healthz")
def healthz():
    try:
        conn = _db(); conn.execute("SELECT 1"); conn.close()
        return "ok", 200
    except Exception as e:
        print("healthz error:", e, file=sys.stderr)
        return "db error", 500

def today_str(): return date.today().isoformat()

def maybe_increment_streak_once_today():
    t = today_str()
    if session.get("last_streak_day") != t:
        session["streak"] = session.get("streak", 0) + 1
        session["last_streak_day"] = t
        session["cases_completed_today"] = 1
    else:
        session["cases_completed_today"] = session.get("cases_completed_today", 0) + 1

# ======================= Scoring / XP policy (stricter) =======================
XP = {
    "miss_required_stage_cap": 60,
    "contra_malus_heavy": 20,
    "contra_malus_moderate": 12,
    "immediate_actions_full": 20,
    "history_select_full": 12,
    "focused_exam_full": 12,
    "ecg_read_full": 12,
    "order_set_full": 20,
    "labs_reasoning_full": 12,
    "imaging_panel_full": 8,
    "plan_builder_full": 20,
    "handoff_full": 8,
    "speed_bonus_fast": 5,
    "speed_bonus_ok": 3,
}

# ======================= Cardio Case (judgment-first) =======================
CASE = {
    "block": "Cardiology",
    "id": 6001,
    "systems": ["ED", "Cardio"],
    "title": "Acute Chest Pain at Triage (AUS)",
    "level": "MD3–4 / Intern-ready",
    "flow": [
        "presenting", "immediate_actions", "targeted_history",
        "focused_exam", "ecg_read", "order_set",
        "labs_reasoning", "imaging_panel", "plan_builder", "handoff"
    ],
    "presenting": "A 54-year-old presents with 40 minutes of central, pressure-like chest pain radiating to the left arm with diaphoresis and nausea. Pain 8/10.",
    "vitals_initial": {"HR": 102, "BP": "146/88", "RR": 20, "SpO2": "96% RA", "Temp": "36.9°C"},
    "curriculum_outcomes": [
        "Interpret a systematic ECG and identify ischaemia.",
        "Discuss differential diagnoses of acute chest pain and initial management priorities.",
        "Apply pathway-based ACS assessment including serial hs-troponins.",
        "Describe pharmacology/safety of aspirin and nitrates in ACS.",
        "Perform focused cardiovascular exam; recognise red flags.",
        "Interpret a portable CXR with a systematic approach."
    ],
    "escalation_cues": [
        "New ST deviation or dynamic ECG changes",
        "Hemodynamic compromise (hypotension, arrhythmia, syncope)",
        "Ongoing pain despite initial measures",
        "High-risk features (e.g., GRACE high-risk) or rising troponins"
    ],
    "immediate_actions": {
        "prompt": "Immediate actions (select all to do now):",
        "items": [
            {"id":"ecg_10", "text":"12-lead ECG within 10 minutes", "required": True, "tag":"ACS_ECG_10MIN"},
            {"id":"monitor", "text":"Cardiac monitoring + IV access", "required": True, "tag":"ACS_MONITOR_IV"},
            {"id":"o2_if", "text":"Oxygen only if SpO₂ < 94%", "required": False, "tag":"OXYGEN_JUDICIOUS"},
            {"id":"delay_trop", "text":"Wait for hs-troponin before ECG", "contra": "heavy"},
            {"id":"discharge", "text":"Discharge now with GP review", "contra": "heavy"}
        ]
    },
    "targeted_history": {
        "prompt": "Pick up to 3 high-yield history prompts to ask first (limit = 3):",
        "limit": 3,
        "items": [
            {"id":"redflags", "text":"Red flags: diaphoresis/SOB/syncope", "required": True, "tag":"HIST_RED_FLAGS"},
            {"id":"char", "text":"Characterise pain: radiation/exertion/relief", "required": True, "tag":"HIST_CHARACTER"},
            {"id":"risk", "text":"Risk: CAD risks/family hx", "required": False, "tag":"HIST_RISK"},
            {"id":"medlist", "text":"Medications/allergies", "required": False},
            {"id":"pleuritic", "text":"Is it pleuritic/positional only?", "required": False}
        ]
    },
    "focused_exam": {
        "prompt": "Choose exam manoeuvres to perform immediately:",
        "items": [
            {"id":"obs", "text":"Repeat vitals (trend HR, BP, RR, SpO₂)", "required": True, "tag":"EXAM_OBS"},
            {"id":"lung", "text":"Auscultate lungs (crackles, wheeze)", "required": True, "tag":"EXAM_LUNGS"},
            {"id":"cv", "text":"Cardiac exam (murmurs, S3, perfusion)", "required": True, "tag":"EXAM_CV"},
            {"id":"abd", "text":"Abdominal exam", "required": False},
            {"id":"neuro", "text":"Neurologic screen", "required": False}
        ]
    },
    "ecg_read": {
        "prompt": "ECG interpretation (tick all that apply):",
        "checklist": [
            {"id":"rate", "text":"Sinus tachycardia (~100–110 bpm)", "required": True},
            {"id":"stdep", "text":"ST depression in V4–V6", "required": True},
            {"id":"stemi", "text":"ST elevation in contiguous leads", "contra": True},
            {"id":"lbbb", "text":"New LBBB likely", "contra": True},
            {"id":"normal", "text":"Normal ECG", "contra": True}
        ],
        "review_tag_correct":"ECG_ISCHAEMIA_RECOGNITION"
    },
    "order_set": {
        "prompt": "Initial order-set (select what to start now):",
        "items": [
            {"id":"aspirin", "text":"Aspirin loading (if no contraindication)", "required": True, "tag":"ASPIRIN"},
            {"id":"gtn", "text":"Sublingual GTN if pain and BP adequate", "required": True, "tag":"GTN"},
            {"id":"tele", "text":"Telemetry/obs charting", "required": True, "tag":"TELEMETRY"},
            {"id":"cxr", "text":"Portable CXR (next 30–60 min)", "required": True, "tag":"CXR"},
            {"id":"thrombolyse_all", "text":"Empiric thrombolysis for everyone", "contra":"heavy"},
            {"id":"discharge_now", "text":"Discharge now", "contra":"heavy"}
        ]
    },
    "labs_reasoning": {
        "table": [
            ("Hb", "141 g/L"), ("Plt", "220 x10^9/L"), ("Na", "139 mmol/L"), ("K", "3.6 mmol/L"),
            ("Cr", "82 µmol/L"), ("eGFR", ">90 mL/min"), ("Glucose", "5.8 mmol/L"),
            ("hs-TnT (0h)", "12 ng/L (ULN ~14)"), ("D-dimer", "Not indicated")
        ],
        "question": "Select the most appropriate statements:",
        "options": [
            {"id":"trend", "text":"Serial hs-troponins are required despite non-diagnostic baseline", "correct": True, "tag":"TROPONIN_SERIAL"},
            {"id":"renal_ok", "text":"Renal function acceptable for contrast if needed", "correct": True, "tag":"RENAL_OK"},
            {"id":"ddimer_first", "text":"D-dimer should be sent first-line in typical ACS pain", "contra": True},
            {"id":"glucose_dka", "text":"Glucose 5.8 suggests DKA as the cause", "contra": True}
        ]
    },
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
    "plan_builder": {
        "prompt": "Plan (select all that apply now):",
        "items": [
            {"id":"acs_pathway", "text":"Pathway-based ACS risk stratification + serial hs-troponins", "required": True, "tag":"ACS_PATHWAY"},
            {"id":"analgesia", "text":"Analgesia judiciously (avoid masking red flags)", "required": False},
            {"id":"cardiology_call", "text":"Discuss with cardiology registrar if dynamic ECG or ongoing pain", "required": True, "tag":"ESCALATE_CARDIO"},
            {"id":"dsch", "text":"Discharge home now", "contra": "heavy"}
        ]
    },
    "handoff": {
        "prompt": "Handoff essentials to the registrar (select all essentials):",
        "items": [
            {"id":"time", "text":"Symptom onset time & ECG timing", "required": True, "tag":"HANDOFF_TIMES"},
            {"id":"hemo", "text":"Hemodynamics & current red flags", "required": True, "tag":"HANDOFF_HEMO"},
            {"id":"tx", "text":"Therapies given (aspirin/GTN) & response", "required": True, "tag":"HANDOFF_TX"},
            {"id":"risk", "text":"Risk stratification plan & serial troponins schedule", "required": True, "tag":"HANDOFF_PLAN"},
            {"id":"discharge", "text":"Plan immediate discharge now", "contra": "moderate"}
        ]
    },
    "feedback": {
        "rationale_html": """
        <p><b>Why your judgment sequence matters:</b> In AUS ED chest pain, time-to-ECG and pathway entry are critical.
        ECG within 10 minutes guides immediate risk. Systematic ECG reading catches ischaemia beyond classic STEMI.
        Start an appropriate order-set (aspirin, GTN if BP adequate, telemetry, early CXR), and commit to serial hs-troponins.
        Oxygen is for hypoxia, not routine. Clear, structured handoff prevents delays and unsafe discharge.</p>
        """,
        "anz_ref": "Aligned with AUS ED/ACS pathways (ECG ≤10 min; pathway-based assessment)."
    }
}

# ======================= Templates (unchanged from v6) =======================
# ... (KEEP YOUR v6 HOME_HTML, CASE_SHELL, FEEDBACK_HTML here UNCHANGED)
# For brevity, omitted in this snippet. Use the same templates you already pasted in v6.

# === Paste your v6 HOME_HTML, CASE_SHELL, FEEDBACK_HTML BELOW ==
BASE_HEAD = """<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
<style>.img-wrap{position:relative;width:100%;max-width:520px;margin:0 auto}.img-wrap img{width:100%;display:block;border-radius:12px}.overlay{position:absolute;inset:0;pointer-events:none;opacity:0;transition:opacity .2s}.overlay.show{opacity:.85}.overlay.silhouette{background:repeating-linear-gradient(90deg,rgba(0,255,255,.12),rgba(0,255,255,.12) 2px,transparent 2px,transparent 12px)}.overlay.angles{background:repeating-linear-gradient(0deg,rgba(255,255,0,.15),rgba(255,255,0,.15) 2px,transparent 2px,transparent 20px)}.overlay.contour{box-shadow:inset 0 0 0 4px rgba(255,0,128,.5)}</style>"""
HOME_HTML = """<!doctype html><html><head>"""+BASE_HEAD+"""<title>MedBud</title></head>
<body class="min-h-screen bg-gradient-to-br from-sky-500 via-indigo-500 to-emerald-500 text-white p-4"><div class="w-full max-w-4xl mx-auto bg-white/15 backdrop-blur-md rounded-2xl p-6 shadow-xl">
<h1 class="text-3xl font-extrabold">MedBud</h1><p class="opacity-90 mt-1">Daily judgment reps for clinical years. AUS context. Built for <b>action-first</b> decisions.</p>
<div class="flex flex-wrap gap-2 my-4"><span class="px-3 py-1 rounded-full bg-white/20">🔥 Streak: {{ streak }}</span><span class="px-3 py-1 rounded-full bg-white/20">⭐ XP: {{ xp }}</span><span class="px-3 py-1 rounded-full bg-white/20">📅 Today: {{ cases_today }} case(s)</span><span class="px-3 py-1 rounded-full {% if due_count>0 %}bg-rose-400/80{% else %}bg-white/20{% endif %}">🧩 Weak spots due: {{ due_count }}</span></div>
<form method="post" action="{{ url_for('start_case') }}" class="grid md:grid-cols-3 gap-3"><div class="bg-white/10 rounded-xl p-4 col-span-2"><h3 class="font-bold mb-2">Pick a block</h3>
<label class="flex items-center gap-2 mb-2"><input type="radio" name="block" value="Cardiology" checked class="accent-emerald-500" /><span>Cardiology</span></label>
<label class="flex items-center gap-2 mb-2 opacity-50"><input type="radio" disabled class="accent-emerald-500" /><span>Neurology (coming soon)</span></label>
<label class="flex items-center gap-2 mb-2 opacity-50"><input type="radio" disabled class="accent-emerald-500" /><span>Geriatrics (coming soon)</span></label>
<input type="hidden" name="review_prefill" value="{{ (due_tags|join('|')) if due_tags else '' }}"><button class="mt-2 px-5 py-3 rounded-xl font-bold bg-emerald-500 hover:bg-emerald-600">Start Case</button>
<p class="mt-2 text-sm opacity-80">We’ll prioritise any due weak-spot tags: {{ due_tags|join(', ') if due_tags else 'none due' }}.</p></div>
<div class="bg-white/10 rounded-xl p-4"><h3 class="font-bold">What this trains</h3><ul class="list-disc ml-5 opacity-90 text-sm"><li>Immediate actions & order-sets (not trivia)</li><li>Structured ECG/CXR reads</li><li>Plan + escalation thresholds</li><li>Registrar handoff discipline</li></ul></div></form>
<div class="mt-6 bg-white/10 rounded-xl p-4"><h3 class="font-bold">Cardio Learning Outcomes covered</h3><ul class="list-disc ml-5 opacity-90">{% for o in curriculum_outcomes %}<li>{{ o }}</li>{% endfor %}</ul></div></div></body></html>"""
CASE_SHELL = """<!doctype html><html><head>"""+BASE_HEAD+"""<title>{{ title }} — MedBud</title></head>
<body class="min-h-screen bg-slate-900 text-slate-100 p-4"><div class="max-w-5xl mx-auto"><div class="flex items-center justify-between mb-3"><h1 class="text-2xl font-extrabold">{{ title }}</h1><div class="text-sm opacity-80">{{ level }} • {{ systems }}</div></div>
<div class="grid md:grid-cols-3 gap-3 mb-3"><div class="md:col-span-2 bg-slate-800/70 rounded-xl p-4"><div class="text-sm">Stage {{ stage_num }} / {{ stage_total }}</div><div class="mt-1 text-slate-200 font-semibold">{{ stage_label }}</div></div>
<div class="bg-slate-800/70 rounded-xl p-4"><div class="font-bold mb-1">Vitals</div><div class="grid grid-cols-2 gap-x-2 text-sm"><div>HR</div><div>{{ vitals.HR }}</div><div>BP</div><div>{{ vitals.BP }}</div><div>RR</div><div>{{ vitals.RR }}</div><div>SpO₂</div><div>{{ vitals.SpO2 }}</div><div>Temp</div><div>{{ vitals.Temp }}</div></div></div></div>
<form method="post" class="space-y-4">{{ body|safe }}{% if show_imaging %}
<div class="bg-slate-800/70 rounded-xl p-4"><h3 class="font-bold mb-2">Portable CXR (toggle overlays)</h3><div class="img-wrap"><img src="{{ url_for('static', filename='cxr_sample.jpg') }}" onerror="this.outerHTML='<div class=\\'p-4 bg-slate-700 rounded-lg text-sm\\'>Place a file at <b>static/cxr_sample.jpg</b> to enable the imaging panel overlays.</div>'"><div id="ov-sil" class="overlay silhouette"></div><div id="ov-ang" class="overlay angles"></div><div id="ov-con" class="overlay contour"></div></div>
<div class="flex gap-2 mt-3"><button name="toggle_overlay" value="sil" class="px-3 py-1 rounded bg-indigo-600">Silhouette</button><button name="toggle_overlay" value="ang" class="px-3 py-1 rounded bg-indigo-600">Costophrenic</button><button name="toggle_overlay" value="con" class="px-3 py-1 rounded bg-indigo-600">Contour</button></div>
<script>document.addEventListener('click',(e)=>{if(e.target.name==='toggle_overlay'){e.preventDefault();const id='ov-'+e.target.value;const el=document.getElementById(id);if(el){el.classList.toggle('show');}}});</script></div>{% endif %}
<div class="flex gap-2"><a href="{{ url_for('home') }}" class="px-4 py-2 rounded-lg bg-slate-700">Quit</a><button name="action" value="continue" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 font-bold">Continue</button></div></form></div></body></html>"""
FEEDBACK_HTML = """<!doctype html><html><head>"""+BASE_HEAD+"""<title>Feedback — MedBud</title></head>
<body class="min-h-screen bg-gradient-to-br from-emerald-50 to-sky-50 text-slate-900 p-4"><div class="max-w-4xl mx-auto bg-white rounded-2xl shadow p-5">
<h2 class="text-2xl font-extrabold">Attending Feedback</h2><div class="flex flex-wrap gap-2 my-3"><span class="px-3 py-1 rounded-full bg-emerald-100 text-emerald-900">Score: {{ score }} / 100</span><span class="px-3 py-1 rounded-full bg-indigo-100 text-indigo-900">🔥 Streak: {{ streak }}</span><span class="px-3 py-1 rounded-full bg-amber-100 text-amber-900">⭐ XP: {{ xp }}</span></div>
<div class="grid gap-4"><div class="bg-slate-50 border rounded-xl p-4"><h3 class="font-bold mb-1">Global Rationale (AUS)</h3><div class="prose max-w-none">{{ rationale|safe }}</div><p class="text-sm text-slate-600 mt-2 italic">{{ anz_ref }}</p></div>
{% for section in sections %}<div class="bg-slate-50 border rounded-xl p-4"><h3 class="font-bold">{{ section.title }}</h3><p class="mt-1"><b>What you did:</b> {{ section.did }}</p>{% if section.missed %}<p class="mt-1 text-rose-700"><b>Critical misses:</b> {{ section.missed }}</p>{% endif %}{% if section.unsafe %}<p class="mt-1 text-rose-700"><b>Unsafe choices:</b> {{ section.unsafe }}</p>{% endif %}{% if section.attending %}<p class="mt-1"><b>Attending comment:</b> {{ section.attending }}</p>{% endif %}</div>{% endfor %}
<div class="bg-slate-50 border rounded-xl p-4"><h3 class="font-bold">Learning Outcomes covered (Cardio)</h3><ul class="list-disc ml-6">{% for o in curriculum_outcomes %}<li>{{ o }}</li>{% endfor %}</ul></div>
<div class="bg-slate-50 border rounded-xl p-4"><h3 class="font-bold">AUS escalation cues — when to call the registrar</h3><ul class="list-disc ml-6">{% for e in escalation_cues %}<li>{{ e }}</li>{% endfor %}</ul></div>
<div class="bg-slate-50 border rounded-xl p-4"><h3 class="font-bold">XP Breakdown</h3><ul class="list-disc ml-6 text-sm">{% for line in xp_breakdown %}<li>{{ line }}</li>{% endfor %}</ul></div>
{% if review_suggestions %}<div class="bg-amber-50 border rounded-xl p-4"><h3 class="font-bold mb-1">Next resurfacing</h3><p class="text-sm">Queued weak spots: <b>{{ review_suggestions|join(', ') }}</b></p></div>{% endif %}
<form method="post" action="{{ url_for('finish_feedback') }}"><button class="px-5 py-3 rounded-xl font-bold bg-indigo-600 text-white hover:bg-indigo-700">Finish</button><a href="{{ url_for('home') }}" class="ml-2 px-4 py-3 rounded-xl bg-slate-200">Back Home</a></form></div></div></body></html>"""

# ======================= Helpers & Routes (same logic as v6) =======================
def _stage_name(key):
    return {"presenting":"Presenting Problem","immediate_actions":"Immediate Actions","targeted_history":"Targeted History (pick ≤3)","focused_exam":"Focused Exam","ecg_read":"ECG Checklist","order_set":"Initial Order-Set","labs_reasoning":"Lab Review & Reasoning","imaging_panel":"Imaging Panel (CXR overlays)","plan_builder":"Plan Builder","handoff":"Registrar Handoff"}.get(key, key)
def _checklist(name, items, prev):
    html=""; 
    for it in items:
        checked = "checked" if prev and it["id"] in prev else ""
        html+=f"""<label class="block bg-slate-800 p-3 rounded-lg mb-2"><input type="checkbox" name="{name}" value="{it['id']}" class="mr-2 accent-indigo-500" {checked}> {it['text']}</label>"""
    return html
def _table_html(rows):
    h="<table class='w-full text-sm'><tbody>"
    for k,v in rows: h+=f"<tr class='border-b border-slate-700/40'><td class='py-1 pr-3 text-slate-300'>{k}</td><td class='py-1'>{v}</td></tr>"
    h+="</tbody></table>"; return h

@app.route("/", methods=["GET"])
def home():
    return render_template_string(HOME_HTML,
        streak=session.get("streak",0), xp=session.get("xp",0), cases_today=session.get("cases_completed_today",0),
        due_count=queued_spaced_count(), due_tags=due_spaced_tags(limit=4), curriculum_outcomes=CASE["curriculum_outcomes"])

@app.route("/start", methods=["POST"])
def start_case():
    review_prefill = request.form.get("review_prefill","").strip()
    review_targets = [t for t in review_prefill.split("|") if t] if review_prefill else due_spaced_tags(limit=4)
    session["case"] = {"id": CASE["id"], "flow": CASE["flow"][:], "stage_idx": 0, "score": 0, "xp_earned": 0, "start_ts": time.time(),
        "decisions": {}, "vitals": dict(CASE["vitals_initial"]), "xp_lines": [], "safety_caps": set(), "contra_flags": [], "review_targets": review_targets or []}
    log_event("start_case", topic=",".join(CASE["systems"]), qid=CASE["id"], from_review=1 if review_targets else 0)
    return redirect(url_for("stage"))

def _render_stage(state):
    key = state["flow"][state["stage_idx"]]; body=""
    if key == "presenting":
        body = f"<div class='bg-slate-800/60 rounded-xl p-4'><p class='text-lg'>{CASE['presenting']}</p></div>"
    elif key == "immediate_actions":
        items = CASE["immediate_actions"]["items"]; prev = state["decisions"].get("immediate_actions",{}).get("ticks",[])
        body = f"<p class='mb-2'>{CASE['immediate_actions']['prompt']}</p>" + _checklist("imm_tick", items, prev)
    elif key == "targeted_history":
        items = CASE["targeted_history"]["items"]; prev = state["decisions"].get("targeted_history",{}).get("ticks",[]); limit = CASE["targeted_history"]["limit"]
        body = f"<p class='mb-2'>{CASE['targeted_history']['prompt']}</p>" + _checklist("hist_tick", items, prev) + f"<div class='text-xs opacity-70 mt-2'>Select up to {limit}.</div>"
    elif key == "focused_exam":
        items = CASE["focused_exam"]["items"]; prev = state["decisions"].get("focused_exam",{}).get("ticks",[]); body = f"<p class='mb-2'>{CASE['focused_exam']['prompt']}</p>" + _checklist("exam_tick", items, prev)
    elif key == "ecg_read":
        ecg = CASE["ecg_read"]; prev = state["decisions"].get("ecg_read",{}).get("ticks",[]); body = f"<p class='mb-2'>{ecg['prompt']}</p>" + _checklist("ecg_tick", ecg["checklist"], prev) + "<div class='text-xs opacity-70 mt-2'>We score by required picks chosen and contraindicated picks avoided.</div>"
    elif key == "order_set":
        items = CASE["order_set"]["items"]; prev = state["decisions"].get("order_set",{}).get("ticks",[]); body = f"<p class='mb-2'>{CASE['order_set']['prompt']}</p>" + _checklist("order_tick", items, prev)
    elif key == "labs_reasoning":
        lr = CASE["labs_reasoning"]; prev = state["decisions"].get("labs_reasoning",{}).get("ticks",[]); body = "<h3 class='font-bold'>Key labs</h3>" + _table_html(lr["table"]) + f"<p class='mt-3 mb-2'>{lr['question']}</p>" + _checklist("labs_tick", lr["options"], prev)
    elif key == "imaging_panel":
        ip = CASE["imaging_panel"]; prev = state["decisions"].get("imaging_panel",{}).get("ticks",[]); body = f"<p class='mb-2 whitespace-pre-line'>{ip['prompt']}</p>" + _checklist("img_tick", ip["checklist"], prev)
    elif key == "plan_builder":
        items = CASE["plan_builder"]["items"]; prev = state["decisions"].get("plan_builder",{}).get("ticks",[]); body = f"<p class='mb-2'>{CASE['plan_builder']['prompt']}</p>" + _checklist("plan_tick", items, prev)
    elif key == "handoff":
        items = CASE["handoff"]["items"]; prev = state["decisions"].get("handoff",{}).get("ticks",[]); body = f"<p class='mb-2'>{CASE['handoff']['prompt']}</p>" + _checklist("handoff_tick", items, prev)
    return key, body

def _score_required_contra(ticks, items, full_points, section_title, state, review_tag_ok=None):
    ids_req = [i["id"] for i in items if i.get("required")]
    ids_contra_heavy = [i["id"] for i in items if i.get("contra") == "heavy"]
    ids_contra_moderate = [i["id"] for i in items if i.get("contra") == "moderate"]

    missing = [i for i in ids_req if i not in ticks]
    unsafe_heavy = [i for i in ids_contra_heavy if i in ticks]
    unsafe_moderate = [i for i in ids_contra_moderate if i in ticks]

    points = 0
    if not missing:
        points = full_points
    else:
        have = len(ids_req) - len(missing)
        if have >= max(1, len(ids_req)//2) and not unsafe_heavy and not unsafe_moderate:
            points = max(0, full_points // 3)

    if unsafe_heavy:
        points = max(0, points - XP["contra_malus_heavy"]); state["contra_flags"].append((section_title, unsafe_heavy))
    if unsafe_moderate:
        points = max(0, points - XP["contra_malus_moderate"]); state["contra_flags"].append((section_title, unsafe_moderate))

    if missing:
        state["safety_caps"].add(section_title)

    if review_tag_ok:
        ok = (not missing) and (not unsafe_heavy) and (not unsafe_moderate)
        upsert_spaced_tag(review_tag_ok, ok)

    return points, missing, unsafe_heavy + unsafe_moderate

@app.route("/stage", methods=["GET","POST"])
def stage():
    state = session.get("case")
    if not state: return redirect(url_for("home"))
    flow = state["flow"]

    if request.method == "POST":
        key = flow[state["stage_idx"]]

        if key == "immediate_actions":
            ticks = request.form.getlist("imm_tick")
            pts, missing, unsafe = _score_required_contra(ticks, CASE["immediate_actions"]["items"], XP["immediate_actions_full"], "Immediate Actions", state, review_tag_ok="ACS_ECG_10MIN")
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} Immediate actions")
            upsert_spaced_tag("ACS_MONITOR_IV", not missing); state["decisions"]["immediate_actions"] = {"ticks": ticks}

        elif key == "targeted_history":
            ticks = request.form.getlist("hist_tick")
            limit = CASE["targeted_history"]["limit"]
            if len(ticks) > limit: ticks = ticks[:limit]
            pts, missing, unsafe = _score_required_contra(ticks, CASE["targeted_history"]["items"], XP["history_select_full"], "Targeted History", state, review_tag_ok="HIST_RED_FLAGS")
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} Targeted history")
            state["decisions"]["targeted_history"] = {"ticks": ticks}

        elif key == "focused_exam":
            ticks = request.form.getlist("exam_tick")
            pts, missing, unsafe = _score_required_contra(ticks, CASE["focused_exam"]["items"], XP["focused_exam_full"], "Focused Exam", state, review_tag_ok="EXAM_OBS")
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} Focused exam")
            state["decisions"]["focused_exam"] = {"ticks": ticks}

        elif key == "ecg_read":
            ticks = request.form.getlist("ecg_tick")
            req = [i["id"] for i in CASE["ecg_read"]["checklist"] if i.get("required")]
            contra = [i["id"] for i in CASE["ecg_read"]["checklist"] if i.get("contra")]
            missing = [r for r in req if r not in ticks]
            picked_contra = [c for c in contra if c in ticks]
            pts = XP["ecg_read_full"] if not missing and not picked_contra else (XP["ecg_read_full"]//3 if not missing else 0)
            if picked_contra: pts = max(0, pts - XP["contra_malus_moderate"]); state["contra_flags"].append(("ECG Checklist", picked_contra))
            if missing: state["safety_caps"].add("ECG Checklist")
            upsert_spaced_tag(CASE["ecg_read"]["review_tag_correct"], not missing and not picked_contra)
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} ECG read")
            state["decisions"]["ecg_read"] = {"ticks": ticks}

        elif key == "order_set":
            ticks = request.form.getlist("order_tick")
            pts, missing, unsafe = _score_required_contra(ticks, CASE["order_set"]["items"], XP["order_set_full"], "Order-Set", state, review_tag_ok="ACS_PATHWAY_BUNDLE")
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} Order-set")
            state["decisions"]["order_set"] = {"ticks": ticks}

        elif key == "labs_reasoning":
            ticks = request.form.getlist("labs_tick")
            opts = CASE["labs_reasoning"]["options"]
            correct_ids = [o["id"] for o in opts if o.get("correct")]
            contra_ids  = [o["id"] for o in opts if o.get("contra")]
            missing = [cid for cid in correct_ids if cid not in ticks]
            unsafe = [c for c in contra_ids if c in ticks]
            pts = XP["labs_reasoning_full"] if not missing and not unsafe else (XP["labs_reasoning_full"]//3 if not missing else 0)
            if unsafe: pts = max(0, pts - XP["contra_malus_moderate"]); state["contra_flags"].append(("Labs Reasoning", unsafe))
            if missing: state["safety_caps"].add("Labs Reasoning")
            for o in opts:
                if o.get("tag"): upsert_spaced_tag(o["tag"], o["id"] in ticks and o.get("correct",False) and not unsafe)
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} Labs reasoning")
            state["decisions"]["labs_reasoning"] = {"ticks": ticks}

        elif key == "imaging_panel":
            ticks = request.form.getlist("img_tick")
            ip = CASE["imaging_panel"]; req = [i["id"] for i in ip["checklist"] if i.get("required")]; contra = [i["id"] for i in ip["checklist"] if i.get("contra")]
            missing = [r for r in req if r not in ticks]; unsafe = [c for c in contra if c in ticks]
            pts = XP["imaging_panel_full"] if not missing and not unsafe else (XP["imaging_panel_full"]//3 if not missing else 0)
            if unsafe: pts = max(0, pts - XP["contra_malus_moderate"]); state["contra_flags"].append(("Imaging Panel", unsafe))
            if missing: state["safety_caps"].add("Imaging Panel")
            upsert_spaced_tag(ip["review_tag_correct"], not missing and not unsafe)
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} CXR panel")
            state["decisions"]["imaging_panel"] = {"ticks": ticks}

        elif key == "plan_builder":
            ticks = request.form.getlist("plan_tick")
            pts, missing, unsafe = _score_required_contra(ticks, CASE["plan_builder"]["items"], XP["plan_builder_full"], "Plan Builder", state, review_tag_ok="ACS_PATHWAY")
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} Plan builder")
            state["decisions"]["plan_builder"] = {"ticks": ticks}

        elif key == "handoff":
            ticks = request.form.getlist("handoff_tick")
            pts, missing, unsafe = _score_required_contra(ticks, CASE["handoff"]["items"], XP["handoff_full"], "Handoff", state, review_tag_ok="HANDOFF_DISCIPLINE")
            state["score"] += pts; state["xp_earned"] += pts; state["xp_lines"].append(f"+{pts} Handoff")
            state["decisions"]["handoff"] = {"ticks": ticks}

        state["stage_idx"] += 1; session["case"] = state

        if state["stage_idx"] >= len(flow):
            elapsed = time.time() - state["start_ts"]
            sb = XP["speed_bonus_fast"] if elapsed <= 8*60 else (XP["speed_bonus_ok"] if elapsed <= 12*60 else 0)
            if sb: state["score"] += sb; state["xp_earned"] += sb; state["xp_lines"].append(f"+{sb} Speed bonus")
            if state["safety_caps"]:
                cap = XP["miss_required_stage_cap"]; state["xp_lines"].append(f"Score capped at {cap} (missed required in: {', '.join(state['safety_caps'])})")
                state["score"] = min(state["score"], cap)
            session["case"] = state
            return redirect(url_for("feedback"))

    key, body = _render_stage(state)
    return render_template_string(CASE_SHELL,
        title=CASE["title"], level=CASE["level"], systems=", ".join(CASE["systems"]),
        stage_num=state["stage_idx"]+1, stage_total=len(state["flow"]), stage_label=_stage_name(key),
        vitals=state["vitals"], body=body, show_imaging=(key=="imaging_panel"))

@app.route("/feedback", methods=["GET"])
def feedback():
    state = session.get("case")
    if not state: return redirect(url_for("home"))
    score = max(0, min(100, int(round(state["score"]))))
    sections = []
    def _mk_section(title, did, missed, unsafe, attending):
        sections.append({"title": title, "did": did or "—", "missed": ", ".join(missed) if missed else "", "unsafe": ", ".join(unsafe) if unsafe else "", "attending": attending})
    d = state["decisions"]

    def _map(items): return {i["id"]:i for i in items}
    # Immediate Actions
    imm = d.get("immediate_actions",{}).get("ticks",[]); I = _map(CASE["immediate_actions"]["items"])
    _mk_section("Immediate Actions", ", ".join([I[i]["text"] for i in imm]) or "None",
                [i["text"] for i in CASE["immediate_actions"]["items"] if i.get("required") and i["id"] not in imm],
                [I[i]["text"] for i in imm if I.get(i,{}).get("contra")],
                "ECG ≤10 min and monitor/IV are non-negotiable. Waiting for troponin or discharging now is unsafe in typical ACS.")
    # Targeted History
    th = d.get("targeted_history",{}).get("ticks",[]); H = _map(CASE["targeted_history"]["items"])
    _mk_section("Targeted History", ", ".join([H[i]["text"] for i in th]) or "None",
                [i["text"] for i in CASE["targeted_history"]["items"] if i.get("required") and i["id"] not in th],
                [], "Lead with red flags and pain character; keep it tight.")
    # Focused Exam
    fx = d.get("focused_exam",{}).get("ticks",[]); F = _map(CASE["focused_exam"]["items"])
    _mk_section("Focused Exam", ", ".join([F[i]["text"] for i in fx]) or "None",
                [i["text"] for i in CASE["focused_exam"]["items"] if i.get("required") and i["id"] not in fx],
                [], "Trend vitals and listen to lungs/heart immediately.")
    # ECG
    er = d.get("ecg_read",{}).get("ticks",[]); E = _map(CASE["ecg_read"]["checklist"])
    _mk_section("ECG Checklist", ", ".join([E[i]["text"] for i in er]) or "None",
                [E[i]["text"] for i in E if E[i].get("required") and i not in er],
                [E[i]["text"] for i in er if E[i].get("contra")],
                "Explicitly call out rate, rhythm, and ST changes; here ST depression V4–V6 is high risk.")
    # Order-set
    osel = d.get("order_set",{}).get("ticks",[]); O = _map(CASE["order_set"]["items"])
    _mk_section("Initial Order-Set", ", ".join([O[i]["text"] for i in osel]) or "None",
                [O[i]["text"] for i in O if O[i].get("required") and i not in osel],
                [O[i]["text"] for i in osel if O[i].get("contra")],
                "Start aspirin early (if no CI), consider GTN if BP adequate, telemetry, CXR. No blanket thrombolysis.")
    # Labs
    lb = d.get("labs_reasoning",{}).get("ticks",[]); L = {o["id"]:o for o in CASE["labs_reasoning"]["options"]}
    _mk_section("Labs Reasoning", ", ".join([L[i]["text"] for i in lb]) or "None",
                [L[i]["text"] for i in L if L[i].get("correct") and i not in lb],
                [L[i]["text"] for i in lb if L[i].get("contra")],
                "Baseline hs-trop doesn’t exclude ACS — trend serials. Renal function supports safe contrast if needed.")
    # Imaging
    im = d.get("imaging_panel",{}).get("ticks",[]); M = _map(CASE["imaging_panel"]["checklist"])
    _mk_section("Imaging (CXR)", ", ".join([M[i]["text"] for i in im]) or "None",
                [M[i]["text"] for i in M if M[i].get("required") and i not in im],
                [M[i]["text"] for i in im if M[i].get("contra")],
                "Read systematically; exclude widened mediastinum or large PTX; note congestion.")
    # Plan
    pl = d.get("plan_builder",{}).get("ticks",[]); P = _map(CASE["plan_builder"]["items"])
    _mk_section("Plan", ", ".join([P[i]["text"] for i in pl]) or "None",
                [P[i]["text"] for i in P if P[i].get("required") and i not in pl],
                [P[i]["text"] for i in pl if P[i].get("contra")],
                "Enter pathway, serial trops, escalate to cardiology with dynamic ECG or ongoing pain.")
    # Handoff
    ho = d.get("handoff",{}).get("ticks",[]); H2 = _map(CASE["handoff"]["items"])
    _mk_section("Registrar Handoff", ", ".join([H2[i]["text"] for i in ho]) or "None",
                [H2[i]["text"] for i in H2 if H2[i].get("required") and i not in ho],
                [H2[i]["text"] for i in ho if H2[i].get("contra")],
                "State times, hemodynamics, therapies + response, and the serial troponin plan.")

    review_suggestions = []
    if "ECG Checklist" in state["safety_caps"]: review_suggestions.append("ECG_ISCHAEMIA_RECOGNITION")
    if "Immediate Actions" in state["safety_caps"]: review_suggestions.append("ACS_ECG_10MIN")
    if "Plan Builder" in state["safety_caps"]: review_suggestions.append("ACS_PATHWAY")

    fb = CASE["feedback"]
    log_event("case_feedback", topic=",".join(CASE["systems"]), qid=CASE["id"], score=score, total=100, percent=score,
              from_review=1 if state.get("review_targets") else 0)

    return render_template_string(FEEDBACK_HTML,
        score=score, streak=session.get("streak",0), xp=session.get("xp",0),
        rationale=fb["rationale_html"], anz_ref=fb["anz_ref"], sections=sections,
        curriculum_outcomes=CASE["curriculum_outcomes"], escalation_cues=CASE["escalation_cues"],
        xp_breakdown=state["xp_lines"], review_suggestions=review_suggestions)

@app.route("/finish", methods=["POST"])
def finish_feedback():
    state = session.get("case") or {}
    xp_earned = int(state.get("xp_earned", 0))
    score = int(round(state.get("score", 0)))
    session["xp"] = max(0, session.get("xp",0) + xp_earned)
    maybe_increment_streak_once_today()
    log_event("case_done", topic=",".join(CASE["systems"]), qid=CASE["id"], score=score, total=100, percent=score)
    session.pop("case", None)
    return redirect(url_for("home"))

@app.route("/gate", methods=["GET","POST"])
def gate():
    access_code = os.getenv("ACCESS_CODE")
    if not access_code: return redirect(url_for("home"))
    err = None
    if request.method == "POST":
        if request.form.get("code","").strip() == access_code:
            resp = make_response(redirect(url_for("home"))); resp.set_cookie("access_ok","1", max_age=60*60*24*60); return resp
        err = "Incorrect code."
    return render_template_string("""<html><head>"""+BASE_HEAD+"""<title>Access — MedBud</title></head>
    <body class="min-h-screen flex items-center justify-center bg-gradient-to-br from-sky-500 to-indigo-600 text-white">
      <form method="post" class="bg-white/15 backdrop-blur-md p-6 rounded-2xl"><div class="flex items-center justify-between mb-2">
          <h2 class="text-xl font-extrabold">Enter Invite Code</h2></div>
        <input name="code" class="text-black p-2 rounded-lg mr-2" placeholder="Access code">
        <button class="px-4 py-2 rounded-lg bg-emerald-500 font-bold">Enter</button>
        {% if err %}<div class="text-rose-200 mt-2">{{ err }}</div>{% endif %}</form></body></html>""", err=err)

@app.route('/static/<path:filename>')
def static_file(filename):
    return send_from_directory(app.static_folder, filename)

@app.errorhandler(500)
def handle_500(e):
    # Log full traceback to stderr; show polite message to user
    print("=== MedBud 500 ===", file=sys.stderr)
    traceback.print_exc()
    return ("<h1>Something went wrong</h1><p>We've logged the error. "
            "Check Render logs for details.</p>"), 500

@app.route("/export.csv")
def export_csv():
    conn = _db()
    cur = conn.execute("SELECT ts,session_id,event,topic,qid,correct,from_review,from_anchor,variant,score,total,percent FROM events ORDER BY id DESC")
    rows = cur.fetchall(); conn.close()
    csv = "ts,session_id,event,topic,qid,correct,from_review,from_anchor,variant,score,total,percent\n"
    for r in rows: csv += ",".join("" if v is None else str(v) for v in r) + "\n"
    from flask import make_response as _mr
    resp = _mr(csv); resp.headers["Content-Type"]="text/csv"; resp.headers["Content-Disposition"]="attachment; filename=events.csv"
    return resp

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")), debug=True)
