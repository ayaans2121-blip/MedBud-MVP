from flask import Flask, render_template_string, request, redirect, url_for, session, make_response
import os, time, uuid, random, json, sqlite3
from datetime import datetime

# ======================= Flask + Secrets =======================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")

# ======================= Analytics (SQLite) ====================
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

def log_event(event, topic=None, qid=None, correct=None,
              from_review=None, from_anchor=None, score=None, total=None, percent=None):
    try:
        conn = _db()
        conn.execute(
            "INSERT INTO events (ts, session_id, event, topic, qid, correct, from_review, from_anchor, variant, score, total, percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.utcnow().isoformat(),
                session.get("sid"),
                event,
                topic,
                qid,
                int(correct) if correct is not None else None,
                int(from_review) if from_review is not None else None,
                int(from_anchor) if from_anchor is not None else None,
                "MVP",
                score, total, percent
            )
        )
        conn.commit(); conn.close()
    except Exception as e:
        print("analytics error:", e)

# ======================= Session bootstrap =====================
COOLDOWN_SECONDS = 20 * 60 * 60  # 20 hours between daily runs

@app.before_request
def ensure_session():
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    if "xp" not in session:
        session.update(dict(xp=0, streak=0, lock_until=0.0))

def now_ts(): return time.time()
def is_locked(): return now_ts() < session.get("lock_until", 0.0)
def human_time_left():
    rem = max(0, int(session.get("lock_until", 0.0) - now_ts()))
    return rem // 3600, (rem % 3600) // 60

# ======================= Cases ================================
# Case schema:
# {
#   "id": 1,
#   "systems": ["Cardio","ED"],
#   "title": "Crushing chest pain in triage",
#   "level": "Intern",
#   "flow": {
#       "priority": "stabilise_or_priority_test",   # options: stabilise_or_priority_test, history_first
#       "management_before_investigations": true,   # sepsis-like
#       "steps": ["presenting","history","exam","investigations","management","nbs","free_text"]
#   },
#   "presenting": "...",
#   "history_tips": ["radiation/exertion","risk factors","red flags"],
#   "investigations_mcq": {...},  # optional per case
#   "nbs_mcq": {...},             # single best action
#   "free_text_prompt": "Summarise diagnosis & plan (free text, 2–4 lines).",
#   "feedback": { "rationale_html": "...", "takeaways": [...], "anz_ref": "..." }
# }

CASES = [
    # --- ACS / Chest pain (priority = ECG <= 10 min; management after) ---
    {
        "id": 101,
        "systems": ["Cardio","ED"],
        "title": "Crushing chest pain in triage",
        "level": "Intern",
        "flow": {
            "priority": "stabilise_or_priority_test",
            "management_before_investigations": False,
            "steps": ["presenting","priority","history","exam","investigations","nbs","free_text"]
        },
        "presenting": "45-year-old, 30 min central pressure-like chest pain, nauseated, diaphoretic.",
        "priority_prompt": "First priority action?",
        "priority_options": [
            {"id":"A","text":"Send straight to CT pulmonary angiogram"},
            {"id":"B","text":"12-lead ECG within 10 minutes of arrival","correct":True,"safety_critical":True},
            {"id":"C","text":"Wait for troponin result first"},
            {"id":"D","text":"Discharge with outpatient stress test"}
        ],
        "history_tips": ["radiation/exertion/relief", "risk factors/family history", "associated diaphoresis, SOB"],
        "exam": "Vitals: HR 98, BP 138/84, RR 18, SpO₂ 98%. Chest clear. No murmur.",
        "investigations_mcq": {
            "prompt": "Which immediate investigation best complements your priority step?",
            "options": [
                {"id":"A","text":"Troponin at appropriate intervals","correct":True},
                {"id":"B","text":"D-dimer first line"},
                {"id":"C","text":"Routine CT brain"},
                {"id":"D","text":"Bone profile and ESR only"}
            ]
        },
        "nbs_mcq": {
            "prompt":"Next best step now?",
            "options":[
                {"id":"A","text":"Start oral antibiotics"},
                {"id":"B","text":"Aspirin + pathway-based ACS risk stratification","correct":True},
                {"id":"C","text":"Immediate discharge with GP f/u"},
                {"id":"D","text":"MRI heart urgently for everyone"}
            ]
        },
        "free_text_prompt": "Free text (2–4 lines): likely dx and immediate plan.",
        "feedback": {
            "rationale_html": "<p><b>ECG first (≤10 min)</b> for suspected ACS; then use biomarkers and pathways to risk-stratify and treat. Do not delay ECG for labs or imaging.</p>",
            "takeaways": [
                "Red-flag chest pain → ECG within 10 minutes.",
                "Use troponin serials & ACS pathways; treat as ACS until ruled out.",
                "Prioritise time-critical actions before downstream imaging."
            ],
            "anz_ref": "Australian ACS/Heart Foundation guidance: ECG within 10 minutes; pathway-based assessment."
        }
    },
    # --- Sepsis (management before full investigations) ---
    {
        "id": 202,
        "systems": ["ED","GenMed"],
        "title": "Shaking rigors on the ward",
        "level": "Intern",
        "flow": {
            "priority": "stabilise_or_priority_test",
            "management_before_investigations": True,
            "steps": ["presenting","priority","management","history","exam","investigations","nbs","free_text"]
        },
        "presenting": "72-year-old post-op (hip fixation) with fever 38.9°C, rigors, hypotension (92/58).",
        "priority_prompt": "Immediate priority?",
        "priority_options": [
            {"id":"A","text":"Pan-CT then consider antibiotics"},
            {"id":"B","text":"Blood cultures then immediate IV antibiotics + fluids","correct":True,"safety_critical":True},
            {"id":"C","text":"Observe and repeat vitals in the morning"},
            {"id":"D","text":"Oral antibiotics only"}
        ],
        "management_hint": "Give broad IV antibiotics + 30 mL/kg fluids; reassess MAP/urine output.",
        "history_tips": ["source hunt (lines, urine, lungs, surgical site)", "abx allergies/previous doses"],
        "exam": "T 38.9, HR 116, RR 24, SpO₂ 95% RA, warm peripheries, GCS 15.",
        "investigations_mcq": {
            "prompt": "Best immediate investigation while resuscitating?",
            "options":[
                {"id":"A","text":"Blood cultures prior to antibiotics","correct":True},
                {"id":"B","text":"Outpatient stool calprotectin"},
                {"id":"C","text":"Barium swallow"},
                {"id":"D","text":"DEXA scan"}
            ]
        },
        "nbs_mcq": {
            "prompt":"After first litre and antibiotics, BP remains 86/50. Next best step?",
            "options":[
                {"id":"A","text":"Start vasopressors (e.g., noradrenaline) and escalate care","correct":True},
                {"id":"B","text":"Wait another 6 hours"},
                {"id":"C","text":"Oral fluids"},
                {"id":"D","text":"Immediate CT brain"}
            ]
        },
        "free_text_prompt": "Free text (2–4 lines): likely source, immediate bundle, escalation plan.",
        "feedback": {
            "rationale_html": "<p><b>Suspected sepsis → cultures, then immediate IV antibiotics + fluids.</b> Do not delay antibiotics for labs/imaging in unstable patients; escalate early if hypotension persists.</p>",
            "takeaways": [
                "Sepsis bundle early; antibiotics should not be delayed by imaging.",
                "Resuscitate and reassess; early vasopressors if fluid-refractory hypotension.",
                "Document source hunt and escalation triggers."
            ],
            "anz_ref": "ACSQHC Sepsis Clinical Care Standard (2022)."
        }
    },
    # --- Neuro (stroke/TIA flavour: urgent imaging & eligibility) ---
    {
        "id": 303,
        "systems": ["Neuro","ED"],
        "title": "Sudden unilateral weakness",
        "level": "Intern/MS5",
        "flow": {
            "priority": "stabilise_or_priority_test",
            "management_before_investigations": False,
            "steps": ["presenting","priority","exam","investigations","nbs","free_text"]
        },
        "presenting": "60-year-old with 45 minutes of right-sided weakness and aphasia.",
        "priority_prompt": "First priority?",
        "priority_options": [
            {"id":"A","text":"Urgent brain imaging to assess for haemorrhage/ischaemia (CT/MRI based on access)","correct":True,"safety_critical":True},
            {"id":"B","text":"Outpatient referral for MRI"},
            {"id":"C","text":"Start anticoagulation immediately without imaging"},
            {"id":"D","text":"Send D-dimer and wait"}
        ],
        "exam": "BP 162/94. Facial droop and dense right arm weakness. Glucose 5.6.",
        "investigations_mcq": {
            "prompt": "Most appropriate immediate test set?",
            "options":[
                {"id":"A","text":"Non-contrast CT ± CT angiography (if MRI not available within 30 min)","correct":True},
                {"id":"B","text":"DEXA scan"},
                {"id":"C","text":"Abdominal ultrasound"},
                {"id":"D","text":"Echocardiography as first and only test"}
            ]
        },
        "nbs_mcq": {
            "prompt":"Imaging shows ischaemic stroke within window. Next best step?",
            "options":[
                {"id":"A","text":"Consider reperfusion eligibility per protocol; urgent stroke team involvement","correct":True},
                {"id":"B","text":"Reassure and discharge"},
                {"id":"C","text":"Start random vitamins"},
                {"id":"D","text":"Delay therapy for 24 hours to observe"}
            ]
        },
        "free_text_prompt": "Free text (2–4 lines): immediate steps & disposition.",
        "feedback": {
            "rationale_html": "<p><b>Stroke suspicion → urgent imaging</b> to distinguish haemorrhage vs ischaemia and determine eligibility for reperfusion. Time is brain.</p>",
            "takeaways": [
                "Do imaging urgently; pathway determines reperfusion.",
                "Check glucose, BP, onset time, contraindications.",
                "Early stroke team escalation."
            ],
            "anz_ref": "Stroke Foundation (AU) Clinical Guidelines."
        }
    }
]

# --- (Optional) Switch to external cases.json later ---
# To activate JSON loading, put your cases in cases.json (same schema) and uncomment below:
# try:
#     with open(os.path.join(os.path.dirname(__file__), "cases.json"), "r", encoding="utf-8") as f:
#         CASES = json.load(f)
# except Exception as _e:
#     print("cases.json not loaded (using built-in cases):", _e)

SYSTEMS = ["Cardio","Neuro","Resp","Endo","Heme","ED","GenMed","GP"]

# ======================= Home ================================
HOME_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>MedBud — Clinical Judgment Trainer</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-gradient-to-br from-sky-500 via-indigo-500 to-emerald-500 text-white flex items-center justify-center p-4">
  <div class="w-full max-w-3xl bg-white/15 backdrop-blur-md rounded-2xl p-6 shadow-xl">
    <h1 class="text-3xl font-extrabold mb-1">🧠 MedBud — Clinical Judgment Trainer</h1>
    <p class="opacity-90 mb-4">Pick 1–3 systems and how many cases you want today. Text-only, 10–15 min per case, instant feedback aligned to Australian guidance.</p>

    <div class="flex flex-wrap gap-2 mb-4">
      <span class="px-3 py-1 rounded-full bg-white/20">🔥 Streak: {{streak}}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">⭐ XP: {{xp}}</span>
      {% if locked %}
        <span class="px-3 py-1 rounded-full bg-black/30">🔒 Next unlock in {{h}}h {{m}}m</span>
      {% endif %}
    </div>

    <form method="post" class="space-y-4">
      <div>
        <label class="font-semibold">Systems</label>
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2">
          {% for s in systems %}
            <label class="flex items-center gap-2 bg-white/10 rounded-xl p-2">
              <input type="checkbox" name="systems" value="{{s}}" class="accent-emerald-400">
              <span>{{s}}</span>
            </label>
          {% endfor %}
        </div>
      </div>
      <div class="flex items-center gap-3">
        <label class="font-semibold">Cases today</label>
        <select name="count" class="text-black rounded-lg p-2">
          <option>1</option><option selected>2</option><option>3</option>
        </select>
      </div>
      <button class="px-5 py-3 rounded-xl font-bold bg-emerald-500 hover:bg-emerald-600 disabled:opacity-60" {% if locked %}disabled{% endif %}>
        Start
      </button>
    </form>
    {% if locked %}
      <p class="mt-3 text-sm opacity-90">You’ve completed today. Come back after the cooldown.</p>
    {% endif %}
  </div>
</body>
</html>
"""

@app.route("/", methods=["GET","POST"])
def home():
    if request.method == "POST":
        if is_locked():
            return redirect(url_for("home"))
        chosen = request.form.getlist("systems")
        count = int(request.form.get("count","2"))
        if not chosen:
            chosen = ["ED"]  # default
        pool = [c for c in CASES if any(s in c["systems"] for s in chosen)]
        random.shuffle(pool)
        session["queue"] = [c["id"] for c in pool[:max(1,min(3,count))]]
        session["progress"] = dict(idx=0, score=0, started=now_ts())
        log_event("start_day", topic=",".join(chosen))
        return redirect(url_for("case_entry"))
    h,m = human_time_left()
    return render_template_string(HOME_HTML, systems=SYSTEMS, xp=session["xp"], streak=session["streak"], locked=is_locked(), h=h, m=m)

# ======================= Helpers ===============================
def _get_case_by_id(cid):
    for c in CASES:
        if c["id"] == cid: return c
    return None

def _score_free_text(text, keywords):
    text_l = (text or "").lower()
    pts = 0
    for kw in keywords:
        if any(k in text_l for k in kw.split("|")):
            pts += 5
    return min(20, pts)  # cap

# ======================= Case engine ===========================
@app.route("/case", methods=["GET","POST"])
def case_entry():
    q = session.get("queue", [])
    p = session.get("progress", {"idx":0,"score":0})
    if not q or p["idx"] >= len(q): return redirect(url_for("summary"))
    case = _get_case_by_id(q[p["idx"]])
    # bootstrap per-case state
    if "case_state" not in session:
        session["case_state"] = {"stage": 0, "history_chosen": [], "priority_choice": None, "invest_choice": None, "nbs_choice": None, "free_text": ""}
    return redirect(url_for("case_stage"))

def _build_stage_list(case):
    # Build a dynamic list of stage identifiers based on case.flow
    steps = case["flow"]["steps"][:]
    # If management_before_investigations == True, ensure "management" comes before "investigations"
    if case["flow"].get("management_before_investigations"):
        if "management" in steps and "investigations" in steps:
            mi = steps.index("management"); ii = steps.index("investigations")
            if mi > ii:
                steps.pop(mi); steps.insert(steps.index("investigations"), "management")
    # Priority always near start if specified
    if case["flow"].get("priority") == "stabilise_or_priority_test":
        if "priority" not in steps: steps.insert(1, "priority")
    return steps

CASE_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{{case['title']}}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-slate-900 text-slate-100 p-4">
  <div class="max-w-3xl mx-auto">
    <div class="flex items-center justify-between mb-3">
      <h1 class="text-2xl font-extrabold">{{case['title']}}</h1>
      <div class="text-sm opacity-80">{{case['level']}} • {{', '.join(case['systems'])}}</div>
    </div>

    <div class="bg-slate-800/70 rounded-xl p-4 mb-3">
      <div class="text-sm">Stage {{stage_num}} / {{stage_total}}</div>
      <div class="mt-1 text-slate-200">{{stage_label}}</div>
    </div>

    <form method="post" class="space-y-4">
      {{body|safe}}
      <div class="flex gap-2">
        <a href="{{url_for('quit_case')}}" class="px-4 py-2 rounded-lg bg-slate-700">Quit</a>
        <button class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 font-bold">Continue</button>
      </div>
    </form>
  </div>
</body>
</html>
"""

@app.route("/stage", methods=["GET","POST"])
def case_stage():
    q = session.get("queue", [])
    p = session.get("progress", {"idx":0,"score":0})
    cstate = session.get("case_state", {"stage":0})
    if not q: return redirect(url_for("summary"))
    case = _get_case_by_id(q[p["idx"]])
    stages = _build_stage_list(case)

    # Handle POST scoring/advance
    if request.method == "POST":
        stage_key = stages[cstate["stage"]]
        # Collect inputs & score lite
        if stage_key == "priority":
            choice = request.form.get("choice")
            cstate["priority_choice"] = choice
            # priority correct worth 20 (safety critical distractor penalty −20 if chosen)
            correct_id = next((o["id"] for o in case["priority_options"] if o.get("correct")), None)
            if choice == correct_id: p["score"] += 20
            elif any(o["id"]==choice and o.get("safety_critical") for o in case["priority_options"]):
                p["score"] = max(0, p["score"] - 20)
        elif stage_key == "history":
            chosen = request.form.getlist("hx")
            cstate["history_chosen"] = chosen[:3]
            p["score"] += min(15, 5*len(cstate["history_chosen"]))
        elif stage_key == "exam":
            p["score"] += 5
        elif stage_key == "management":
            # acknowledging they saw/respected early management hint
            p["score"] += 10
        elif stage_key == "investigations":
            inv = case.get("investigations_mcq")
            if inv:
                choice = request.form.get("choice")
                cstate["invest_choice"] = choice
                corr = next((o["id"] for o in inv["options"] if o.get("correct")), None)
                if choice == corr: p["score"] += 15
        elif stage_key == "nbs":
            nbs = case.get("nbs_mcq")
            if nbs:
                choice = request.form.get("choice")
                cstate["nbs_choice"] = choice
                corr = next((o["id"] for o in nbs["options"] if o.get("correct")), None)
                if choice == corr: p["score"] += 25
        elif stage_key == "free_text":
            ft = request.form.get("free_text","").strip()
            cstate["free_text"] = ft
            # simple keyword scoring by case
            kw = []
            if case["id"]==101: kw = ["ecg|twelve-lead","aspirin|antiplatelet","troponin","acs|nstemi|stemi","risk|pathway"]
            if case["id"]==202: kw = ["antibiotic|broad-spectrum","fluids|bolus|30 ml/kg","cultures","vasopressor|noradrenaline","escalate|icu"]
            if case["id"]==303: kw = ["ct|mri|imaging","stroke|ischaemic|haemorrhage","reperfusion|thrombolysis|thrombectomy","onset time|window","stroke team|neurology"]
            p["score"] += _score_free_text(ft, kw)

        cstate["stage"] += 1
        session["case_state"] = cstate
        session["progress"] = p
        # Next stage or feedback
        if cstate["stage"] >= len(stages):
            return redirect(url_for("feedback"))
        return redirect(url_for("case_stage"))

    # Render GET
    stage_key = stages[cstate["stage"]]
    stage_names = {
        "presenting":"Presenting Problem",
        "priority":"Immediate Priority",
        "history":"Targeted History",
        "exam":"Focused Exam/Vitals",
        "management":"Immediate Management",
        "investigations":"Investigations",
        "nbs":"Next Best Step",
        "free_text":"Free-text Summary"
    }
    stage_label = stage_names.get(stage_key, stage_key)
    body = ""

    if stage_key == "presenting":
        body = f"<p class='text-lg'>{case['presenting']}</p>"
    elif stage_key == "priority":
        opts = case.get("priority_options", [])
        body = "<p class='mb-2'>" + case.get("priority_prompt","Immediate priority?") + "</p>"
        for o in opts:
            body += f"""
              <label class='block bg-slate-800 p-3 rounded-lg mb-2'>
                <input required type='radio' name='choice' value='{o['id']}' class='mr-2 accent-indigo-500'>
                <span>{o['id']}) {o['text']}</span>
              </label>"""
    elif stage_key == "history":
        chips = "".join([f"<label class='inline-flex items-center gap-2 bg-slate-800 rounded-full px-3 py-2 mr-2 mb-2'><input type='checkbox' name='hx' value='{h}' class='accent-emerald-400'><span>{h}</span></label>" for h in case.get("history_tips", [])])
        body = "<p class='mb-2'>Pick up to 3 targeted history questions (prioritise):</p>" + chips
    elif stage_key == "exam":
        body = f"<p class='mb-2'>Focused exam & vitals:</p><div class='bg-slate-800 p-3 rounded-lg'>{case.get('exam','')}</div>"
    elif stage_key == "management":
        body = f"<p class='mb-2'>Immediate management (when indicated):</p><div class='bg-amber-100 text-amber-900 p-3 rounded-lg'>{case.get('management_hint','Start stabilisation and time-critical therapy as indicated.')}</div>"
    elif stage_key == "investigations":
        inv = case.get("investigations_mcq")
        if inv:
            body = f"<p class='mb-2'>{inv['prompt']}</p>"
            for o in inv["options"]:
                body += f"""
                <label class='block bg-slate-800 p-3 rounded-lg mb-2'>
                  <input required type='radio' name='choice' value='{o['id']}' class='mr-2 accent-indigo-500'>
                  <span>{o['id']}) {o['text']}</span>
                </label>"""
        else:
            body = "<p>No investigations in this case step.</p>"
    elif stage_key == "nbs":
        nbs = case.get("nbs_mcq")
        if nbs:
            body = f"<p class='mb-2'>{nbs['prompt']}</p>"
            for o in nbs["options"]:
                body += f"""
                <label class='block bg-slate-800 p-3 rounded-lg mb-2'>
                  <input required type='radio' name='choice' value='{o['id']}' class='mr-2 accent-indigo-500'>
                  <span>{o['id']}) {o['text']}</span>
                </label>"""
        else:
            body = "<p>No NBS MCQ in this case.</p>"
    elif stage_key == "free_text":
        body = f"""
        <p class='mb-2'>{case.get('free_text_prompt','Free text: your summary & plan')}</p>
        <textarea name="free_text" rows="4" class="w-full rounded-lg p-3 text-black" placeholder="2–4 lines..."></textarea>
        """

    return render_template_string(
        CASE_HTML,
        case=case,
        stage_num=cstate["stage"]+1,
        stage_total=len(stages),
        stage_label=stage_label,
        body=body
    )

@app.route("/quit")
def quit_case():
    # reset current case state but keep queue to allow restart
    session.pop("case_state", None)
    return redirect(url_for("home"))

# ======================= Feedback ==============================
FEEDBACK_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Feedback</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-gradient-to-br from-emerald-50 to-sky-50 text-slate-900 p-4">
  <div class="max-w-3xl mx-auto bg-white rounded-2xl shadow p-5">
    <h2 class="text-2xl font-extrabold mb-2">Case Feedback</h2>
    <div class="flex flex-wrap gap-2 mb-3">
      <span class="px-3 py-1 rounded-full bg-emerald-100 text-emerald-900">Score: {{score}} / 100</span>
      <span class="px-3 py-1 rounded-full bg-indigo-100 text-indigo-900">🔥 Streak: {{streak}}</span>
      <span class="px-3 py-1 rounded-full bg-amber-100 text-amber-900">⭐ XP: {{xp}}</span>
    </div>
    <div class="space-y-4">
      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Instant Rationale</h3>
        <div class="prose max-w-none">{{rationale|safe}}</div>
        <p class="text-sm text-slate-600 mt-2 italic">{{anz_ref}}</p>
      </div>
      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Takeaways</h3>
        <ul class="list-disc ml-6">
          {% for t in takeaways %}<li>{{t}}</li>{% endfor %}
        </ul>
      </div>
      <form method="post" action="{{url_for('next_case')}}">
        <button class="px-5 py-3 rounded-xl font-bold bg-indigo-600 text-white hover:bg-indigo-700">Continue</button>
        <a href="{{url_for('summary')}}" class="ml-2 px-4 py-3 rounded-xl bg-slate-200">Finish Today</a>
      </form>
    </div>
  </div>
</body>
</html>
"""

@app.route("/feedback")
def feedback():
    q = session.get("queue", [])
    p = session.get("progress", {"idx":0,"score":0})
    cstate = session.get("case_state", {})
    if not q: return redirect(url_for("summary"))
    case = _get_case_by_id(q[p["idx"]])

    # Finalise score for case (add small speed bonus if < 12 min)
    elapsed = now_ts() - p.get("started", now_ts())
    speed_bonus = 10 if elapsed <= 12*60 else 0
    score = min(100, p["score"] + speed_bonus)
    session["progress"]["last_score"] = score

    # Feedback
    fb = case["feedback"]
    log_event("case_feedback", topic=",".join(case["systems"]), qid=case["id"], score=score, percent=score)

    return render_template_string(
        FEEDBACK_HTML,
        score=score, streak=session["streak"], xp=session["xp"], rationale=fb["rationale_html"], takeaways=fb["takeaways"], anz_ref=fb["anz_ref"]
    )

@app.route("/next", methods=["POST"])
def next_case():
    q = session.get("queue", [])
    p = session.get("progress", {"idx":0,"score":0})
    last = int(p.get("last_score", 0))
    session["xp"] = session.get("xp",0) + last
    session["case_state"] = {"stage":0,"history_chosen":[]}
    p["idx"] += 1
    p["score"] = 0
    session["progress"] = p
    if p["idx"] >= len(q):
        return redirect(url_for("summary"))
    return redirect(url_for("case_entry"))

# ======================= Day summary ===========================
SUMMARY_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Today’s Summary</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="min-h-screen bg-gradient-to-br from-indigo-600 to-sky-600 text-white p-4">
  <div class="max-w-2xl mx-auto bg-white/15 backdrop-blur-md rounded-2xl p-6 shadow-xl">
    <h2 class="text-2xl font-extrabold mb-2">🎉 Daily Run Complete</h2>
    <p class="opacity-90 mb-3">Great work. Your daily window is now locked to preserve the “short, sticky rep” habit.</p>
    <div class="flex flex-wrap gap-2 mb-4">
      <span class="px-3 py-1 rounded-full bg-white/20">🔥 Streak: {{streak}}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">⭐ XP: {{xp}}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">⏱️ Next unlock: {{h}}h {{m}}m</span>
    </div>
    <a href="{{url_for('home')}}" class="inline-block px-5 py-3 rounded-xl font-bold bg-emerald-500 hover:bg-emerald-600">Back Home</a>
  </div>
</body>
</html>
"""

@app.route("/summary")
def summary():
    # increment streak and lock the day
    last_lock = session.get("lock_until", 0.0)
    if now_ts() >= last_lock - 60:
        session["streak"] = session.get("streak",0) + 1
        session["lock_until"] = now_ts() + COOLDOWN_SECONDS
    h,m = human_time_left()
    log_event("day_done", score=session.get("xp"))
    return render_template_string(SUMMARY_HTML, xp=session["xp"], streak=session["streak"], h=h, m=m)

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
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1"><script src="https://cdn.tailwindcss.com"></script></head>
    <body class="min-h-screen flex items-center justify-center bg-gradient-to-br from-sky-500 to-indigo-600 text-white">
      <form method="post" class="bg-white/15 backdrop-blur-md p-6 rounded-2xl">
        <h2 class="text-xl font-extrabold mb-2">Enter Invite Code</h2>
        <input name="code" class="text-black p-2 rounded-lg mr-2" placeholder="Access code">
        <button class="px-4 py-2 rounded-lg bg-emerald-500 font-bold">Enter</button>
        {% if err %}<div class="text-rose-200 mt-2">{{err}}</div>{% endif %}
      </form>
    </body></html>
    """, err=err)

@app.before_request
def guard_gate():
    access_code = os.getenv("ACCESS_CODE")
    if access_code and request.endpoint not in ("gate","static"):
        if not request.cookies.get("access_ok"):
            return redirect(url_for("gate"))

# ======================= Run (local) ===========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")), debug=True)
