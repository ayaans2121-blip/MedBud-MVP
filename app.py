from flask import Flask, render_template_string, request, redirect, url_for, session, make_response, jsonify
import time, random, os, sqlite3, uuid, math
from datetime import datetime

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

def log_event(event, topic=None, qid=None, correct=None,
              from_review=None, from_anchor=None,
              score=None, total=None, percent=None):
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
                session.get("variant"),
                score,
                total,
                percent
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print("analytics error:", e)

# ======================= AB + Invite Gate =======================
@app.before_request
def ensure_session_and_variant():
    access_code = os.getenv("ACCESS_CODE")
    if access_code:
        if request.endpoint not in ("gate", "static") and not request.cookies.get("access_ok"):
            return redirect(url_for("gate"))

    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    if "variant" not in session:
        session["variant"] = random.choice(["A", "B"])

    _state()

@app.route("/gate", methods=["GET", "POST"])
def gate():
    access_code = os.getenv("ACCESS_CODE")
    if not access_code:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        if request.form.get("code", "").strip() == access_code:
            resp = make_response(redirect(url_for("home")))
            resp.set_cookie("access_ok", "1", max_age=60*60*24*60)
            return resp
        else:
            error = "Incorrect code. Try again."
    return render_template_string("""
    <html><head><title>Access</title>
    <style>
    body{font-family:Arial;display:flex;align-items:center;justify-content:center;height:100vh;
         background:linear-gradient(120deg,#0ea5e9,#8b5cf6);color:white}
    .card{background:rgba(255,255,255,0.15);padding:28px 24px;border-radius:16px;backdrop-filter:blur(6px)}
    input{padding:10px;border-radius:10px;border:none;width:220px;margin-right:8px}
    button{padding:10px 16px;border:none;border-radius:10px;background:#22c55e;color:white;font-weight:700}
    .err{margin-top:10px;color:#fee2e2}
    </style></head>
    <body>
      <div class="card">
        <h2>Enter Invite Code</h2>
        <form method="post"><input name="code" placeholder="Access code"><button>Enter</button></form>
        {% if error %}<div class="err">{{error}}</div>{% endif %}
      </div>
    </body></html>
    """, error=error)

# ======================= ENSO MVP: Daily Clinical Judgment Case =======================

COOLDOWN_SECONDS = 20 * 60 * 60  # 20 hours
FRIDAY_BOSS = True  # shows a harder case on Fridays

# Minimal case schema (author-friendly)
# Each case trains ONE "next best step" (nbs).
CASES = [
    {
        "id": 1,
        "topic": "ED – Chest Pain",
        "level": "Intern",
        "title": "Crushing chest pain in triage",
        "stem": "45-year-old with 30 minutes of central, pressure-like chest pain, nauseated, diaphoretic, no known history.",
        "history_options": [
            {"key":"radiation", "label":"Ask about radiation/exertion/relief", "value":"+ to left arm; exertional; no relief with rest"},
            {"key":"risk", "label":"Ask PMHx and risks", "value":"Smoker 20 pack-years; father had MI at 52"},
            {"key":"gi", "label":"Ask reflux/meals", "value":"No clear relation to meals"}
        ],
        "exam": "Vitals: HR 98, BP 138/84, RR 18, SpO₂ 98%. Chest clear. No murmur.",
        "nbs": {
            "prompt":"What is the next best step?",
            "options":[
                {"id":"A","text":"Order CT pulmonary angiogram"},
                {"id":"B","text":"Obtain ECG within 10 minutes","correct":True,"safety_critical":True},
                {"id":"C","text":"Check troponin first, then ECG"},
                {"id":"D","text":"Discharge with outpatient stress test"}
            ]
        },
        "rationale_html": "<p><b>ECG first.</b> In suspected ACS, ECG within 10 minutes identifies ST-segment changes and guides immediate therapy. Troponin may be normal early and <i>must not</i> delay ECG. CT first delays lifesaving actions; outpatient workup is unsafe with red flags.</p>",
        "pitfalls": [
            "Ordering troponin before ECG (delays critical decision).",
            "Going to CT first (misprioritises ACS over PE in this context).",
            "Discharging despite red flags (exertional, diaphoresis, radiation)."
        ],
        "takeaways": [
            "In chest pain with red flags, get ECG ≤10 minutes.",
            "Treat as ACS until ruled out; tests complement, not precede ECG.",
            "Prioritise time-critical actions before downstream imaging."
        ],
        "anz_ref": "RACGP Chest Pain 2023: ECG within 10 minutes for suspected ACS."
    },
    {
        "id": 2,
        "topic": "Gen Med – Sepsis screen",
        "level": "Intern",
        "title": "Shaking rigors on the ward",
        "stem": "72-year-old with fever 38.9°C, rigors, hypotension (BP 92/58) 2 hours post-op hip fixation.",
        "history_options": [
            {"key":"focus", "label":"Ask likely source (surgical, lines, urine, lungs)", "value":"Surgical drain serosanguinous; foley present; mild cough"},
            {"key":"abx", "label":"Allergies / prior antibiotics", "value":"No allergies; received cefazolin"},
            {"key":"comorb", "label":"Comorbidities", "value":"DM2; CKD stage 3"}
        ],
        "exam": "T 38.9, HR 116, RR 24, SpO₂ 95% RA. Warm peripheries, GCS 15.",
        "nbs": {
            "prompt":"What is the next best step?",
            "options":[
                {"id":"A","text":"Pan-CT imaging then consider antibiotics"},
                {"id":"B","text":"Immediate broad-spectrum IV antibiotics and fluids","correct":True,"safety_critical":True},
                {"id":"C","text":"Wait for lactate and cultures before antibiotics"},
                {"id":"D","text":"Start oral antibiotics and review in the morning"}
            ]
        },
        "rationale_html": "<p><b>Early IV antibiotics + fluids now.</b> In suspected sepsis with hypotension and tachycardia, give timely broad-spectrum antibiotics after prompt cultures but <i>do not</i> delay antibiotics for labs/imaging. Early resuscitation reduces mortality.</p>",
        "pitfalls": [
            "Waiting for labs/cultures before antibiotics.",
            "Sending patient to CT while unstable.",
            "Treating with oral antibiotics in a hypotensive patient."
        ],
        "takeaways": [
            "Sepsis bundle: cultures then immediate IV antibiotics + fluids.",
            "Do not delay antibiotics for imaging in unstable patients.",
            "Reassess response frequently; escalate early."
        ],
        "anz_ref": "ACSQHC Sepsis Clinical Care Standard (AU)."
    },
    {
        "id": 3,
        "topic": "Neuro – First seizure",
        "level": "MS5/Intern",
        "title": "Collapsed at home with witnessed tonic-clonic activity",
        "stem": "28-year-old had a 2-minute tonic-clonic seizure, now post-ictal, afebrile, glucose 5.6, no head trauma.",
        "history_options": [
            {"key":"hx", "label":"Ask precipitating factors/tox/exposure", "value":"No alcohol binge, no new meds, poor sleep"},
            {"key":"neuro", "label":"Ask focal neuro symptoms/headache", "value":"No headache, no focal deficits reported"},
            {"key":"pmhx", "label":"Past neuro history", "value":"None; no family epilepsy"}
        ],
        "exam": "Neuro exam after recovery: non-focal. Vitals normal.",
        "nbs": {
            "prompt":"What is the next best step?",
            "options":[
                {"id":"A","text":"Start long-term anti-seizure medication immediately"},
                {"id":"B","text":"Non-contrast CT brain now to exclude bleed/structural cause","correct":True},
                {"id":"C","text":"Discharge with reassurance and outpatient EEG only"},
                {"id":"D","text":"CT pulmonary angiogram"}
            ]
        },
        "rationale_html": "<p><b>Non-contrast CT now.</b> First seizure warrants evaluation for structural causes or hemorrhage. Long-term medication often deferred until full work-up. Pure reassurance is unsafe without imaging depending on context.</p>",
        "pitfalls": [
            "Starting long-term medication before excluding secondary causes.",
            "Skipping imaging on a first seizure without red-flag assessment.",
            "Ordering unrelated tests (e.g., CTPA)."
        ],
        "takeaways": [
            "First seizure: assess for red flags and structural causes.",
            "CT brain (non-contrast) is appropriate in many ED settings.",
            "Plan further outpatient EEG/MRI as indicated."
        ],
        "anz_ref": "ANZ Neurology guidance; ED first seizure pathways."
    }
]

# Simple rotation for a "boss case" (reuse IDs or mark one as harder)
BOSS_CASE_ID = 2  # make the sepsis case the 'boss' by default

# ======================= Per-user state =======================
USERS = {}
def _blank_state():
    return {
        "xp": 0,
        "streak": 0,
        "last_completed_at": 0.0,
        "cycle_lock_until": 0.0,
        "today_case_id": None,
        "today_started_at": None,
        "history_selected": [],
        "mode": "text",  # or "voice"
        "scores": [],  # history_points, decision_correct, total_score
        "recent_case_ids": [],
    }

def _state():
    sid = session.get("sid")
    if not sid:
        session["sid"] = str(uuid.uuid4())
        sid = session["sid"]
    if sid not in USERS:
        USERS[sid] = _blank_state()
    return USERS[sid]

def now_ts(): return time.time()
def is_locked(S): return now_ts() < S.get("cycle_lock_until", 0.0)
def human_time_left(S):
    remaining = max(0, int(S.get("cycle_lock_until", 0.0) - now_ts()))
    h = remaining // 3600
    m = (remaining % 3600) // 60
    return h, m

def _pick_today_case():
    # Friday boss case (optional)
    weekday = datetime.utcnow().weekday()  # 0=Mon ... 4=Fri
    if FRIDAY_BOSS and weekday == 4:
        return next(c for c in CASES if c["id"] == BOSS_CASE_ID)
    # otherwise pick one not used recently
    recent = set(_state().get("recent_case_ids", [])[-5:])
    pool = [c for c in CASES if c["id"] not in recent]
    if not pool:
        pool = CASES[:]
    return random.choice(pool)

# ======================= HOME =======================
@app.route("/", methods=["GET","POST"])
def home():
    S = _state()
    locked = is_locked(S)
    h_left, m_left = human_time_left(S)

    if request.method == "POST":
        if locked:
            return redirect(url_for("home"))
        # Choose mode
        S["mode"] = request.form.get("mode","text")
        case = _pick_today_case()
        S["today_case_id"] = case["id"]
        S["today_started_at"] = now_ts()
        S["history_selected"] = []
        session["score"] = 0
        session["unsafe"] = 0
        log_event("start_case", topic=case["topic"], qid=case["id"])
        return redirect(url_for("case_history"))

    return render_template_string("""
    <html>
    <head>
      <title>ENSO — Daily Clinical Rep</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <style>
        body{font-family:Inter,Arial,system-ui;margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:radial-gradient(1200px 800px at 15% 10%,#34d399 0%,#3b82f6 35%,#8b5cf6 70%,#0ea5e9 100%);color:white}
        .card{background:rgba(0,0,0,0.2);padding:28px;border-radius:18px;backdrop-filter:blur(6px);max-width:680px}
        h1{margin:0 0 6px}
        .sub{opacity:0.95;margin-bottom:16px}
        .row{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
        button{padding:12px 16px;border:none;border-radius:12px;font-weight:700;color:white;cursor:pointer}
        .start{background:linear-gradient(135deg,#22c55e,#16a34a)}
        .disabled{opacity:0.6;cursor:not-allowed}
        .chip{display:inline-block;background:rgba(255,255,255,0.16);padding:8px 12px;border-radius:999px;margin-right:8px}
        small{opacity:0.9}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>🧠 ENSO — Daily Clinical Judgment Rep</h1>
        <div class="sub">One 10–15 min case/day. Voice or text. Train <b>next best step</b> with instant rationale.</div>
        <div style="margin:8px 0 16px">
          <span class="chip">🔥 Streak: {{streak}}</span>
          <span class="chip">⭐ XP: {{xp}}</span>
        </div>
        {% if locked %}
          <div style="margin-bottom:12px;background:rgba(0,0,0,0.25);padding:10px;border-radius:10px">
            🔒 Daily complete. Next unlock in <b>{{h_left}}h {{m_left}}m</b>.
          </div>
        {% endif %}
        <form method="post">
          <div class="row">
            <label><input type="radio" name="mode" value="voice" checked> Voice (with text fallback)</label>
            <label><input type="radio" name="mode" value="text"> Text only</label>
          </div>
          <div class="row">
            <button class="start {% if locked %}disabled{% endif %}" {% if locked %}disabled{% endif %}>Start Today’s Case</button>
          </div>
        </form>
        <div style="margin-top:10px"><small>Tip: If voice is buggy on your device, switch to Text anytime.</small></div>
      </div>
    </body>
    </html>
    """, streak=S["streak"], xp=S["xp"], h_left=h_left, m_left=m_left, locked=locked)

# ======================= CASE: HISTORY / EXAM =======================
def _get_case():
    cid = _state().get("today_case_id")
    if not cid:
        return None
    for c in CASES:
        if c["id"] == cid:
            return c
    return None

@app.route("/history", methods=["GET","POST"])
def case_history():
    S = _state()
    case = _get_case()
    if not case: return redirect(url_for("home"))

    if request.method == "POST":
        selected = request.form.getlist("hx")
        # Keep only 3 max to force prioritisation
        selected = selected[:3]
        S["history_selected"] = selected
        # History points: each selected option worth 10 if it narrows risk
        history_points = 0
        for opt in case["history_options"]:
            if opt["key"] in selected:
                history_points += 10
        session["history_points"] = min(history_points, 30)  # cap at 30
        log_event("hx_done", topic=case["topic"], qid=case["id"], score=session["history_points"])
        return redirect(url_for("case_exam"))

    mode = S.get("mode","text")
    return render_template_string("""
    <html><head><title>History</title><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body{font-family:Inter,Arial,system-ui;margin:0;padding:24px;background:#0f172a;color:#e2e8f0}
      .wrap{max-width:760px;margin:0 auto}
      .card{background:#111827;border:1px solid #1f2937;border-radius:14px;padding:16px;margin:10px 0}
      .btn{padding:12px 16px;border-radius:12px;border:none;color:white;background:linear-gradient(135deg,#2563eb,#1d4ed8);font-weight:700}
      .hx{display:flex;gap:10px;flex-wrap:wrap}
      label.hxopt{display:inline-flex;gap:8px;align-items:center;background:#0b1220;border:1px solid #243047;padding:10px 12px;border-radius:10px}
      .pill{display:inline-block;background:#0b3b2f;color:#34d399;padding:4px 8px;border-radius:999px;margin-left:10px}
      .mode{font-size:14px;opacity:0.85}
    </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <div class="mode">Mode: <b>{{mode}}</b></div>
          <h2>{{case['title']}}</h2>
          <p><b>{{case['topic']}}</b> • {{case['level']}}</p>
          <p>{{case['stem']}}</p>
          <p><i>Pick up to 3 targeted history items (prioritise!).</i></p>
          <form method="post">
            <div class="hx">
              {% for opt in case['history_options'] %}
                <label class="hxopt">
                  <input type="checkbox" name="hx" value="{{opt['key']}}" />
                  {{opt['label']}} <span class="pill">reveals: {{opt['value']}}</span>
                </label>
              {% endfor %}
            </div>
            <div style="margin-top:12px">
              <button class="btn">Continue to Exam</button>
              <a class="btn" href="{{url_for('home')}}" style="background:#374151">Quit</a>
            </div>
          </form>
        </div>
      </div>
      {% if mode=='voice' %}
        <script>
          // Progressive enhancement: read stem out loud if speechSynthesis exists
          if ('speechSynthesis' in window) {
            const msg = new SpeechSynthesisUtterance("{{case['title']}}. {{case['stem']}}. Pick up to three history questions.");
            speechSynthesis.speak(msg);
          }
        </script>
      {% endif %}
    </body></html>
    """, case=case, mode=mode)

@app.route("/exam", methods=["GET","POST"])
def case_exam():
    S = _state()
    case = _get_case()
    if not case: return redirect(url_for("home"))
    if request.method == "POST":
        log_event("exam_viewed", topic=case["topic"], qid=case["id"])
        return redirect(url_for("case_decision"))
    return render_template_string("""
    <html><head><title>Exam</title><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body{font-family:Inter,Arial,system-ui;margin:0;padding:24px;background:#0f172a;color:#e2e8f0}
      .wrap{max-width:760px;margin:0 auto}
      .card{background:#111827;border:1px solid #1f2937;border-radius:14px;padding:16px;margin:10px 0}
      .btn{padding:12px 16px;border-radius:12px;border:none;color:white;background:linear-gradient(135deg,#22c55e,#16a34a);font-weight:700}
    </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <h2>Exam & Vitals</h2>
          <p>{{case['exam']}}</p>
          <form method="post">
            <button class="btn">Proceed to Next Best Step</button>
          </form>
        </div>
      </div>
    </body></html>
    """, case=case)

# ======================= DECISION (NBS) =======================
@app.route("/decision", methods=["GET","POST"])
def case_decision():
    S = _state()
    case = _get_case()
    if not case: return redirect(url_for("home"))

    if request.method == "POST":
        choice = request.form.get("choice")
        correct_id = next((o["id"] for o in case["nbs"]["options"] if o.get("correct")), None)
        correct = (choice == correct_id)
        unsafe = 1 if any(o["id"] == choice and o.get("safety_critical") for o in case["nbs"]["options"]) else 0

        # Scoring rubric
        history_points = int(session.get("history_points", 0))            # ≤30
        decision_points = 40 if correct else 0                            # 40
        data_points = 10                                                  # simple credit for reaching decision
        reflection_points = 0                                             # added on /feedback
        speed_bonus = 0
        # speed bonus if under 10 minutes since start
        started = _state().get("today_started_at") or now_ts()
        elapsed = now_ts() - started
        if elapsed <= 10*60: speed_bonus = 10

        base_score = history_points + decision_points + data_points + speed_bonus
        # unsafe penalty if wrong & safety-critical distractor chosen
        if unsafe and not correct:
            base_score = max(0, base_score - 30)

        session["nbs_choice"] = choice
        session["nbs_correct"] = int(correct)
        session["unsafe"] = unsafe
        session["pre_feedback_score"] = base_score

        log_event("nbs_decision", topic=case["topic"], qid=case["id"], correct=int(correct), score=base_score)
        return redirect(url_for("case_feedback"))

    options = case["nbs"]["options"]
    return render_template_string("""
    <html><head><title>Next Best Step</title><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body{font-family:Inter,Arial,system-ui;margin:0;padding:24px;background:#0f172a;color:#e2e8f0}
      .wrap{max-width:760px;margin:0 auto}
      .card{background:#111827;border:1px solid #1f2937;border-radius:14px;padding:16px;margin:10px 0}
      .opt{display:block;margin:8px 0;padding:10px 12px;border-radius:10px;border:1px solid #243047;background:#0b1220}
      .btn{padding:12px 16px;border-radius:12px;border:none;color:white;background:linear-gradient(135deg,#2563eb,#1d4ed8);font-weight:700}
    </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <h2>{{case['nbs']['prompt']}}</h2>
          <form method="post">
            {% for o in options %}
              <label class="opt">
                <input type="radio" name="choice" value="{{o['id']}}" required> {{o['id']}}) {{o['text']}}
              </label>
            {% endfor %}
            <button class="btn" style="margin-top:10px">Submit Decision</button>
          </form>
        </div>
      </div>
    </body></html>
    """, case=case, options=options)

# ======================= FEEDBACK =======================
@app.route("/feedback", methods=["GET","POST"])
def case_feedback():
    S = _state()
    case = _get_case()
    if not case: return redirect(url_for("home"))

    if request.method == "POST":
        reflection = request.form.get("reflection","").strip()
        reflection_pts = 5 if len(reflection) >= 8 else 0
        base = int(session.get("pre_feedback_score", 0))
        total_score = base + reflection_pts

        # Update XP and streak/cooldown
        S["xp"] += total_score
        # lock the daily run
        S["cycle_lock_until"] = now_ts() + COOLDOWN_SECONDS
        S["last_completed_at"] = now_ts()
        # streak logic: if last was > cooldown ago, increment streak
        last = S.get("last_completed_at", 0)
        if (now_ts() - last) >= (COOLDOWN_SECONDS - 60):  # small forgiveness window
            S["streak"] += 1
        # store recent case id to avoid immediate repeats
        rc = S.get("recent_case_ids", [])
        rc.append(case["id"])
        S["recent_case_ids"] = rc[-10:]

        # log and finish
        session["final_score"] = total_score
        session["final_percent"] = min(100, int((total_score/100)*100))
        log_event("feedback_done", topic=case["topic"], qid=case["id"], score=total_score, percent=session["final_percent"])
        return redirect(url_for("done"))

    correct_id = next((o["id"] for o in case["nbs"]["options"] if o.get("correct")), None)
    choice = session.get("nbs_choice")
    correct = bool(session.get("nbs_correct"))
    unsafe = bool(session.get("unsafe"))
    base = int(session.get("pre_feedback_score", 0))

    # Build pitfalls list: show generic for wrong options
    pitfalls = case.get("pitfalls", [])
    takeaways = case.get("takeaways", [])
    ref = case.get("anz_ref","")

    return render_template_string("""
    <html><head><title>Feedback</title><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body{font-family:Inter,Arial,system-ui;margin:0;padding:24px;background:#f8fafc;color:#0f172a}
      .wrap{max-width:820px;margin:0 auto}
      .card{background:white;border:1px solid #e5e7eb;border-radius:14px;padding:18px;margin:10px 0}
      .badge{display:inline-block;padding:6px 10px;border-radius:999px;background:#dbeafe;color:#1e40af;font-weight:700;margin-right:8px}
      .ok{color:#065f46}
      .err{color:#991b1b}
      .pill{display:inline-block;background:#f1f5f9;color:#0f172a;padding:6px 10px;border-radius:999px;margin:4px 6px 0 0}
      .btn{padding:12px 16px;border-radius:12px;border:none;color:white;background:linear-gradient(135deg,#22c55e,#16a34a);font-weight:700}
      .subbtn{padding:10px 14px;border-radius:10px;border:none;background:#e2e8f0}
      ul{margin-top:6px}
    </style>
    </head>
    <body>
      <div class="wrap">
        <div class="card">
          <span class="badge">{{ "✅ Correct" if correct else "❌ Not quite" }}</span>
          {% if unsafe and not correct %}<span class="badge" style="background:#fee2e2;color:#991b1b">Safety risk</span>{% endif %}
          <h2>{{case['title']}}</h2>
          <p><b>{{case['topic']}}</b> • {{case['level']}}</p>
          <div class="card" style="background:#f8fafc">
            <h3 style="margin:0 0 6px">Instant Rationale</h3>
            <div>{{case['rationale_html']|safe}}</div>
            <p style="margin:6px 0 0;color:#334155"><i>{{case['anz_ref']}}</i></p>
          </div>

          <div class="card">
            <h3 style="margin:0 0 6px">Why the other choices are wrong</h3>
            <ul>
              {% for p in pitfalls %}<li>{{p}}</li>{% endfor %}
            </ul>
          </div>

          <div class="card">
            <h3 style="margin:0 0 6px">Takeaways (commit these)</h3>
            <ul>
              {% for t in takeaways %}<li>{{t}}</li>{% endfor %}
            </ul>
          </div>

          <form method="post" class="card">
            <h3 style="margin:0 0 6px">Micro-reflection (≤80 chars)</h3>
            <input name="reflection" maxlength="80" placeholder="What will you do differently next time?" style="width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:10px">
            <div style="margin-top:10px;display:flex;gap:10px;align-items:center">
              <button class="btn">Finish & Score</button>
              <span>Base score: <b>{{base}}</b> / 100</span>
            </div>
          </form>
        </div>
      </div>
    </body></html>
    """, case=case, correct=correct, unsafe=unsafe, base=base)

# ======================= DONE / SUMMARY =======================
@app.route("/done")
def done():
    S = _state()
    case = _get_case()
    if not case: return redirect(url_for("home"))

    total = 100
    score = int(session.get("final_score", 0))
    percent = int((score/total)*100)
    S["today_case_id"] = None

    # Friendly finisher message
    if percent == 100:
        message = "🌟 Perfect! Clinical-grade decision making."
    elif percent >= 80:
        message = "🔥 Strong work! Safer and faster each day."
    elif percent >= 50:
        message = "💡 Nice reps — keep compounding the habit."
    else:
        message = "🌱 You showed up — tomorrow will be sharper."

    log_event("case_done", topic=case["topic"], qid=case["id"], score=score, total=total, percent=percent)
    h_left, m_left = human_time_left(S)

    return render_template_string("""
    <html><head><title>Complete</title><meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      body{font-family:Inter,Arial,system-ui;margin:0;padding:60px 24px;background:radial-gradient(900px 600px at 80% 0%, #bbf7d0 0%, #a7f3d0 40%, #86efac 70%, #d9f99d 100%)}
      .wrap{max-width:760px;margin:0 auto;background:white;border-radius:16px;padding:18px;border:1px solid #e5e7eb}
      .chips{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
      .chip{background:#ecfeff;color:#0e7490;padding:8px 12px;border-radius:999px;font-weight:700}
      a.btn{display:inline-block;padding:12px 16px;border-radius:12px;color:white;text-decoration:none;background:linear-gradient(135deg,#2563eb,#1d4ed8)}
    </style>
    </head>
    <body>
      <div class="wrap">
        <h2>🎉 Case Complete: {{case['title']}}</h2>
        <p>{{message}}</p>
        <div class="chips">
          <div class="chip">Score: {{score}} / 100 ({{percent}}%)</div>
          <div class="chip">🔥 Streak: {{streak}}</div>
          <div class="chip">⭐ XP: {{xp}}</div>
        </div>
        <p>Daily run is locked. Next unlock in <b>{{h_left}}h {{m_left}}m</b>.</p>
        <a href="/" class="btn">Back Home</a>
      </div>
    </body></html>
    """, case=case, score=score, percent=percent, message=message,
       streak=S["streak"], xp=S["xp"], h_left=h_left, m_left=m_left)

# ======================= Analytics export =======================
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

# ======================= Voice helper (optional, client-side only) =======================
# Note: We use the browser Web Speech API in templates; no server endpoint needed.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=True)
