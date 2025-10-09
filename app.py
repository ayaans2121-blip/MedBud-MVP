# app.py — Enzo: Single-Case, Text-Only Clinical Judgment Trainer
# - Single high-fidelity case with variable flow (priority/management/investigations/NBS/free text)
# - Enzo 🐶 coach with graded hints (cost XP), fun UI (Tailwind CDN)
# - Confidence calibration on each decision (rewards well-calibrated judgment)
# - Badges: No Hints, Fast Finish, Well-Calibrated
# - Streak increments once per calendar day on first completion (NO cooldown lock)
# - Analytics retained (SQLite), optional invite gate via ACCESS_CODE env var
#
# Later: you can switch to cases.json (same schema) by uncommenting the JSON loader section.

from flask import Flask, render_template_string, request, redirect, url_for, session, make_response
import os, time, uuid, random, sqlite3, json
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
             int(correct) if correct is not None else None, None, None, "EnzoMVP",
             score, total, percent)
        )
        conn.commit(); conn.close()
    except Exception as e:
        print("analytics error:", e)

# ======================= Session bootstrap =======================
@app.before_request
def ensure_session():
    # Optional invite gate
    access_code = os.getenv("ACCESS_CODE")
    if access_code and request.endpoint not in ("gate","static"):
        if not request.cookies.get("access_ok"):
            return redirect(url_for("gate"))

    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    if "xp" not in session:
        session.update(dict(xp=0, streak=0, last_streak_day=None))

def today_str():
    return date.today().isoformat()

def maybe_increment_streak():
    t = today_str()
    if session.get("last_streak_day") != t:
        session["streak"] = session.get("streak", 0) + 1
        session["last_streak_day"] = t

# ======================= Single, High-Fidelity Case =======================
# Hint costs escalate slightly (keeps it playful + meaningful)
HINT_COSTS = [2, 3, 5]  # Nudge, Clue, Teaching

CASE = {
    "id": 1001,
    "systems": ["ED", "Cardio"],
    "title": "Tight chest in triage",
    "level": "Intern",
    "presenting": "A 45-year-old presents with 30 minutes of central, pressure-like chest pain, nausea, and diaphoresis.",
    "flow": ["presenting", "priority", "history", "exam", "investigations", "nbs", "free_text"],  # variable flow supported
    # Priority decision (safety-critical)
    "priority": {
        "prompt": "Immediate priority?",
        "options": [
            {"id":"A","text":"CT pulmonary angiogram to exclude PE first"},
            {"id":"B","text":"12-lead ECG within 10 minutes of arrival","correct":True,"safety_critical":True},
            {"id":"C","text":"Wait for troponin before ECG"},
            {"id":"D","text":"Discharge with outpatient stress test"}
        ],
        "hints": [
            "Nudge 🐶: Which action is both fast and likely to change immediate management?",
            "Clue 🐶: In red-flag chest pain, the bedside test that detects ischemia quickly is your priority.",
            "Teaching 🐶: Suspected ACS → obtain/read an ECG within 10 minutes of arrival."
        ],
        "state_if_correct": "ECG obtained promptly; vitals unchanged. ECG shows 1 mm ST depression in V4–V6.",
        "state_if_wrong": "Delay to ECG. Patient becomes more distressed; repeat vitals: HR 112, BP 138/84, SpO₂ 97%."
    },
    # Targeted history (choose up to 3)
    "history_tips": [
        "Ask radiation/exertion/relief",
        "Ask risk factors/family history",
        "Ask diaphoresis/SOB/red flags"
    ],
    "exam": "On arrival: HR 98, BP 138/84, RR 18, SpO₂ 98% RA. Chest clear, no murmur.",
    # Investigations MCQ
    "investigations": {
        "prompt": "Best next investigation to complement ECG and guide pathway?",
        "options": [
            {"id":"A","text":"Serial troponin at appropriate intervals","correct":True},
            {"id":"B","text":"D-dimer as first line"},
            {"id":"C","text":"CT brain"},
            {"id":"D","text":"ESR and bone profile only"}
        ],
        "hints": [
            "Nudge 🐶: What biomarker rises with myocardial injury but may be normal very early?",
            "Clue 🐶: Use it serially in pathway-based ACS risk stratification."
        ]
    },
    # Next Best Step MCQ
    "nbs": {
        "prompt": "Next best step now?",
        "options": [
            {"id":"A","text":"Start oral antibiotics"},
            {"id":"B","text":"Aspirin + pathway-based ACS risk stratification","correct":True},
            {"id":"C","text":"Immediate discharge with GP follow-up"},
            {"id":"D","text":"MRI heart urgently for everyone"}
        ],
        "hints": [
            "Nudge 🐶: Treat the dangerous thing first while refining risk.",
            "Clue 🐶: Antiplatelet + pathway-based risk tools are paired."
        ]
    },
    # Free text summary
    "free_text_prompt": "Free text (2–4 lines): likely diagnosis and immediate plan (include monitoring/escalation).",
    "free_text_keywords": ["ecg|twelve-lead","aspirin|antiplatelet","troponin","acs|nstemi|stemi","risk|pathway|stratification","monitor|telemetry|reassess"],
    # Feedback
    "feedback": {
        "rationale_html": "<p><b>ECG first (≤10 min)</b> for suspected ACS; complement with serial troponins and pathway-based risk stratification. Start antiplatelet when indicated. Do not delay ECG for labs/imaging.</p>",
        "takeaways": [
            "Red-flag chest pain → ECG within 10 minutes.",
            "Use serial troponin and ACS pathways; treat as ACS until ruled out.",
            "Prioritise time-critical actions before downstream imaging."
        ],
        "anz_ref": "Heart Foundation Australia / ACS pathways: early ECG and pathway-based assessment."
    }
}

# --- (Optional) Switch to external cases.json later (same schema) ---
# try:
#     with open(os.path.join(os.path.dirname(__file__), "cases.json"), "r", encoding="utf-8") as f:
#         CASE = json.load(f)
# except Exception as e:
#     print("cases.json not loaded (using built-in case):", e)

# ======================= Utility: scoring + calibration =======================
def calibration_points(correct: bool, confidence_pct: int) -> int:
    """
    Reward accurate confidence, penalize miscalibration.
    0..10 points each decision:
      - if correct: points ~ confidence (higher conf → more points)
      - if wrong: points ~ 100 - confidence (lower conf when wrong → more points)
    """
    c = max(0, min(100, int(confidence_pct)))
    return c // 10 if correct else (100 - c) // 10

def keyword_points(text: str, keywords: list[str]) -> int:
    """
    Free-text scoring by keywords/groups. +4 per group hit, cap at 24.
    Each item can be 'kw1|kw2' to allow synonyms.
    """
    if not text: return 0
    t = text.lower()
    score = 0
    for group in keywords:
        if any(k.strip() in t for k in group.split("|")):
            score += 4
    return min(24, score)

# ======================= UI templates =======================
BASE_HEAD = """
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.tailwindcss.com"></script>
"""

ENZO_BADGE = """
<span class="inline-flex items-center gap-2 rounded-full px-3 py-1 bg-white/20">
  <span class="text-2xl">🐶</span><span class="font-bold">Enzo</span>
</span>
"""

HOME_HTML = f"""
<!doctype html><html><head>{BASE_HEAD}<title>Enzo — Judgment Gym</title></head>
<body class="min-h-screen bg-gradient-to-br from-sky-500 via-indigo-500 to-emerald-500 text-white flex items-center justify-center p-4">
  <div class="w-full max-w-3xl bg-white/15 backdrop-blur-md rounded-2xl p-6 shadow-xl">
    <div class="flex items-center justify-between">
      <h1 class="text-3xl font-extrabold">Enzo — Clinical Judgment Gym</h1>
      {ENZO_BADGE}
    </div>
    <p class="opacity-90 mt-1">Short, realistic reps that train <b>what you do next</b>. Text-only. Calibrate confidence. Get coached.</p>

    <div class="flex flex-wrap gap-2 my-4">
      <span class="px-3 py-1 rounded-full bg-white/20">🔥 Streak: {{streak}}</span>
      <span class="px-3 py-1 rounded-full bg-white/20">⭐ XP: {{xp}}</span>
    </div>

    <div class="grid gap-3 sm:grid-cols-2">
      <div class="bg-white/10 rounded-xl p-4">
        <h3 class="font-bold">Today’s Case</h3>
        <p class="opacity-90">ACS-style chest pain in ED (Intern level). Variable flow, hints, confidence calibration.</p>
      </div>
      <div class="bg-white/10 rounded-xl p-4">
        <h3 class="font-bold">How scoring works</h3>
        <ul class="list-disc ml-5 opacity-90">
          <li>Priority, Investigations, NBS (MCQ + confidence)</li>
          <li>Free-text summary (keywords)</li>
          <li>Calibration rewards, hint costs, speed bonus</li>
        </ul>
      </div>
    </div>

    <form method="post" action="{{{{ url_for('start_case') }}}}" class="mt-4">
      <button class="px-5 py-3 rounded-xl font-bold bg-emerald-500 hover:bg-emerald-600">Start Case</button>
    </form>

    <p class="mt-3 text-sm opacity-90">No cooldowns. Streak increases once per calendar day on your first completion.</p>
  </div>
</body></html>
"""

CASE_SHELL = f"""
<!doctype html><html><head>{BASE_HEAD}<title>{{{{title}}}}</title></head>
<body class="min-h-screen bg-slate-900 text-slate-100 p-4">
  <div class="max-w-3xl mx-auto">
    <div class="flex items-center justify-between mb-3">
      <h1 class="text-2xl font-extrabold">{{{{title}}}}</h1>
      {ENZO_BADGE}
    </div>
    <div class="bg-slate-800/70 rounded-xl p-4 mb-3">
      <div class="text-sm">Stage {{{{stage_num}}}} / {{{{stage_total}}}}</div>
      <div class="mt-1 text-slate-200 font-semibold">{{{{stage_label}}}}</div>
    </div>

    <form method="post" class="space-y-4">
      {{{{body|safe}}}}
      <div class="flex gap-2">
        <a href="{{{{ url_for('home') }}}}" class="px-4 py-2 rounded-lg bg-slate-700">Quit</a>
        <button name="action" value="continue" class="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 font-bold">Continue</button>
      </div>
    </form>
  </div>
</body></html>
"""

FEEDBACK_HTML = f"""
<!doctype html><html><head>{BASE_HEAD}<title>Feedback</title></head>
<body class="min-h-screen bg-gradient-to-br from-emerald-50 to-sky-50 text-slate-900 p-4">
  <div class="max-w-3xl mx-auto bg-white rounded-2xl shadow p-5">
    <div class="flex items-center justify-between">
      <h2 class="text-2xl font-extrabold">Case Feedback</h2>
      {ENZO_BADGE}
    </div>
    <div class="flex flex-wrap gap-2 my-3">
      <span class="px-3 py-1 rounded-full bg-emerald-100 text-emerald-900">Score: {{score}} / 100</span>
      <span class="px-3 py-1 rounded-full bg-indigo-100 text-indigo-900">🔥 Streak: {{streak}}</span>
      <span class="px-3 py-1 rounded-full bg-amber-100 text-amber-900">⭐ XP: {{xp}}</span>
    </div>

    <div class="grid gap-4">
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

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Your Calibration</h3>
        <p class="text-sm text-slate-700">We reward accurate confidence. High+correct or low+wrong wins; high+wrong loses.</p>
        <ul class="list-disc ml-6">
          <li>Priority: {{calib['priority']}} / 10</li>
          <li>Investigations: {{calib['investigations']}} / 10</li>
          <li>NBS: {{calib['nbs']}} / 10</li>
          <li><b>Avg:</b> {{calib_avg}} / 10</li>
        </ul>
      </div>

      <div class="bg-slate-50 border rounded-xl p-4">
        <h3 class="font-bold mb-1">Badges</h3>
        <div class="flex flex-wrap gap-2">
          {% for b in badges %}<span class="px-3 py-1 rounded-full bg-indigo-100 text-indigo-900 font-semibold">{{b}}</span>{% endfor %}
          {% if not badges %}<span class="text-slate-600">No badges this time—try a run with no hints, finish in under 8 min, and keep great calibration.</span>{% endif %}
        </div>
      </div>

      <form method="post" action="{{url_for('finish_feedback')}}">
        <button class="px-5 py-3 rounded-xl font-bold bg-indigo-600 text-white hover:bg-indigo-700">Finish</button>
        <a href="{{url_for('home')}}" class="ml-2 px-4 py-3 rounded-xl bg-slate-200">Back Home</a>
      </form>
    </div>
  </div>
</body></html>
"""

# ======================= Routes =======================
@app.route("/", methods=["GET"])
def home():
    return render_template_string(HOME_HTML, streak=session.get("streak",0), xp=session.get("xp",0))

@app.route("/start", methods=["POST"])
def start_case():
    # initialize case state
    session["case"] = {
        "id": CASE["id"],
        "flow": CASE["flow"][:],
        "stage_idx": 0,
        "score": 0,
        "start_ts": time.time(),
        "hints_used": {"priority":0, "investigations":0, "nbs":0},
        "decisions": {},      # e.g., {"priority": {"choice":"B","correct":True,"conf":80}}
        "free_text": "",
        "badges": [],
    }
    log_event("start_case", topic=",".join(CASE["systems"]), qid=CASE["id"])
    return redirect(url_for("stage"))

def _stage_names(key):
    return {
        "presenting":"Presenting Problem",
        "priority":"Immediate Priority Decision",
        "history":"Targeted History",
        "exam":"Focused Exam/Vitals",
        "investigations":"Investigations",
        "nbs":"Next Best Step",
        "free_text":"Free-text Summary"
    }.get(key, key)

def _render_stage(case_state):
    key = case_state["flow"][case_state["stage_idx"]]
    body = ""
    # Build stage body
    if key == "presenting":
        body = f"""
        <div class="bg-slate-800/60 rounded-xl p-4">
          <p class="text-lg">{CASE['presenting']}</p>
        </div>
        """
    elif key == "priority":
        data = CASE["priority"]
        opts_html = ""
        for o in data["options"]:
            opts_html += f"""
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value="{o['id']}" class="mr-2 accent-indigo-500"> {o['id']}) {o['text']}
            </label>"""
        # Hints
        used = case_state["hints_used"]["priority"]
        hint_block = _hint_block("priority", used, data["hints"])
        conf = case_state["decisions"].get("priority",{}).get("conf",50)
        body = f"""
        <p class="mb-2">{data['prompt']}</p>
        {opts_html}
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value="{conf}" name="confidence" class="w-full">
          <div class="text-sm mt-1 opacity-80">Be honest—calibration matters.</div>
        </div>
        {hint_block}
        """
    elif key == "history":
        chips = "".join([f"<label class='inline-flex items-center gap-2 bg-slate-800 rounded-full px-3 py-2 mr-2 mb-2'><input type='checkbox' name='hx' value='{h}' class='accent-emerald-400'><span>{h}</span></label>" for h in CASE.get("history_tips", [])])
        body = "<p class='mb-2'>Pick up to 3 targeted history questions (prioritise):</p>" + chips
    elif key == "exam":
        body = f"<p class='mb-2'>Focused exam & vitals:</p><div class='bg-slate-800 p-3 rounded-lg'>{CASE.get('exam','')}</div>"
        # If priority chosen, show evolving state snippet
        pr = case_state["decisions"].get("priority")
        if pr:
            evo = CASE["priority"]["state_if_correct"] if pr["correct"] else CASE["priority"]["state_if_wrong"]
            body += f"<div class='mt-3 bg-indigo-900/40 p-3 rounded-lg'><b>Update:</b> {evo}</div>"
    elif key == "investigations":
        inv = CASE.get("investigations")
        opts_html = ""
        for o in inv["options"]:
            opts_html += f"""
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value="{o['id']}" class="mr-2 accent-indigo-500"> {o['id']}) {o['text']}
            </label>"""
        used = case_state["hints_used"]["investigations"]
        hint_block = _hint_block("investigations", used, inv["hints"])
        conf = case_state["decisions"].get("investigations",{}).get("conf",50)
        body = f"""
        <p class="mb-2">{inv['prompt']}</p>
        {opts_html}
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value="{conf}" name="confidence" class="w-full">
        </div>
        {hint_block}
        """
    elif key == "nbs":
        nbs = CASE.get("nbs")
        opts_html = ""
        for o in nbs["options"]:
            opts_html += f"""
            <label class="block bg-slate-800 p-3 rounded-lg mb-2">
              <input required type="radio" name="choice" value="{o['id']}" class="mr-2 accent-indigo-500"> {o['id']}) {o['text']}
            </label>"""
        used = case_state["hints_used"]["nbs"]
        hint_block = _hint_block("nbs", used, nbs["hints"])
        conf = case_state["decisions"].get("nbs",{}).get("conf",50)
        body = f"""
        <p class="mb-2">{nbs['prompt']}</p>
        {opts_html}
        <div class="mt-3 p-3 bg-slate-800 rounded-lg">
          <label class="block font-semibold mb-1">Confidence (0–100%)</label>
          <input type="range" min="0" max="100" value="{conf}" name="confidence" class="w-full">
        </div>
        {hint_block}
        """
    elif key == "free_text":
        prev = case_state.get("free_text","")
        body = f"""
        <p class='mb-2'>{CASE.get('free_text_prompt','Free text: summary & plan')}</p>
        <textarea name="free_text" rows="4" class="w-full rounded-lg p-3 text-black" placeholder="2–4 lines...">{prev}</textarea>
        <div class="text-sm opacity-80 mt-1">Include: likely dx, immediate plan, monitoring/escalation.</div>
        """
    return key, body

def _hint_block(stage_key, used, hints):
    out = "<div class='mt-3 p-3 bg-indigo-950/40 rounded-lg'>"
    # already shown hints
    for i in range(used):
        out += f"<div class='text-indigo-200 mb-1'>💡 {hints[i]}</div>"
    # button to reveal next hint (if any)
    if used < len(hints):
        cost = HINT_COSTS[min(used, len(HINT_COSTS)-1)]
        out += f"""
        <button name="action" value="hint_{stage_key}" class="mt-2 px-3 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-700 font-bold">
          Get Hint (–{cost} XP)
        </button>
        """
    else:
        out += "<div class='text-indigo-300 opacity-80'>No more hints.</div>"
    out += "</div>"
    return out

@app.route("/stage", methods=["GET","POST"])
def stage():
    case_state = session.get("case")
    if not case_state: return redirect(url_for("home"))
    flow = case_state["flow"]

    # Handle POST: continue or hint
    if request.method == "POST":
        action = request.form.get("action","continue")
        key = flow[case_state["stage_idx"]]

        # Hints (don't advance stage)
        if action.startswith("hint_"):
            stage_key = action.split("hint_")[-1]
            if stage_key in case_state["hints_used"]:
                used = case_state["hints_used"][stage_key]
                max_hints = len(CASE[stage_key]["hints"])
                if used < max_hints:
                    # charge XP
                    cost = HINT_COSTS[min(used, len(HINT_COSTS)-1)]
                    session["xp"] = max(0, session.get("xp",0) - cost)
                    case_state["hints_used"][stage_key] += 1
            session["case"] = case_state
            return redirect(url_for("stage"))

        # Otherwise, process stage inputs and advance
        if key == "priority":
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            data = CASE["priority"]
            correct_id = next((o["id"] for o in data["options"] if o.get("correct")), None)
            correct = (choice == correct_id)
            # base scoring
            case_state["score"] += 20 if correct else 0
            # calibration
            case_state["score"] += calibration_points(correct, conf)
            # save decision
            case_state["decisions"]["priority"] = {"choice": choice, "correct": correct, "conf": conf}
            log_event("priority_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"])
        elif key == "history":
            chosen = request.form.getlist("hx")
            chosen = chosen[:3]
            case_state["decisions"]["history"] = {"chosen": chosen}
            case_state["score"] += min(12, 4*len(chosen))  # small reward for prioritisation
        elif key == "exam":
            # small participation points
            case_state["score"] += 4
        elif key == "investigations":
            inv = CASE.get("investigations")
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            corr = next((o["id"] for o in inv["options"] if o.get("correct")), None)
            correct = (choice == corr)
            case_state["score"] += 16 if correct else 0
            case_state["score"] += calibration_points(correct, conf)
            case_state["decisions"]["investigations"] = {"choice": choice, "correct": correct, "conf": conf}
            log_event("investigation_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"])
        elif key == "nbs":
            nbs = CASE.get("nbs")
            choice = request.form.get("choice")
            conf = int(request.form.get("confidence", 50))
            corr = next((o["id"] for o in nbs["options"] if o.get("correct")), None)
            correct = (choice == corr)
            case_state["score"] += 26 if correct else 0
            case_state["score"] += calibration_points(correct, conf)
            case_state["decisions"]["nbs"] = {"choice": choice, "correct": correct, "conf": conf}
            log_event("nbs_decision", topic=",".join(CASE["systems"]), qid=CASE["id"], correct=int(correct), score=case_state["score"])
        elif key == "free_text":
            ft = (request.form.get("free_text","") or "").strip()
            case_state["free_text"] = ft
            case_state["score"] += keyword_points(ft, CASE.get("free_text_keywords", []))

        # advance stage
        case_state["stage_idx"] += 1
        session["case"] = case_state
        if case_state["stage_idx"] >= len(flow):
            # compute speed bonus
            elapsed = time.time() - case_state["start_ts"]
            speed_bonus = 8 if elapsed <= 8*60 else (5 if elapsed <= 12*60 else 0)
            case_state["score"] += speed_bonus
            session["case"] = case_state
            return redirect(url_for("feedback"))

    # Render current stage
    stage_key, body = _render_stage(session["case"])
    return render_template_string(
        CASE_SHELL,
        title=f"{CASE['title']} • {CASE['level']} • {', '.join(CASE['systems'])}",
        stage_num=session["case"]["stage_idx"]+1,
        stage_total=len(session["case"]["flow"]),
        stage_label=_stage_names(stage_key),
        body=body
    )

@app.route("/feedback", methods=["GET"])
def feedback():
    case_state = session.get("case")
    if not case_state: return redirect(url_for("home"))
    score = min(100, case_state["score"])

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

    # Save into session for finish step
    session["last_run"] = {
        "score": score,
        "calib": calib,
        "calib_avg": calib_avg,
        "badges": badges
    }

    fb = CASE["feedback"]
    log_event("case_feedback", topic=",".join(CASE["systems"]), qid=CASE["id"], score=score, total=100, percent=score)

    return render_template_string(
        FEEDBACK_HTML,
        score=score,
        streak=session.get("streak",0),
        xp=session.get("xp",0),
        rationale=fb["rationale_html"],
        takeaways=fb["takeaways"],
        anz_ref=fb["anz_ref"],
        calib=calib,
        calib_avg=calib_avg,
        badges=badges
    )

@app.route("/finish", methods=["POST"])
def finish_feedback():
    # award XP = score minus hint penalties already applied via XP deductions, plus small streak bonus
    last = session.get("last_run", {"score":0})
    session["xp"] = session.get("xp",0) + int(last.get("score",0))
    maybe_increment_streak()
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
    return render_template_string(f"""
    <html><head>{BASE_HEAD}<title>Access</title></head>
    <body class="min-h-screen flex items-center justify-center bg-gradient-to-br from-sky-500 to-indigo-600 text-white">
      <form method="post" class="bg-white/15 backdrop-blur-md p-6 rounded-2xl">
        <div class="flex items-center justify-between mb-2">
          <h2 class="text-xl font-extrabold">Enter Invite Code</h2>
          {ENZO_BADGE}
        </div>
        <input name="code" class="text-black p-2 rounded-lg mr-2" placeholder="Access code">
        <button class="px-4 py-2 rounded-lg bg-emerald-500 font-bold">Enter</button>
        {{% if err %}}<div class="text-rose-200 mt-2">{{{{err}}}}</div>{{% endif %}}
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

# ======================= Local run (Render uses Gunicorn) ======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")), debug=True)
