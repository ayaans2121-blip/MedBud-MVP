from flask import Flask, render_template_string, request, redirect, url_for, session, make_response
import time, random, os, sqlite3, uuid
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
    # Optional invite-only gate
    access_code = os.getenv("ACCESS_CODE")
    if access_code:
        if request.endpoint not in ("gate", "static") and not request.cookies.get("access_ok"):
            return redirect(url_for("gate"))

    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    if "variant" not in session:
        session["variant"] = random.choice(["A", "B"])  # A=confetti, B=no confetti

    # Ensure a state bucket exists for this user
    _state()  # initialize if missing

@app.route("/gate", methods=["GET", "POST"])
def gate():
    access_code = os.getenv("ACCESS_CODE")
    if not access_code:
        return redirect(url_for("home"))
    error = None
    if request.method == "POST":
        if request.form.get("code", "").strip() == access_code:
            resp = make_response(redirect(url_for("home")))
            resp.set_cookie("access_ok", "1", max_age=60*60*24*60)  # 60 days
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

# ======================= Question Bank =======================
QUESTIONS = {
    "Anatomy": [
        {"q":"A patient can’t abduct the right eye when looking to the right. Which nerve is most likely affected?",
         "options":["Oculomotor (CN III)","Trochlear (CN IV)","Abducens (CN VI)","Optic (CN II)"],
         "answer":"Abducens (CN VI)",
         "explanation":"CN VI drives the lateral rectus — think **“LR6”**. If the eye can’t go out, the ‘tug boat’ lost its rope.",
         "difficulty":"easy"},
        {"q":"Sudden left facial droop and left arm weakness with aphasia. Which artery is most likely involved?",
         "options":["ACA","MCA","PCA","Basilar"],
         "answer":"MCA",
         "explanation":"MCA = **Face & upper limb + language** (dominant). Think **“MCA feeds the Mic-Chatter Areas.”**",
         "difficulty":"medium"},
        {"q":"Loss of pain/temperature on the left face with loss on the right body. This pattern localizes best to…",
         "options":["Medial medulla","Lateral medulla","Midbrain tectum","Cervical dorsal column"],
         "answer":"Lateral medulla",
         "explanation":"Lateral medulla (PICA) hits **ipsi face (spinal trigeminal)** + **contra body (spinothalamic)** — the **‘split hot/cold’** clue.",
         "difficulty":"hard"},
        {"q":"Pupil is ‘down & out’ with ptosis on the right. Which structure is compressed in an uncal herniation?",
         "options":["CN II","CN III","CN IV","CN VI"],
         "answer":"CN III",
         "explanation":"Uncal herniation blows CN III — **‘down & out’ + blown pupil**. Picture the **‘III-thread’ to the pupil being yanked**.",
         "difficulty":"medium"},
        {"q":"A stroke causes difficulty recognizing objects by touch despite intact primary sensation. Which lobe?",
         "options":["Frontal","Parietal","Temporal","Occipital"],
         "answer":"Parietal",
         "explanation":"Parietal integrates sensation (**stereognosis**). Think **‘Parietal = puzzle-piece placer’**.",
         "difficulty":"easy"}
    ],
    "Physiology": [
        {"q":"What primarily sets the neuron’s resting membrane potential near −70 mV?",
         "options":["Voltage-gated Na⁺ channels","K⁺ leak channels","Na⁺/Ca²⁺ exchanger","Cl⁻ channels"],
         "answer":"K⁺ leak channels",
         "explanation":"Like a **leaky K⁺ bucket**: K⁺ trickles out via leak channels, dragging Vm negative.",
         "difficulty":"easy"},
        {"q":"Why does myelination speed conduction?",
         "options":["Lowers threshold","Increases axon diameter only","Enables saltatory conduction","Adds more Na⁺ channels everywhere"],
         "answer":"Enables saltatory conduction",
         "explanation":"Signals **hop node-to-node** (saltatory). Lily pads on a pond — fewer strokes, faster travel.",
         "difficulty":"easy"},
        {"q":"Which change most increases axonal conduction velocity?",
         "options":["↓ Axon diameter & ↓ myelination","↑ Axon diameter & ↑ myelination","↑ External Na⁺ only","↑ K⁺ leak only"],
         "answer":"↑ Axon diameter & ↑ myelination",
         "explanation":"Bigger cable + better insulation → **↑ length constant, ↓ time constant**. Like upgrading to **fat, insulated fiber**.",
         "difficulty":"medium"},
        {"q":"At the NMJ, acetylcholine triggers depolarization primarily via which receptor?",
         "options":["Nicotinic ionotropic receptor","Muscarinic M2","GABA-A","AMPA"],
         "answer":"Nicotinic ionotropic receptor",
         "explanation":"Nicotinic = **fast ion channel** → Na⁺ in, K⁺ out. Think **‘Nico is quick’**.",
         "difficulty":"easy"},
        {"q":"During the absolute refractory period, why can’t the neuron fire again?",
         "options":["K⁺ channels are closed","Na⁺ channels are inactivated","Cl⁻ channels are open","Membrane is hyperexcitable"],
         "answer":"Na⁺ channels are inactivated",
         "explanation":"Na⁺ gates are **locked & resting** — no new spike until they reset. Like a camera flash recharging.",
         "difficulty":"medium"}
    ],
    "Pathophysiology": [
        {"q":"A young woman has neurologic deficits that ‘flare with heat’ (Uhthoff). What’s the core lesion in MS?",
         "options":["Axonal transection only","CNS demyelination","PNS demyelination","Synaptic vesicle defect"],
         "answer":"CNS demyelination",
         "explanation":"MS = **CNS myelin peeling** — signals stall, worse with heat (like **wifi dropping in a sauna**).",
         "difficulty":"easy"},
        {"q":"Ptosis, diplopia worse at day’s end, improves with rest. Antibodies target…",
         "options":["ACh receptor","Voltage-gated Ca²⁺ channel","MuSK","Dopamine receptor"],
         "answer":"ACh receptor",
         "explanation":"Myasthenia gravis = **AChR blockade** → fatigable weakness. Locks at the NMJ get **gummed up**.",
         "difficulty":"medium"},
        {"q":"Nonfluent speech, good comprehension, impaired repetition: most consistent with…",
         "options":["Broca’s aphasia","Wernicke’s aphasia","Conduction aphasia","Global aphasia"],
         "answer":"Broca’s aphasia",
         "explanation":"Broca = **broken mouth** (production). Wernicke = **word salad** (comprehension).",
         "difficulty":"easy"},
        {"q":"A trauma patient develops a unilateral ‘blown pupil’ and contralateral hemiparesis. Likely mechanism?",
         "options":["Central herniation","Tonsillar herniation","Uncal herniation","Upward cerebellar herniation"],
         "answer":"Uncal herniation",
         "explanation":"Uncus compresses **CN III** (blown pupil) + **cerebral peduncle** (hemiparesis). Think **‘temporal lobe hook’** snagging brainstem.",
         "difficulty":"hard"},
        {"q":"Elderly patient with progressive memory loss and hippocampal atrophy. Which neurotransmitter is reduced?",
         "options":["Dopamine","Serotonin","Acetylcholine","Glutamate"],
         "answer":"Acetylcholine",
         "explanation":"Alzheimer’s → ↓ **ACh** (basal nucleus of Meynert). Memory needs **ACh ignition**.",
         "difficulty":"medium"}
    ]
}

CYCLE_TOPICS = ["Anatomy", "Physiology", "Pathophysiology"]
COOLDOWN_SECONDS = 20 * 60 * 60  # 20 hours

# ======================= Per-user state =======================
# In-memory map: session_id -> state dict
USERS = {}

def _blank_state():
    return {
        "xp": 0,
        "streak": 0,
        "topic": None,
        "day": 1,  # 1..5
        "review_queue": {t: [] for t in QUESTIONS.keys()},
        "recent_qs": {t: [] for t in QUESTIONS.keys()},
        "nudge_plan": {t: {"anchors": 0} for t in QUESTIONS.keys()},
        # daily cycle control (per user)
        "completed_topics": [],
        "topic_completed_at": {t: None for t in QUESTIONS.keys()},
        "today_sets": {t: [] for t in QUESTIONS.keys()},
        "last_cycle_completed_at": None,
        "cycle_lock_until": 0.0
    }

def _state():
    sid = session.get("sid")
    if not sid:
        # session will be created by before_request, but just in case
        session["sid"] = str(uuid.uuid4())
        sid = session["sid"]
    if sid not in USERS:
        USERS[sid] = _blank_state()
    return USERS[sid]

def now_ts(): return time.time()
def is_cycle_locked(S): return now_ts() < S.get("cycle_lock_until", 0.0)
def human_time_left(S):
    remaining = max(0, int(S.get("cycle_lock_until", 0.0) - now_ts()))
    h = remaining // 3600
    m = (remaining % 3600) // 60
    return h, m

def reset_cycle_if_expired(S):
    if not is_cycle_locked(S) and len(S["completed_topics"]) == len(CYCLE_TOPICS):
        S["completed_topics"] = []
        S["today_sets"] = {t: [] for t in QUESTIONS.keys()}
        S["topic_completed_at"] = {t: None for t in QUESTIONS.keys()}

# ======================= HOME =======================
@app.route("/", methods=["GET", "POST"])
def home():
    S = _state()
    reset_cycle_if_expired(S)

    if request.method == "POST":
        topic = request.form.get("topic")
        if is_cycle_locked(S):
            return redirect(url_for("home"))
        if topic in S["completed_topics"]:
            return redirect(url_for("view_topic", topic=topic))

        S["topic"] = topic

        # ---- Build today's personalized set (review -> anchors -> fresh) ----
        review_items = S["review_queue"].get(topic, [])[:]
        review_q_texts = {q["q"] for q in review_items}
        recent = set(S["recent_qs"].get(topic, []))

        pool = [q for q in QUESTIONS[topic] if q["q"] not in review_q_texts]
        easy_pool = [q for q in pool if q.get("difficulty") == "easy" and q["q"] not in recent]
        other_pool = [q for q in pool if q.get("difficulty") != "easy" and q["q"] not in recent]
        random.shuffle(easy_pool); random.shuffle(other_pool)

        anchors_needed = S["nudge_plan"][topic].get("anchors", 0)
        anchors = []
        for q in easy_pool[:anchors_needed]:
            anchors.append(dict(q, _from_review=False, _from_anchor=True))

        combined = [dict(r, _from_review=True, _from_anchor=False) for r in review_items]
        combined.extend(anchors)

        baseline = 5
        fresh_needed = max(0, baseline - len(combined))
        for f in other_pool[:fresh_needed]:
            combined.append(dict(f, _from_review=False, _from_anchor=False))

        if len(combined) < baseline:
            more_easy = easy_pool[anchors_needed: anchors_needed + (baseline - len(combined))]
            for e in more_easy:
                combined.append(dict(e, _from_review=False, _from_anchor=False))

        MAX_TOTAL = 10
        if len(combined) < MAX_TOTAL:
            remainder = other_pool[fresh_needed:] + easy_pool[anchors_needed + max(0, baseline - len(combined)):]
            for x in remainder[:(MAX_TOTAL - len(combined))]:
                combined.append(dict(x, _from_review=False, _from_anchor=False))

        if len(combined) < baseline:
            fallback = [q for q in QUESTIONS[topic] if q["q"] not in review_q_texts]
            random.shuffle(fallback)
            for y in fallback:
                if len(combined) >= baseline: break
                combined.append(dict(y, _from_review=False, _from_anchor=False))

        session["question_list"] = combined
        session["score"] = 0
        session["wrong_count"] = 0
        session["start_time"] = time.time()

        # Freeze view-only copy & consume today's review queue
        S["today_sets"][topic] = [dict(q) for q in combined]
        S["review_queue"][topic] = []

        log_event("start_session", topic=topic)
        return redirect(url_for("quiz", qid=0))

    locked = is_cycle_locked(S)
    h_left, m_left = human_time_left(S)
    completed = {t: (t in S["completed_topics"]) for t in CYCLE_TOPICS}

    return render_template_string("""
    <html>
    <head>
        <title>MedBud</title>
        <style>
            body { font-family: Arial, system-ui; text-align:center; margin:0; padding:46px 20px;
                   background: radial-gradient(1200px 600px at 20% 10%, #34d399 0%, #3b82f6 35%, #8b5cf6 70%, #0ea5e9 100%);
                   color:white; }
            h1 { font-size:44px; margin:6px 0 4px; text-shadow: 0 2px 12px rgba(0,0,0,0.25); }
            .sub { opacity:0.95; margin-bottom:20px; }
            .stats { display:flex; gap:12px; justify-content:center; margin:8px 0 18px; flex-wrap:wrap; }
            .chip { background: rgba(255,255,255,0.16); padding:9px 14px; border-radius:999px; font-weight:700; backdrop-filter: blur(4px); }
            .grid { max-width:620px; margin:0 auto; }
            .row { display:flex; gap:12px; align-items:center; justify-content:center; flex-wrap:wrap; margin:10px 0; }
            button, a.btn { padding:12px 18px; border:none; border-radius:14px; text-decoration:none; color:white; cursor:pointer; font-weight:700; }
            .start { background: linear-gradient(135deg,#22c55e,#16a34a); box-shadow: 0 10px 24px rgba(22,163,74,0.35); }
            .view { background: linear-gradient(135deg,#60a5fa,#2563eb); box-shadow: 0 10px 24px rgba(37,99,235,0.35); }
            .disabled { background: rgba(255,255,255,0.18); color: rgba(255,255,255,0.7); cursor:not-allowed; }
            .lock { margin:16px auto 6px; padding:10px 16px; background: rgba(0,0,0,0.25); border-radius:12px; display:inline-block; }
            .topic { min-width:180px; text-align:left; font-weight:800; }
            .done { opacity:0.9; }
        </style>
    </head>
    <body>
        <h1>🧠 MedBud</h1>
        <div class="sub">Complete <b>all three topics</b> once per day. After that, you’re done until the next window. (Day {{day}}/5)</div>
        <div class="stats">
            <div class="chip">🔥 Streak: {{streak}}</div>
            <div class="chip">⭐ XP: {{xp}}</div>
            <div class="chip">🧪 Variant: {{variant}}</div>
        </div>

        {% if locked %}
          <div class="lock">🔒 Daily complete. Next unlock in <b>{{h_left}}h {{m_left}}m</b>.</div>
        {% endif %}

        <div class="grid">
            {% for t in topics %}
              <div class="row">
                <div class="topic">{{ '🦴' if t=='Anatomy' else ('⚡' if t=='Physiology' else '🧬') }} <b>{{t}}</b></div>
                <form method="post" style="display:inline">
                    <input type="hidden" name="topic" value="{{t}}">
                    {% if locked or completed[t] %}
                      <button class="start disabled" disabled>Start</button>
                    {% else %}
                      <button class="start">Start</button>
                    {% endif %}
                </form>
                {% if completed[t] %}
                  <a class="btn view" href="{{ url_for('view_topic', topic=t) }}">View today’s set</a>
                  <span class="done">✓ completed</span>
                {% endif %}
              </div>
            {% endfor %}
        </div>
    </body>
    </html>
    """, streak=S["streak"], xp=S["xp"], day=S["day"], variant=session.get("variant"),
       locked=locked, h_left=h_left, m_left=m_left, topics=CYCLE_TOPICS, completed=completed)

# ======================= VIEW (read-only) =======================
@app.route("/view/<topic>")
def view_topic(topic):
    S = _state()
    reset_cycle_if_expired(S)
    if topic not in CYCLE_TOPICS:
        return redirect(url_for("home"))
    items = S["today_sets"].get(topic, [])
    return render_template_string("""
    <html>
    <head>
      <title>View – {{topic}}</title>
      <style>
        body{font-family:Arial,system-ui;margin:0;padding:36px 16px;background:linear-gradient(120deg,#f0f9ff,#eef2ff)}
        h2{text-align:center}
        .card{max-width:720px;margin:12px auto;background:white;border-radius:14px;padding:16px 18px;box-shadow:0 8px 20px rgba(0,0,0,0.08)}
        .q{font-weight:800;margin-bottom:8px}
        .a{margin-left:12px;color:#065f46}
        .tag{font-size:12px;color:#6b7280}
      </style>
    </head>
    <body>
      <h2>📖 Today’s set – {{topic}}</h2>
      {% if not items %}
        <div class="card">No set saved for today yet.</div>
      {% endif %}
      {% for it in items %}
        <div class="card">
          <div class="q">{{ loop.index }}. {{ it['q'] }}</div>
          <div class="a">✅ Correct answer: <b>{{ it['answer'] }}</b></div>
          <div class="a">💡 {{ it['explanation']|safe }}</div>
          <div class="tag">
            {% if it.get('_from_review') %}🔁 from review &nbsp;{% endif %}
            {% if it.get('_from_anchor') %}🧩 anchor &nbsp;{% endif %}
            difficulty: {{ it.get('difficulty','n/a') }}
          </div>
        </div>
      {% endfor %}
      <div style="text-align:center;margin-top:18px;"><a href="/">Back</a></div>
    </body>
    </html>
    """, topic=topic, items=items)

# ======================= QUIZ =======================
@app.route("/quiz/<int:qid>", methods=["GET", "POST"])
def quiz(qid: int):
    S = _state()
    topic = S.get("topic")
    if topic is None:
        return redirect(url_for("home"))

    if topic in S["completed_topics"] or is_cycle_locked(S):
        return redirect(url_for("view_topic", topic=topic))

    questions = session.get("question_list")
    if not questions:
        pool = QUESTIONS[topic][:]
        random.shuffle(pool)
        session["question_list"] = [dict(q, _from_review=False, _from_anchor=False) for q in pool[:5]]
        questions = session["question_list"]

    if qid >= len(questions):
        return redirect(url_for("done"))

    variant = session.get("variant", "A")

    if request.method == "POST":
        selected = request.form.get("answer")
        current = questions[qid]
        correct_answer = current["answer"]
        fast = (time.time() - session.get("start_time", time.time())) < 8

        if selected == correct_answer:
            session["score"] = session.get("score", 0) + 1
            base = "⚡ Quick thinker!" if fast else "🎉 Brilliant!"
            if current.get("_from_review"):
                feedback = f"{base} <br><b>{correct_answer}</b> is correct. 🔁 Redemption!"
            elif current.get("_from_anchor"):
                feedback = f"{base} <br><b>{correct_answer}</b> is correct. 🧩 Confidence anchor cleared!"
            else:
                feedback = f"{base} <br><b>{correct_answer}</b> is correct."
            confetti = (variant == "A")
            log_event("answer", topic=topic, qid=qid, correct=1,
                      from_review=current.get("_from_review", False),
                      from_anchor=current.get("_from_anchor", False))
        else:
            session["wrong_count"] = session.get("wrong_count", 0) + 1
            qtext = current["q"]
            if qtext not in {q["q"] for q in S["review_queue"][topic]}:
                clean = {k: v for k, v in current.items() if not k.startswith("_")}
                S["review_queue"][topic].append(clean)

            prefix = "🔁 You missed this yesterday — great to reinforce it!<br>" if current.get("_from_review") \
                     else "Keep going — good reps build recall!<br>"
            feedback = (
                f"{prefix}"
                f"❌ Not quite. Correct answer: <b>{correct_answer}</b>.<br>"
                f"💡 {current['explanation']}<br><br>"
                f"📌 We’ll give you more of this topic tomorrow!"
            )
            confetti = False
            log_event("answer", topic=topic, qid=qid, correct=0,
                      from_review=current.get("_from_review", False),
                      from_anchor=current.get("_from_anchor", False))

        session["start_time"] = time.time()

        return render_template_string("""
        <html>
        <head>
            <title>Feedback</title>
            {% if confetti %}
            <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/dist/confetti.browser.min.js"></script>
            {% endif %}
            <style>
                body { font-family: Arial, system-ui; text-align:center; margin:0; padding-top:100px;
                       background: radial-gradient(900px 500px at 20% 0%, #fef3c7 0%, #ffe4e6 60%, #ffedd5 100%); }
                .msg { font-size:22px; margin:0 24px 24px; color:#1e3a8a; }
                .next { display:inline-block; margin-top:6px; padding:12px 28px; font-size:18px;
                        background: linear-gradient(135deg,#60A5FA,#2563EB); color:white; text-decoration:none;
                        border-radius:12px; box-shadow: 0 10px 22px rgba(37,99,235,0.25); transition: transform .1s; }
                .next:hover { transform: translateY(-1px) scale(1.02); }
            </style>
        </head>
        <body {% if confetti %}onload="(function(){var d=900,e=Date.now()+d;(function f(){window.confetti({particleCount:5,angle:60,spread:55,origin:{x:0}});window.confetti({particleCount:5,angle:120,spread:55,origin:{x:1}});if(Date.now()<e)requestAnimationFrame(f);}())})();"{% endif %}>
            <div class="msg">{{ feedback|safe }}</div>
            <a class="next" href="{{ url_for('quiz', qid=qid+1) }}">➡️ Next Question</a>
        </body>
        </html>
        """, feedback=feedback, qid=qid, confetti=confetti)

    question = questions[qid]
    progress = int(((qid) / len(questions)) * 100)

    return render_template_string("""
    <html>
    <head>
        <title>Quiz</title>
        <style>
            body { font-family: Arial, system-ui; text-align:center; margin:0; padding:48px 16px;
                   background: linear-gradient(120deg,#eff6ff 0%, #e0e7ff 45%, #f5f3ff 100%); }
            h2 { font-size:26px; color:#0f172a; margin: 0 0 10px; }
            .topic { color:#334155; margin-bottom: 10px; }
            .review { color:#7c3aed; margin-bottom: 10px; font-weight:700; }
            .anchor { color:#065f46; margin-bottom: 10px; font-weight:700; }
            .progress { margin: 18px auto 24px; width: 82%; height: 18px; background:#e5e7eb; border-radius: 12px; overflow:hidden; }
            .bar { height:100%; background: linear-gradient(90deg,#60A5FA,#2563EB); width: {{progress}}%; transition: width .5s; }
            form { max-width: 560px; margin: 0 auto; }
            .option { display:block; margin:14px auto; padding:14px 22px;
                      background:#DBEAFE; color:#1E3A8A; font-size:18px; border:none;
                      border-radius:14px; cursor:pointer; width:100%; max-width:480px; transition:.12s; }
            .option:hover { background: linear-gradient(135deg,#60A5FA,#2563EB); color:white; transform: translateY(-1px); }
        </style>
    </head>
    <body>
        <div class="topic">🎯 Topic: <b>{{ topic }}</b> • Day {{ day }}/5</div>
        {% if question.get('_from_review') %}<div class="review">🔁 From yesterday’s misses — let’s lock it in.</div>{% endif %}
        {% if question.get('_from_anchor') %}<div class="anchor">🧩 Confidence anchor — quick win to build momentum.</div>{% endif %}
        <div class="progress"><div class="bar"></div></div>
        <h2>{{ question['q'] }}</h2>
        <form method="post">
            {% for opt in question['options'] %}
                <button type="submit" name="answer" value="{{ opt }}" class="option">{{ opt }}</button>
            {% endfor %}
        </form>
    </body>
    </html>
    """, topic=topic, question=question, progress=progress, day=S["day"])

# ======================= DONE =======================
@app.route("/done")
def done():
    S = _state()
    topic = S.get("topic") or "Anatomy"
    questions = session.get("question_list", [])
    total = len(questions) if questions else len(QUESTIONS[topic])
    score = session.get("score", 0)
    wrong = session.get("wrong_count", 0)
    percent = int((score / max(total, 1)) * 100)

    if percent == 100:
        message = "🌟 Perfect! You owned this session."
        finisher_choices = [
            "That was clinical-grade recall — bank the feeling.",
            "Your neural pathways are firing like fiber optics — lock in that streak tomorrow.",
            "Treat yourself and come back for a streak booster 🔥"
        ]
    elif percent >= 80:
        message = "🔥 Strong work! You’re getting sharper every day."
        finisher_choices = [
            "One more day like this and you’ll unlock a personal best.",
            "Stack this win: same time tomorrow for habit momentum.",
            "Close to mastery — tiny reps, compounding gains."
        ]
    elif percent >= 50:
        message = "💡 Solid effort — consistency will compound."
        finisher_choices = [
            "Today’s reps = tomorrow’s recall. Keep the streak warm.",
            "Micro-wins add up — 5 minutes again tomorrow.",
            "You’re laying tracks — the train gets faster with each day."
        ]
    else:
        message = "🌱 Good reps. Tomorrow you’ll be even sharper."
        finisher_choices = [
            "Every expert started here — we’ll tilt questions to your topic tomorrow.",
            "Momentum beats perfection — 1% better next session.",
            "You showed up — that’s the hardest part. We’ll tune the difficulty."
        ]
    finisher = random.choice(finisher_choices)

    # XP
    S["xp"] += score

    # Mark topic complete for this user’s cycle
    if topic not in S["completed_topics"]:
        S["completed_topics"].append(topic)
        S["topic_completed_at"][topic] = now_ts()

    # Track recents to reduce repeats
    if questions:
        recent_list = S["recent_qs"].setdefault(topic, [])
        for q in questions:
            if q["q"] not in recent_list:
                recent_list.append(q["q"])
        S["recent_qs"][topic] = recent_list[-20:]

    # Compute nudging anchors for tomorrow
    anchors = 2 if (wrong >= 2 or percent < 60) else 0
    S["nudge_plan"][topic] = {"anchors": anchors}

    # If all topics done, set lock & streak rules (per user)
    all_done = len(S["completed_topics"]) == len(CYCLE_TOPICS)
    streak_incremented = False

    if all_done:
        now = now_ts()
        last_done = S.get("last_cycle_completed_at")
        if (last_done is None) or (now - last_done >= COOLDOWN_SECONDS):
            S["streak"] += 1
            S["day"] = min(S["day"] + 1, 5)
            S["last_cycle_completed_at"] = now
            S["cycle_lock_until"] = now + COOLDOWN_SECONDS
            streak_incremented = True
            log_event("cycle_completed", topic="ALL")
        else:
            S["cycle_lock_until"] = max(S.get("cycle_lock_until", 0.0), last_done + COOLDOWN_SECONDS)

    log_event("done", topic=topic, score=score, total=total, percent=percent)

    h_left, m_left = human_time_left(S)

    return render_template_string("""
    <html>
    <head>
        <title>Session Complete</title>
        <style>
            body { font-family: Arial, system-ui; text-align:center; margin:0; padding-top:80px;
                   background: radial-gradient(900px 600px at 80% 0%, #bbf7d0 0%, #a7f3d0 40%, #86efac 70%, #d9f99d 100%); }
            h1 { font-size:34px; color:#065f46; margin-bottom:8px; text-shadow: 0 1px 8px rgba(0,0,0,0.08); }
            .stats { font-size:20px; margin: 8px 0 12px; color:#064e3b; }
            .msg { font-size:22px; margin: 14px; color:#1e3a8a; font-weight:700; }
            .finisher { font-size:18px; margin: 6px 0 20px; color:#0f172a; opacity:0.9; }
            .chips { display:flex; gap:12px; justify-content:center; margin: 10px 0 26px; flex-wrap:wrap; }
            .chip { background: rgba(16,185,129,0.14); color:#065f46; padding:8px 14px; border-radius:999px; font-weight:700; }
            a { display:inline-block; padding:12px 28px; background: linear-gradient(135deg,#2563EB,#1d4ed8);
                color:white; border-radius:12px; text-decoration:none; font-size:18px; box-shadow: 0 10px 22px rgba(37,99,235,0.25); }
            a:hover { transform: translateY(-1px); }
            .lock { margin-top:10px; }
        </style>
    </head>
    <body>
        <h1>🎉 Challenge Complete!</h1>
        <div class="stats">Topic: <b>{{ topic }}</b> • Day {{ day }}/5</div>
        <div class="stats">You scored <b>{{ score }}/{{ total }}</b> ({{ percent }}%).</div>
        <div class="msg">{{ message }}</div>
        <div class="finisher">👉 {{ finisher }}</div>
        <div class="chips">
            <div class="chip">🔥 Streak: {{ streak }}</div>
            <div class="chip">⭐ XP: {{ xp }}</div>
            <div class="chip">✅ Done today: {{ done_count }}/3 topics</div>
            {% if all_done %}
              {% if streak_incremented %}
                <div class="chip">📅 Streak +1</div>
              {% else %}
                <div class="chip">⏳ Streak waits for cooldown</div>
              {% endif %}
            {% endif %}
        </div>

        {% if all_done %}
          <div class="lock">🔒 Daily complete. Next unlock in <b>{{ h_left }}h {{ m_left }}m</b>.</div>
        {% endif %}

        <a href="/">Back to Home</a>
    </body>
    </html>
    """, topic=topic, score=score, total=total, percent=percent,
       streak=S["streak"], xp=S["xp"], message=message, finisher=finisher,
       day=S["day"], done_count=len(S["completed_topics"]),
       all_done=all_done, streak_incremented=streak_incremented,
       h_left=h_left, m_left=m_left)

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

if __name__ == "__main__":
    # Local dev; on Render, gunicorn runs it
    app.run(host="0.0.0.0", port=8000, debug=True)
