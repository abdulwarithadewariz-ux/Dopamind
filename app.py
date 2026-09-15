import os, sqlite3, hashlib, random, json, time, secrets, re, base64, mimetypes
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, session, Response, g
from werkzeug.utils import secure_filename

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.permanent_session_lifetime = timedelta(days=30)  # used when "remember me" is checked
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15
DB_PATH = os.path.join(os.path.dirname(__file__), "dopamind.db")
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------- AI (Groq primary — free tier, OpenAI-compatible; OpenRouter fallback) ----------------
# Get a free Groq key at https://console.groq.com/keys — no card required.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GROQ_TEXT_MODEL = os.environ.get("DOPAMIND_AI_MODEL", "llama-3.3-70b-versatile").strip()
GROQ_VISION_MODEL = os.environ.get("DOPAMIND_VISION_MODEL", "llama-3.2-11b-vision-preview").strip()
GROQ_WHISPER_MODEL = os.environ.get("DOPAMIND_WHISPER_MODEL", "whisper-large-v3").strip()

# Get a free OpenRouter key at https://openrouter.ai/keys — no card required.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Free-tagged individual models rotate out with little notice — openrouter/free is a router
# that auto-picks whichever free model is currently available, so we don't chase renames.
OPENROUTER_TEXT_MODEL = os.environ.get("OPENROUTER_TEXT_MODEL", "openrouter/free").strip()
OPENROUTER_VISION_MODEL = os.environ.get("OPENROUTER_VISION_MODEL", "openrouter/free").strip()

PRO_TEST_EMAIL = os.environ.get("PRO_TEST_EMAIL", "").strip().lower()
OTP_EMAIL_ADDRESS = os.environ.get("OTP_EMAIL_ADDRESS", "").strip()
OTP_EMAIL_APP_PASSWORD = os.environ.get("OTP_EMAIL_APP_PASSWORD", "").strip()
DEV_SKIP_OTP = os.environ.get("DEV_SKIP_OTP", "0") == "1"  # off by default now — real OTP flow

PRO_PRICE_NAIRA = 10000

def get_ai_provider():
    """Returns a dict with a ready client + model names, preferring Groq, falling back to
    OpenRouter. Returns None if neither key is set."""
    if GROQ_API_KEY:
        from openai import OpenAI
        return {
            "client": OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL),
            "text_model": GROQ_TEXT_MODEL,
            "vision_model": GROQ_VISION_MODEL,
            "name": "groq",
        }
    if OPENROUTER_API_KEY:
        from openai import OpenAI
        return {
            "client": OpenAI(
                api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL,
                default_headers={"HTTP-Referer": "https://dopamind.app", "X-Title": "Dopamind"},
            ),
            "text_model": OPENROUTER_TEXT_MODEL,
            "vision_model": OPENROUTER_VISION_MODEL,
            "name": "openrouter",
        }
    return None

# ---------------- DB ----------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        name TEXT,
        password_hash TEXT,
        verified INTEGER DEFAULT 0,
        is_pro INTEGER DEFAULT 0,
        xp INTEGER DEFAULT 0,
        study_minutes INTEGER DEFAULT 0,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS otps (
        email TEXT PRIMARY KEY,
        otp TEXT,
        expires_at TEXT,
        attempts INTEGER DEFAULT 0,
        purpose TEXT DEFAULT 'register'
    );
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, name TEXT
    );
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, title TEXT, content TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, title TEXT, priority TEXT, deadline TEXT, status TEXT
    );
    CREATE TABLE IF NOT EXISTS flashcards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, front TEXT, back TEXT
    );
    CREATE TABLE IF NOT EXISTS quiz_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, question TEXT, options TEXT, correct TEXT
    );
    CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, invite_code TEXT UNIQUE, owner TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS group_members (
        group_id INTEGER, email TEXT,
        PRIMARY KEY (group_id, email)
    );
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, role TEXT, content TEXT, created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT, title TEXT, content TEXT, origin TEXT, created_at TEXT
    );
    """)
    conn.commit()
    # migrations for older DBs
    for stmt in [
        "ALTER TABLE otps ADD COLUMN purpose TEXT DEFAULT 'register'",
        "ALTER TABLE users ADD COLUMN failed_attempts INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN locked_until TEXT",
    ]:
        try:
            c.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()

def hash_pw(pw): return hashlib.sha256(pw.encode()).hexdigest()

def current_user():
    email = session.get("email")
    if not email:
        return None
    row = get_db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return row

def require_login():
    u = current_user()
    if not u:
        return None
    return u

# ---------------- Auth ----------------
@app.post("/api/register")
def register():
    data = request.json or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    pw = data.get("password") or ""
    confirm_pw = data.get("confirm_password")
    if not name or not email or not pw:
        return jsonify(error="All fields are required."), 400
    if len(pw) < 6:
        return jsonify(error="Password must be at least 6 characters."), 400
    if confirm_pw is not None and pw != confirm_pw:
        return jsonify(error="Passwords don't match."), 400
    db = get_db()
    existing = db.execute("SELECT email FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        return jsonify(error="This Gmail already has a Dopamind account. Please log in instead."), 409

    is_pro = 1 if (PRO_TEST_EMAIL and email == PRO_TEST_EMAIL) else 0
    verified = 1 if DEV_SKIP_OTP else 0
    db.execute(
        "INSERT INTO users (email,name,password_hash,verified,is_pro,xp,study_minutes,created_at) VALUES (?,?,?,?,?,0,0,?)",
        (email, name, hash_pw(pw), verified, is_pro, datetime.now().isoformat()),
    )
    db.commit()

    if DEV_SKIP_OTP:
        return jsonify(ok=True, skip_otp=True)

    otp = f"{random.randint(100000,999999)}"
    db.execute(
        "INSERT OR REPLACE INTO otps (email,otp,expires_at,attempts,purpose) VALUES (?,?,?,0,'register')",
        (email, otp, (datetime.now() + timedelta(minutes=5)).isoformat()),
    )
    db.commit()
    try:
        send_otp_email(email, otp, purpose="register")
    except Exception as e:
        return jsonify(ok=True, skip_otp=False, warning=f"Account created but OTP email failed to send: {e}. Set OTP_EMAIL_ADDRESS/OTP_EMAIL_APP_PASSWORD in .env.")
    return jsonify(ok=True, skip_otp=False)

@app.post("/api/resend-otp")
def resend_otp():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        return jsonify(error="This email doesn't exist. Please register."), 404
    if user["verified"]:
        return jsonify(error="This account is already verified."), 400
    otp = f"{random.randint(100000,999999)}"
    db.execute(
        "INSERT OR REPLACE INTO otps (email,otp,expires_at,attempts,purpose) VALUES (?,?,?,0,'register')",
        (email, otp, (datetime.now() + timedelta(minutes=5)).isoformat()),
    )
    db.commit()
    try:
        send_otp_email(email, otp, purpose="register")
    except Exception as e:
        return jsonify(error=f"Couldn't send email: {e}"), 500
    return jsonify(ok=True)

@app.post("/api/verify-otp")
def verify_otp():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("otp") or "").strip()
    db = get_db()
    row = db.execute("SELECT * FROM otps WHERE email=? AND purpose='register'", (email,)).fetchone()
    if not row:
        return jsonify(error="No pending verification."), 400
    if datetime.now() > datetime.fromisoformat(row["expires_at"]):
        return jsonify(error="Code expired. Request a new one."), 400
    if row["attempts"] >= 5:
        return jsonify(error="Too many attempts."), 400
    if code != row["otp"]:
        db.execute("UPDATE otps SET attempts=attempts+1 WHERE email=?", (email,))
        db.commit()
        return jsonify(error="Incorrect code."), 400
    db.execute("UPDATE users SET verified=1 WHERE email=?", (email,))
    db.execute("DELETE FROM otps WHERE email=?", (email,))
    db.commit()
    return jsonify(ok=True)

@app.post("/api/login")
def login():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    pw = data.get("password") or ""
    remember_me = bool(data.get("remember_me"))
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        return jsonify(error="This email doesn't exist. Please register."), 404
    if not user["verified"]:
        return jsonify(error="Please verify your email first."), 403

    locked_until = user["locked_until"]
    if locked_until and datetime.now() < datetime.fromisoformat(locked_until):
        mins_left = int((datetime.fromisoformat(locked_until) - datetime.now()).total_seconds() // 60) + 1
        return jsonify(error=f"Too many failed attempts. Try again in {mins_left} minute(s)."), 423

    if user["password_hash"] != hash_pw(pw):
        attempts = (user["failed_attempts"] or 0) + 1
        if attempts >= LOGIN_MAX_ATTEMPTS:
            lock_until = (datetime.now() + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)).isoformat()
            db.execute("UPDATE users SET failed_attempts=0, locked_until=? WHERE email=?", (lock_until, email))
            db.commit()
            return jsonify(error=f"Too many failed attempts. Account locked for {LOGIN_LOCKOUT_MINUTES} minutes."), 423
        db.execute("UPDATE users SET failed_attempts=? WHERE email=?", (attempts, email))
        db.commit()
        return jsonify(error="Incorrect password."), 401

    db.execute("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE email=?", (email,))
    db.commit()
    session.permanent = remember_me
    session["email"] = email
    return jsonify(ok=True, name=user["name"], is_pro=bool(user["is_pro"]))

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/api/me")
def me():
    u = current_user()
    if not u:
        return jsonify(logged_in=False)
    return jsonify(
        logged_in=True, name=u["name"], email=u["email"],
        is_pro=bool(u["is_pro"]), xp=u["xp"], study_minutes=u["study_minutes"],
        pro_price=PRO_PRICE_NAIRA,
    )

def send_otp_email(to_email, otp, purpose="register"):
    if not OTP_EMAIL_ADDRESS or not OTP_EMAIL_APP_PASSWORD:
        raise RuntimeError("Email sending not configured")
    import smtplib, ssl
    from email.mime.text import MIMEText
    if purpose == "reset":
        subject = "Reset your Dopamind password"
        body = f"Your Dopamind password reset code is: {otp}\nExpires in 5 minutes. If you didn't request this, ignore this email."
    else:
        subject = "Your Dopamind verification code"
        body = f"Your Dopamind verification code is: {otp}\nExpires in 5 minutes."
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = OTP_EMAIL_ADDRESS
    msg["To"] = to_email
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(OTP_EMAIL_ADDRESS, OTP_EMAIL_APP_PASSWORD)
        server.sendmail(OTP_EMAIL_ADDRESS, to_email, msg.as_string())

# ---------------- Forgot / Reset / Change password ----------------
@app.post("/api/password/forgot")
def forgot_password():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user:
        return jsonify(error="This email doesn't exist."), 404
    otp = f"{random.randint(100000,999999)}"
    db.execute(
        "INSERT OR REPLACE INTO otps (email,otp,expires_at,attempts,purpose) VALUES (?,?,?,0,'reset')",
        (email, otp, (datetime.now() + timedelta(minutes=5)).isoformat()),
    )
    db.commit()
    try:
        send_otp_email(email, otp, purpose="reset")
    except Exception as e:
        return jsonify(error=f"Couldn't send reset email: {e}"), 500
    return jsonify(ok=True)

@app.post("/api/password/reset")
def reset_password():
    data = request.json or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("otp") or "").strip()
    new_pw = data.get("new_password") or ""
    confirm_pw = data.get("confirm_password")
    if len(new_pw) < 6:
        return jsonify(error="Password must be at least 6 characters."), 400
    if confirm_pw is not None and new_pw != confirm_pw:
        return jsonify(error="Passwords don't match."), 400
    db = get_db()
    row = db.execute("SELECT * FROM otps WHERE email=? AND purpose='reset'", (email,)).fetchone()
    if not row:
        return jsonify(error="No pending reset request."), 400
    if datetime.now() > datetime.fromisoformat(row["expires_at"]):
        return jsonify(error="Code expired. Request a new one."), 400
    if row["attempts"] >= 5:
        return jsonify(error="Too many attempts."), 400
    if code != row["otp"]:
        db.execute("UPDATE otps SET attempts=attempts+1 WHERE email=?", (email,))
        db.commit()
        return jsonify(error="Incorrect code."), 400
    db.execute("UPDATE users SET password_hash=? WHERE email=?", (hash_pw(new_pw), email))
    db.execute("DELETE FROM otps WHERE email=?", (email,))
    db.commit()
    return jsonify(ok=True)

@app.post("/api/password/change")
def change_password():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    data = request.json or {}
    current_pw = data.get("current_password") or ""
    new_pw = data.get("new_password") or ""
    confirm_pw = data.get("confirm_password")
    if u["password_hash"] != hash_pw(current_pw):
        return jsonify(error="Current password is incorrect."), 401
    if len(new_pw) < 6:
        return jsonify(error="New password must be at least 6 characters."), 400
    if confirm_pw is not None and new_pw != confirm_pw:
        return jsonify(error="Passwords don't match."), 400
    db = get_db()
    db.execute("UPDATE users SET password_hash=? WHERE email=?", (hash_pw(new_pw), u["email"]))
    db.commit()
    return jsonify(ok=True)

@app.post("/api/account/delete")
def delete_account():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    data = request.json or {}
    pw = data.get("password") or ""
    if u["password_hash"] != hash_pw(pw):
        return jsonify(error="Password is incorrect."), 401
    db = get_db()
    email = u["email"]
    for table in ["notes", "tasks", "flashcards", "quiz_questions", "courses",
                  "sources", "chat_history"]:
        db.execute(f"DELETE FROM {table} WHERE owner=?", (email,))
    db.execute("DELETE FROM group_members WHERE email=?", (email,))
    db.execute("DELETE FROM groups WHERE owner=?", (email,))
    db.execute("DELETE FROM otps WHERE email=?", (email,))
    db.execute("DELETE FROM users WHERE email=?", (email,))
    db.commit()
    session.clear()
    return jsonify(ok=True)

# ---------------- Pro upgrade (stub, Paystack-ready) ----------------
@app.post("/api/upgrade/init")
def upgrade_init():
    u = require_login()
    if not u:
        return jsonify(error="Not logged in"), 401
    return jsonify(
        error="Payment isn't wired up yet. Add PAYSTACK_SECRET_KEY and implement "
              "transaction initialize + webhook verification before granting Pro."
    ), 501

# ---------------- Courses / Notes / Tasks / Flashcards / Quiz ----------------
def crud_list(table, owner, order="id DESC"):
    return get_db().execute(f"SELECT * FROM {table} WHERE owner=? ORDER BY {order}", (owner,)).fetchall()

@app.route("/api/courses", methods=["GET", "POST"])
def courses():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    db = get_db()
    if request.method == "POST":
        name = (request.json or {}).get("name", "").strip()
        if not name: return jsonify(error="Name required"), 400
        db.execute("INSERT INTO courses (owner,name) VALUES (?,?)", (u["email"], name))
        db.commit()
    rows = crud_list("courses", u["email"])
    return jsonify([dict(r) for r in rows])

@app.route("/api/notes", methods=["GET", "POST"])
def notes():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    db = get_db()
    if request.method == "POST":
        d = request.json or {}
        title = d.get("title", "").strip()
        content = d.get("content", "").strip()
        if not title: return jsonify(error="Title required"), 400
        db.execute("INSERT INTO notes (owner,title,content,created_at) VALUES (?,?,?,?)",
                   (u["email"], title, content, datetime.now().isoformat()))
        db.commit()
    rows = crud_list("notes", u["email"])
    return jsonify([dict(r) for r in rows])

@app.route("/api/tasks", methods=["GET", "POST"])
def tasks():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    db = get_db()
    if request.method == "POST":
        d = request.json or {}
        title = d.get("title", "").strip()
        if not title: return jsonify(error="Title required"), 400
        db.execute("INSERT INTO tasks (owner,title,priority,deadline,status) VALUES (?,?,?,?,?)",
                   (u["email"], title, d.get("priority") or "Medium",
                    d.get("deadline") or "No deadline", "Not started"))
        db.commit()
    rows = crud_list("tasks", u["email"], "id ASC")
    return jsonify([dict(r) for r in rows])

@app.post("/api/tasks/<int:task_id>/toggle")
def toggle_task(task_id):
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    db = get_db()
    row = db.execute("SELECT * FROM tasks WHERE id=? AND owner=?", (task_id, u["email"])).fetchone()
    if not row: return jsonify(error="Not found"), 404
    order = ["Not started", "In progress", "Completed"]
    new_status = order[(order.index(row["status"]) + 1) % 3]
    db.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, task_id))
    db.commit()
    return jsonify(ok=True, status=new_status)

@app.route("/api/flashcards", methods=["GET", "POST"])
def flashcards():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    db = get_db()
    if request.method == "POST":
        d = request.json or {}
        front, back = d.get("front", "").strip(), d.get("back", "").strip()
        if not front or not back: return jsonify(error="Front and back required"), 400
        db.execute("INSERT INTO flashcards (owner,front,back) VALUES (?,?,?)", (u["email"], front, back))
        db.commit()
    rows = crud_list("flashcards", u["email"], "id ASC")
    return jsonify([dict(r) for r in rows])

@app.route("/api/quiz", methods=["GET", "POST"])
def quiz():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    db = get_db()
    if request.method == "POST":
        d = request.json or {}
        q, opts, correct = d.get("question", "").strip(), d.get("options", []), d.get("correct", "").strip()
        if not q or len(opts) < 2 or correct not in opts:
            return jsonify(error="Need a question, 2+ options, correct must match an option"), 400
        db.execute("INSERT INTO quiz_questions (owner,question,options,correct) VALUES (?,?,?,?)",
                   (u["email"], q, json.dumps(opts), correct))
        db.commit()
    rows = crud_list("quiz_questions", u["email"], "id ASC")
    return jsonify([{**dict(r), "options": json.loads(r["options"])} for r in rows])

# ---------------- Timer / XP / Leaderboard ----------------
@app.post("/api/focus/complete")
def focus_complete():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    minutes = int((request.json or {}).get("minutes", 25))
    xp_gain = minutes * 2
    db = get_db()
    db.execute("UPDATE users SET study_minutes=study_minutes+?, xp=xp+? WHERE email=?",
               (minutes, xp_gain, u["email"]))
    db.commit()
    return jsonify(ok=True, xp_gain=xp_gain)

@app.get("/api/leaderboard")
def leaderboard():
    rows = get_db().execute(
        "SELECT name, study_minutes, xp FROM users ORDER BY study_minutes DESC LIMIT 20"
    ).fetchall()
    return jsonify([dict(r) for r in rows])

# ---------------- Study Groups ----------------
@app.post("/api/groups/create")
def create_group():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    name = (request.json or {}).get("name", "").strip()
    if not name: return jsonify(error="Name required"), 400
    code = secrets.token_hex(3).upper()
    db = get_db()
    db.execute("INSERT INTO groups (name,invite_code,owner,created_at) VALUES (?,?,?,?)",
               (name, code, u["email"], datetime.now().isoformat()))
    gid = db.execute("SELECT id FROM groups WHERE invite_code=?", (code,)).fetchone()["id"]
    db.execute("INSERT INTO group_members (group_id,email) VALUES (?,?)", (gid, u["email"]))
    db.commit()
    return jsonify(ok=True, invite_code=code, group_id=gid)

@app.post("/api/groups/join")
def join_group():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    code = (request.json or {}).get("invite_code", "").strip().upper()
    db = get_db()
    grp = db.execute("SELECT * FROM groups WHERE invite_code=?", (code,)).fetchone()
    if not grp: return jsonify(error="Invalid invite code"), 404
    db.execute("INSERT OR IGNORE INTO group_members (group_id,email) VALUES (?,?)", (grp["id"], u["email"]))
    db.commit()
    return jsonify(ok=True, group=dict(grp))

@app.get("/api/groups/mine")
def my_groups():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    db = get_db()
    rows = db.execute("""
        SELECT g.* FROM groups g JOIN group_members m ON g.id=m.group_id
        WHERE m.email=?
    """, (u["email"],)).fetchall()
    result = []
    for r in rows:
        members = db.execute("""
            SELECT u.name, u.study_minutes FROM group_members m
            JOIN users u ON u.email=m.email WHERE m.group_id=?
            ORDER BY u.study_minutes DESC
        """, (r["id"],)).fetchall()
        result.append({**dict(r), "members": [dict(m) for m in members]})
    return jsonify(result)

# ---------------- AI Chat (streaming, Groq) ----------------
SYSTEM_PROMPT = (
    "You are Dopamind, a sharp, warm, slightly playful AI study copilot inside the "
    "Dopamind web app. Help with courses, notes, study plans, quizzes, flashcards, "
    "and motivation. Be concise and use short paragraphs."
)

@app.post("/api/ai/chat")
def ai_chat():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    data = request.json or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(error="Message required"), 400

    db = get_db()
    db.execute("INSERT INTO chat_history (owner,role,content,created_at) VALUES (?,?,?,?)",
               (u["email"], "user", message, datetime.now().isoformat()))
    db.commit()

    history_rows = db.execute(
        "SELECT role, content FROM chat_history WHERE owner=? ORDER BY id DESC LIMIT 16",
        (u["email"],),
    ).fetchall()
    history = [{"role": r["role"], "content": r["content"]} for r in reversed(history_rows)]

    def generate():
        provider = get_ai_provider()
        if not provider:
            yield ("Dopamind's brain isn't plugged in yet. Get a free key at "
                   "console.groq.com/keys or openrouter.ai/keys, set GROQ_API_KEY or "
                   "OPENROUTER_API_KEY in your .env, then restart the server.")
            return
        try:
            stream = provider["client"].chat.completions.create(
                model=provider["text_model"],
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
                stream=True,
                max_tokens=800,
            )
            full = ""
            for chunk in stream:
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    full += delta
                    yield delta
            db2 = sqlite3.connect(DB_PATH)
            db2.execute("INSERT INTO chat_history (owner,role,content,created_at) VALUES (?,?,?,?)",
                        (u["email"], "assistant", full, datetime.now().isoformat()))
            db2.commit()
            db2.close()
        except Exception as e:
            yield f"\n\n[AI request failed: {e}]"

    return Response(generate(), mimetype="text/plain")

@app.get("/api/ai/history")
def ai_history():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    rows = get_db().execute(
        "SELECT role, content FROM chat_history WHERE owner=? ORDER BY id ASC", (u["email"],)
    ).fetchall()
    return jsonify([dict(r) for r in rows])

# ---------------- AI content generation: notes / flashcards / quiz ----------------
def ai_text(client, model, system, user_content, max_tokens=1200, json_mode=False):
    kwargs = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user_content}],
        max_tokens=max_tokens,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(**kwargs)
    return resp.choices[0].message.content

def get_source_or_topic_text(db, owner, d):
    """Shared helper: request body has either {"source_id": N} or {"topic": "..."}"""
    if d.get("source_id"):
        src = db.execute("SELECT * FROM sources WHERE id=? AND owner=?", (d["source_id"], owner)).fetchone()
        if not src:
            return None, ("Source not found", 404)
        return src["content"][:8000], None
    topic = (d.get("topic") or "").strip()
    if not topic:
        return None, ("Provide either source_id or topic", 400)
    return topic, None

@app.post("/api/notes/generate")
def generate_notes():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    provider = get_ai_provider()
    if not provider: return jsonify(error="AI not configured (missing GROQ_API_KEY or OPENROUTER_API_KEY)."), 503
    db = get_db()
    d = request.json or {}
    text, err = get_source_or_topic_text(db, u["email"], d)
    if err: return jsonify(error=err[0]), err[1]
    try:
        content = ai_text(
            provider["client"], provider["text_model"],
            "Write clear, well-organized study notes (headings, bullet points, key terms bolded "
            "with **). Cover the material thoroughly but concisely.",
            text,
        )
        title = d.get("title") or (f"Notes: {d.get('topic')}" if d.get("topic") else "Generated Notes")
        db.execute("INSERT INTO notes (owner,title,content,created_at) VALUES (?,?,?,?)",
                   (u["email"], title, content, datetime.now().isoformat()))
        db.commit()
        return jsonify(ok=True, title=title, content=content)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/api/flashcards/generate")
def generate_flashcards():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    provider = get_ai_provider()
    if not provider: return jsonify(error="AI not configured (missing GROQ_API_KEY or OPENROUTER_API_KEY)."), 503
    db = get_db()
    d = request.json or {}
    text, err = get_source_or_topic_text(db, u["email"], d)
    if err: return jsonify(error=err[0]), err[1]
    count = int(d.get("count", 10))
    try:
        raw = ai_text(
            provider["client"], provider["text_model"],
            f'Generate exactly {count} flashcards from the material. '
            'Respond ONLY with JSON: {"cards": [{"front": "...", "back": "..."}]}',
            text,
            json_mode=True,
        )
        parsed = json.loads(raw)
        cards = parsed.get("cards", [])
        for c in cards:
            db.execute("INSERT INTO flashcards (owner,front,back) VALUES (?,?,?)",
                       (u["email"], c.get("front", ""), c.get("back", "")))
        db.commit()
        return jsonify(ok=True, count=len(cards), cards=cards)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/api/quiz/generate")
def generate_quiz():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    provider = get_ai_provider()
    if not provider: return jsonify(error="AI not configured (missing GROQ_API_KEY or OPENROUTER_API_KEY)."), 503
    db = get_db()
    d = request.json or {}
    text, err = get_source_or_topic_text(db, u["email"], d)
    if err: return jsonify(error=err[0]), err[1]
    count = int(d.get("count", 5))
    try:
        raw = ai_text(
            provider["client"], provider["text_model"],
            f'Generate exactly {count} multiple-choice quiz questions from the material. '
            'Each has 4 options and one correct answer that matches an option exactly. '
            'Respond ONLY with JSON: {"questions": [{"question": "...", "options": ["...","...","...","..."], "correct": "..."}]}',
            text,
            json_mode=True,
        )
        parsed = json.loads(raw)
        questions = parsed.get("questions", [])
        for q in questions:
            opts = q.get("options", [])
            correct = q.get("correct", "")
            if len(opts) >= 2 and correct in opts:
                db.execute("INSERT INTO quiz_questions (owner,question,options,correct) VALUES (?,?,?,?)",
                           (u["email"], q.get("question", ""), json.dumps(opts), correct))
        db.commit()
        return jsonify(ok=True, count=len(questions), questions=questions)
    except Exception as e:
        return jsonify(error=str(e)), 500

# ---------------- Sources (Notebook-style) ----------------
@app.route("/api/sources", methods=["GET", "POST"])
def sources():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    db = get_db()
    if request.method == "POST":
        d = request.json or {}
        title, content = d.get("title", "").strip(), d.get("content", "").strip()
        if not title or not content:
            return jsonify(error="Title and content required"), 400
        db.execute("INSERT INTO sources (owner,title,content,origin,created_at) VALUES (?,?,?,?,?)",
                   (u["email"], title, content, "manual", datetime.now().isoformat()))
        db.commit()
    rows = crud_list("sources", u["email"])
    return jsonify([dict(r) for r in rows])

@app.post("/api/sources/<int:source_id>/study-guide")
def source_study_guide(source_id):
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    if not u["is_pro"]:
        return jsonify(error="Study guide generation is a Pro feature."), 403
    db = get_db()
    src = db.execute("SELECT * FROM sources WHERE id=? AND owner=?", (source_id, u["email"])).fetchone()
    if not src: return jsonify(error="Not found"), 404
    provider = get_ai_provider()
    if not provider:
        return jsonify(error="AI not configured (missing GROQ_API_KEY or OPENROUTER_API_KEY)."), 503
    try:
        content = ai_text(
            provider["client"], provider["text_model"],
            "Produce a clear, well-structured study guide from the given source text: "
            "key concepts, definitions, and a short quiz.",
            src["content"][:8000],
            max_tokens=1200,
        )
        return jsonify(ok=True, guide=content)
    except Exception as e:
        return jsonify(error=str(e)), 500

# ---------------- File upload: PDF / image ----------------
ALLOWED_EXT = {"pdf", "png", "jpg", "jpeg", "webp", "gif"}

def file_ext(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

@app.post("/api/upload")
def upload_file():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    if "file" not in request.files:
        return jsonify(error="No file in request (use multipart/form-data, field name 'file')"), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify(error="Empty filename"), 400
    ext = file_ext(f.filename)
    if ext not in ALLOWED_EXT:
        return jsonify(error=f"Unsupported file type .{ext}. Allowed: {', '.join(sorted(ALLOWED_EXT))}"), 400

    safe_name = f"{secrets.token_hex(6)}_{secure_filename(f.filename)}"
    save_path = os.path.join(UPLOAD_DIR, safe_name)
    f.save(save_path)

    db = get_db()
    extracted = ""
    origin = "pdf" if ext == "pdf" else "image"

    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return jsonify(error="PDF support needs a package. Run: pip install pypdf"), 501
        try:
            reader = PdfReader(save_path)
            extracted = "\n\n".join((page.extract_text() or "") for page in reader.pages)[:20000]
        except Exception as e:
            return jsonify(error=f"Couldn't read PDF: {e}"), 500
    else:
        provider = get_ai_provider()
        if not provider:
            return jsonify(error="Image text extraction needs AI configured (missing GROQ_API_KEY or OPENROUTER_API_KEY)."), 503
        try:
            with open(save_path, "rb") as img_f:
                b64 = base64.b64encode(img_f.read()).decode()
            mime = mimetypes.guess_type(save_path)[0] or "image/png"
            resp = provider["client"].chat.completions.create(
                model=provider["vision_model"],
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe all text and describe any diagrams/charts in this image, for study notes."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }],
                max_tokens=1000,
            )
            extracted = resp.choices[0].message.content
        except Exception as e:
            return jsonify(error=f"Couldn't read image: {e}"), 500

    title = secure_filename(f.filename)
    db.execute("INSERT INTO sources (owner,title,content,origin,created_at) VALUES (?,?,?,?,?)",
               (u["email"], title, extracted, origin, datetime.now().isoformat()))
    db.commit()
    source_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return jsonify(ok=True, source_id=source_id, title=title, preview=extracted[:500])

# ---------------- YouTube link import ----------------
def extract_youtube_id(url):
    patterns = [
        r"(?:v=|/videos/|embed/|youtu\.be/|/v/|/shorts/)([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

@app.post("/api/sources/youtube")
def import_youtube():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    url = ((request.json or {}).get("url") or "").strip()
    vid = extract_youtube_id(url)
    if not vid:
        return jsonify(error="Couldn't find a video ID in that URL."), 400
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return jsonify(error="YouTube import needs a package. Run: pip install youtube-transcript-api"), 501
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(vid)
        full_text = " ".join(seg["text"] for seg in transcript_list)[:20000]
    except Exception as e:
        return jsonify(error=f"Couldn't fetch transcript (captions may be disabled on this video): {e}"), 500

    db = get_db()
    title = f"YouTube: {vid}"
    db.execute("INSERT INTO sources (owner,title,content,origin,created_at) VALUES (?,?,?,?,?)",
               (u["email"], title, full_text, "youtube", datetime.now().isoformat()))
    db.commit()
    source_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return jsonify(ok=True, source_id=source_id, title=title, preview=full_text[:500])

# ---------------- Voice notes (record -> transcribe -> note) ----------------
@app.post("/api/voice-note")
def voice_note():
    u = require_login()
    if not u: return jsonify(error="Not logged in"), 401
    if "audio" not in request.files:
        return jsonify(error="No audio in request (multipart/form-data, field name 'audio')"), 400
    if not GROQ_API_KEY:
        return jsonify(error="Voice transcription needs a free Groq key specifically (OpenRouter doesn't do audio) — set GROQ_API_KEY in .env."), 503
    from openai import OpenAI
    client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
    f = request.files["audio"]
    safe_name = f"{secrets.token_hex(6)}_{secure_filename(f.filename or 'voice.webm')}"
    save_path = os.path.join(UPLOAD_DIR, safe_name)
    f.save(save_path)
    try:
        with open(save_path, "rb") as audio_f:
            transcript = client.audio.transcriptions.create(
                model=GROQ_WHISPER_MODEL,
                file=audio_f,
            )
        text = transcript.text
    except Exception as e:
        return jsonify(error=f"Transcription failed: {e}"), 500

    db = get_db()
    title = f"Voice note {datetime.now().strftime('%b %d, %H:%M')}"
    db.execute("INSERT INTO notes (owner,title,content,created_at) VALUES (?,?,?,?)",
               (u["email"], title, text, datetime.now().isoformat()))
    db.commit()
    return jsonify(ok=True, title=title, transcript=text)

# NOTE on live "voice call": a real-time spoken conversation (mic streaming in, AI voice
# streaming out) is a browser WebRTC + audio-streaming feature, not a single backend route —
# it needs its own frontend build and testing pass. Voice notes above (record, upload,
# transcribe into a note) are fully working; flag when you're ready to tackle live voice call
# and we'll scope that separately.

# ---------------- Frontend ----------------
@app.get("/")
def index():
    from flask import render_template
    return render_template("index.html", pro_price=PRO_PRICE_NAIRA)

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))  # Render (and similar hosts) assign this dynamically
    try:
        from waitress import serve
        print(f"Dopamind running on port {port}")
        serve(app, host="0.0.0.0", port=port)
    except ImportError:
        # waitress not installed yet — falls back to Flask's dev server (shows the warning)
        app.run(debug=False, host="0.0.0.0", port=port)
else:
    init_db()