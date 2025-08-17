import os
import base64
import time
import sqlite3
from datetime import timedelta
import zipfile
import smtplib
from email.message import EmailMessage
from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    send_from_directory,
    session,
    redirect,
    url_for,
)
from openai import OpenAI
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash


# =========================
# Config
# =========================
class BaseConfig:
    APP_NAME = "Architect 3D Home Modeler"

    # Secrets & API Keys
    SECRET_KEY = os.environ.get("SECRET_KEY", "supersecretkey")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
    OPENAI_IMAGE_SIZE = os.environ.get("OPENAI_IMAGE_SIZE", "1024x1024")

    # File uploads
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
    PLAN_FOLDER = os.path.join(UPLOAD_FOLDER, "plans")
    RENDER_FOLDER = os.path.join(UPLOAD_FOLDER, "renderings")
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", 25 * 1024 * 1024))  # 25MB
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "pdf"}

    # Email (SMTP)
    SMTP_HOST = os.environ.get("SMTP_HOST")
    SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USER = os.environ.get("SMTP_USER")
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
    MAIL_FROM = os.environ.get("MAIL_FROM")

    # Database
    DATABASE_PATH = os.environ.get("DATABASE_PATH", "architect.db")

    # Sessions / Cookies
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")

    # Flask
    JSON_SORT_KEYS = False
    TEMPLATES_AUTO_RELOAD = True


class DevelopmentConfig(BaseConfig):
    ENV = "development"
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class ProductionConfig(BaseConfig):
    ENV = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = True


# Pick config based on FLASK_ENV (default to Production)
if os.environ.get("FLASK_ENV", "production").lower() == "development":
    Config = DevelopmentConfig
else:
    Config = ProductionConfig


# =========================
# App & Clients
# =========================
app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = app.config["SECRET_KEY"]

# OpenAI client (reads env automatically but we also pass explicit key if provided)
if app.config.get("OPENAI_API_KEY"):
    client = OpenAI(api_key=app.config["OPENAI_API_KEY"])
else:
    client = OpenAI()

# Ensure uploads directories exist
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["PLAN_FOLDER"], exist_ok=True)
os.makedirs(app.config["RENDER_FOLDER"], exist_ok=True)
os.makedirs(os.path.join(app.config["UPLOAD_FOLDER"], "downloads"), exist_ok=True)

# Create a sample rendering file for testing
sample_file_path = os.path.join(app.config["UPLOAD_FOLDER"], "sample_rendering.txt")
if not os.path.exists(sample_file_path):
    with open(sample_file_path, "w") as f:
        f.write("This is a sample rendering placeholder.")


# =========================
# DB Init (Flask 3.x-safe)
# =========================
_db_initialized = False


def _db_connect():
    return sqlite3.connect(app.config["DATABASE_PATH"])


@app.before_request
def initialize_db_once():
    global _db_initialized
    if _db_initialized:
        return

    conn = _db_connect()
    c = conn.cursor()

    # Users table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
        """
    )

    # Renderings table
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS renderings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            liked INTEGER DEFAULT 0,
            favorited INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )

    # Room Options Catalog
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS room_options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_name TEXT NOT NULL,
            option_category TEXT NOT NULL,
            option_value TEXT NOT NULL
        )
        """
    )

    # Preload catalogs if empty (you can expand this list anytime)
    c.execute("SELECT COUNT(*) FROM room_options")
    if c.fetchone()[0] == 0:
        catalogs = {
            "Front Exterior": {
                "Siding Material": ["Brick", "Stucco", "Vinyl", "Wood", "Stone"],
                "Roof Style": ["Gable", "Hip", "Flat", "Shed", "Mansard"],
                "Window Trim Color": ["White", "Black", "Gray", "Brown", "Blue"],
                "Landscaping": ["Modern", "Tropical", "Desert", "Cottage", "Minimalist"],
                "Driveway Material": ["Concrete", "Pavers", "Asphalt", "Gravel", "Stamped Concrete"],
            },
            "Back Exterior": {
                "Siding Material": ["Brick", "Stucco", "Vinyl", "Wood", "Stone"],
                "Roof Style": ["Gable", "Hip", "Flat", "Shed", "Mansard"],
                "Swimming Pool": ["Infinity", "Lap", "Kidney", "Rectangular", "Freeform"],
                "Basketball Court": ["Half", "Full", "Indoor", "Outdoor", "Multi-sport"],
            },
            "Living Room": {
                "Flooring": ["Hardwood", "Carpet", "Tile", "Laminate", "Concrete"],
                "Wall Color": ["White", "Gray", "Beige", "Blue", "Green"],
                "Lighting": ["Chandelier", "Recessed", "Pendant", "Track", "Floor Lamp"],
                "Furniture Style": ["Modern", "Traditional", "Scandinavian", "Industrial", "Minimalist"],
            },
            "Kitchen": {
                "Flooring": ["Tile", "Hardwood", "Laminate", "Concrete", "Vinyl"],
                "Cabinet Styles": ["Shaker", "Flat-panel", "Inset", "Raised-panel", "Glass-front"],
                "Countertops": ["Granite", "Quartz", "Marble", "Concrete", "Butcher Block"],
            },
            "Home Office": {
                "Flooring": ["Hardwood", "Carpet", "Tile", "Laminate", "Concrete"],
                "Desk Style": ["Standing", "Executive", "Minimalist", "Floating", "Corner"],
            },
            "Primary Bedroom": {
                "Bed Style": ["Canopy", "Platform", "Four-poster", "Sleigh", "Storage"],
                "Furniture Style": ["Modern", "Traditional", "Rustic", "Industrial", "Minimalist"],
            },
            "Primary Bathroom": {
                "Vanity Style": ["Floating", "Double-sink", "Pedestal", "Wall-mounted", "Traditional"],
                "Shower or Tub": ["Walk-in Shower", "Clawfoot Tub", "Jacuzzi", "Combo", "Steam Shower"],
            },
            "Basement - Theater Room": {
                "Seating": ["Recliners", "Sofas", "Loveseats", "Beanbags", "Mixed"],
                "Sound System": ["Dolby", "Surround", "Stereo", "Smart", "Custom"],
            },
        }

        for room, categories in catalogs.items():
            for cat, values in categories.items():
                for val in values:
                    c.execute(
                        "INSERT INTO room_options (room_name, option_category, option_value) VALUES (?, ?, ?)",
                        (room, cat, val),
                    )

    # Insert a sample rendering for testing (assigned to user 1 if exists)
    c.execute("SELECT COUNT(*) FROM renderings")
    if c.fetchone()[0] == 0:
        c.execute(
            "INSERT INTO renderings (user_id, filename, liked, favorited) VALUES (?, ?, ?, ?)",
            (1, "sample_rendering.txt", 0, 0),
        )

    conn.commit()
    conn.close()
    _db_initialized = True


# =========================
# DB Helpers
# =========================

def db_query(query: str, params: tuple = (), one: bool = False):
    """Run a read-only query. Returns list of rows or single row if one=True."""
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(query, params)
        if one:
            return c.fetchone()
        return c.fetchall()
    finally:
        conn.close()


def db_execute(query: str, params: tuple = ()):  # returns lastrowid
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        return c.lastrowid
    finally:
        conn.close()


def db_get_user_id(username: str, password: str):
    row = db_query("SELECT id FROM users WHERE username=? AND password=?", (username, password), one=True)
    return row[0] if row else None


def db_add_rendering(user_id: int, filename: str):
    return db_execute(
        "INSERT INTO renderings (user_id, filename, liked, favorited) VALUES (?, ?, 0, 0)",
        (user_id, filename),
    )


def db_get_renderings(user_id: int):
    return db_query(
        "SELECT id, filename, liked, favorited FROM renderings WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    )


def db_update_renderings_like_favorite(user_id: int, ids: list[int], liked=None, favorited=None):
    if not ids:
        return 0
    sets = []
    params = []
    if liked is not None:
        sets.append("liked=?")
        params.append(int(bool(liked)))
    if favorited is not None:
        sets.append("favorited=?")
        params.append(int(bool(favorited)))
    if not sets:
        return 0
    placeholders = ",".join(["?"] * len(ids))
    params.extend([user_id, *ids])
    q = f"UPDATE renderings SET {', '.join(sets)} WHERE user_id=? AND id IN ({placeholders})"
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(q, tuple(params))
        conn.commit()
        return c.rowcount
    finally:
        conn.close()


def db_delete_renderings(user_id: int, ids: list[int]):
    if not ids:
        return 0
    placeholders = ",".join(["?"] * len(ids))
    conn = _db_connect()
    try:
        c = conn.cursor()
        c.execute(f"DELETE FROM renderings WHERE user_id=? AND id IN ({placeholders})", (user_id, *ids))
        conn.commit()
        return c.rowcount
    finally:
        conn.close()


def db_count_favorites(user_id: int) -> int:
    row = db_query("SELECT COUNT(*) FROM renderings WHERE user_id=? AND favorited=1", (user_id,), one=True)
    return int(row[0] if row else 0)


def db_get_catalog(room: str):
    rows = db_query("SELECT option_category, option_value FROM room_options WHERE room_name=?", (room,))
    catalog = {}
    for category, value in rows:
        catalog.setdefault(category, []).append(value)
    return catalog


def db_get_rooms():
    rows = db_query("SELECT DISTINCT room_name FROM room_options")
    return [r[0] for r in rows]


def db_get_filenames_for_ids(user_id: int, ids: list[int]):
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    rows = db_query(
        f"SELECT id, filename FROM renderings WHERE user_id=? AND id IN ({placeholders})",
        (user_id, *ids),
    )
    return rows


def db_get_liked_filenames_for_ids(user_id: int, ids: list[int]):
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    rows = db_query(
        f"SELECT id, filename FROM renderings WHERE user_id=? AND liked=1 AND id IN ({placeholders})",
        (user_id, *ids),
    )
    return rows


# =========================
# Auth Utilities
# =========================

def _user_by_username(username: str):
    return db_query("SELECT id, username, password FROM users WHERE username=?", (username,), one=True)


def create_user(username: str, password: str):
    """Create a new user with a hashed password. Returns user id or None if username exists."""
    hashed = generate_password_hash(password)
    try:
        uid = db_execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
        return uid
    except sqlite3.IntegrityError:
        return None


def verify_password(password: str, stored_hash: str) -> bool:
    """Support legacy plaintext rows by checking either PBKDF2 hash or raw match."""
    if not stored_hash:
        return False
    if stored_hash.startswith("pbkdf2:"):
        return check_password_hash(stored_hash, password)
    return stored_hash == password


def authenticate_user(username: str, password: str):
    row = _user_by_username(username)
    if not row:
        return None
    user_id, _uname, stored = row
    if verify_password(password, stored):
        # upgrade legacy plaintext to hashed on successful login
        if not stored.startswith("pbkdf2:"):
            db_execute("UPDATE users SET password=? WHERE id=?", (generate_password_hash(password), user_id))
        return user_id
    return None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" in session:
            return fn(*args, **kwargs)
        wants_json = request.path.startswith("/api/") or (
            request.accept_mimetypes["application/json"] >= request.accept_mimetypes["text/html"]
        )
        if wants_json:
            return jsonify({"error": "auth required"}), 401
        return redirect(url_for("login", next=request.path))
    return wrapper


# =========================
# Helpers
# =========================

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def _save_b64_image(b64_str: str, prefix: str = "img") -> str:
    """Save a base64 PNG to uploads/renderings and return relative path (relative to uploads)."""
    data = base64.b64decode(b64_str)
    ts = int(time.time() * 1000)
    rel_path = os.path.join("renderings", f"{prefix}_{ts}.png")
    abs_path = os.path.join(app.config["UPLOAD_FOLDER"], rel_path)
    with open(abs_path, "wb") as f:
        f.write(data)
    return rel_path


def _insert_rendering_record(user_id, filename):
    db_add_rendering(user_id, filename)


# =========================
# Email Helper
# =========================

# (Email helper moved to dedicated section above)

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body or "Renderings attached.")

    for p in abs_paths:
        if not os.path.exists(p):
            continue
        with open(p, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="image", subtype="png", filename=os.path.basename(p))

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)


# =========================
# OpenAI Image Generation
# =========================

DEFAULT_IMAGE_MODEL = app.config.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
DEFAULT_IMAGE_SIZE = app.config.get("OPENAI_IMAGE_SIZE", "1024x1024")

STYLE_SUFFIX = (
    " 8k, photorealistic, high dynamic range, global illumination, architectural visualization,"
    " physically based rendering, sharp focus."
)


def ai_generate_image(prompt: str, size: str | None = None) -> str:
    """Generate an image with OpenAI and return base64 string."""
    sz = size or DEFAULT_IMAGE_SIZE
    resp = client.images.generate(model=DEFAULT_IMAGE_MODEL, prompt=prompt, size=sz)
    return resp.data[0].b64_json


def build_exterior_prompts(description: str, plan_note: str = "", has_basement: bool = False):
    front = (
        f"Ultra-realistic architectural rendering of the FRONT exterior of a single-family home{plan_note}. "
        f"Design details: {description}. Wide-angle, golden-hour street-level view." + STYLE_SUFFIX
    )
    back = (
        f"Ultra-realistic architectural rendering of the BACK exterior of a single-family home{plan_note}. "
        f"Design details: {description}. Include backyard context such as patio/deck if suitable." + STYLE_SUFFIX
    )
    if has_basement:
        back += " If the brief suggests a walkout basement, subtly show grade change."
    return front, back


def ai_generate_exterior_pair(description: str, plan_note: str = "", size: str | None = None):
    has_basement = "basement" in (description or "").lower()
    front_prompt, back_prompt = build_exterior_prompts(description, plan_note, has_basement)
    b64_front = ai_generate_image(front_prompt, size=size)
    b64_back = ai_generate_image(back_prompt, size=size)
    return b64_front, b64_back


def build_room_prompt(room: str, selections: dict, description: str = "") -> str:
    parts = [f"{k}: {v}" for k, v in (selections or {}).items()]
    options_text = "; ".join(parts) if parts else "default finishes"
    return (
        f"Photorealistic interior rendering of the {room}. Apply these options: {options_text}. "
        f"Design brief: {description}. Natural lighting when appropriate." + STYLE_SUFFIX
    )


def ai_generate_room_image(room: str, selections: dict, description: str = "", size: str | None = None) -> str:
    prompt = build_room_prompt(room, selections, description)
    return ai_generate_image(prompt, size=size)


# =========================
# Routes
# =========================
@app.route("/")
def home():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user_id = authenticate_user(username, password)
        if user_id:
            session["user_id"] = user_id
            session.permanent = True
            next_url = request.args.get("next") or url_for("dashboard")
            return redirect(next_url)
        return "Invalid credentials", 401
    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            return "Username and password are required", 400
        uid = create_user(username, password)
        if uid:
            session["user_id"] = uid
            session.permanent = True
            return redirect(url_for("dashboard"))
        return "Username already exists", 409
    return render_template("signup.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/dashboard")
@login_required
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    rooms = db_get_rooms()
    return render_template("dashboard.html", rooms=rooms)


@app.route("/renderings")
@login_required
def get_renderings():
    if "user_id" not in session:
        return redirect(url_for("login"))
    renderings = db_get_renderings(session["user_id"]) 
    return jsonify(renderings)


@app.route("/catalog/<room>")
def get_catalog(room):
    return jsonify(db_get_catalog(room))


@app.route("/api/generate_exteriors", methods=["POST"])
def generate_exteriors():
    """Generate Front & Back exterior renderings using OpenAI images API."""
    description = request.form.get("description", "").strip()

    # Optional plan upload
    plan_file = request.files.get("plan")
    plan_note = ""
    if plan_file and plan_file.filename and allowed_file(plan_file.filename):
        plan_path = os.path.join(app.config["PLAN_FOLDER"], plan_file.filename)
        plan_file.save(plan_path)
        plan_note = " based on the provided architectural plan"

    if not description:
        return jsonify({"error": "Please provide a home description."}), 400

    try:
        b64_front, b64_back = ai_generate_exterior_pair(description, plan_note, size=DEFAULT_IMAGE_SIZE)

        front_rel = _save_b64_image(b64_front, prefix="front_exterior")
        back_rel = _save_b64_image(b64_back, prefix="back_exterior")

        # Save to DB if logged in
        user_id = session.get("user_id")
        if user_id:
            _insert_rendering_record(user_id, front_rel)
            _insert_rendering_record(user_id, back_rel)

        return jsonify({"front": front_rel, "back": back_rel})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/generate_room", methods=["POST"])
def generate_room():
    """Generate a room rendering from selected options."""
    data = request.get_json(silent=True) or {}
    room = data.get("room")
    selections = data.get("selections", {})  # {category: choice}
    description = data.get("description", "")

    if not room:
        return jsonify({"error": "Missing room name."}), 400

    try:
        b64 = ai_generate_room_image(room, selections, description, size=DEFAULT_IMAGE_SIZE)
        rel = _save_b64_image(b64, prefix=room.lower().replace(" ", "_"))
        user_id = session.get("user_id")
        if user_id:
            _insert_rendering_record(user_id, rel)
        return jsonify({"image": rel})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---- Gallery management API ----

def send_email_with_attachments(to_addr: str, subject: str, body: str, abs_paths: list[str]):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pwd  = os.environ.get("SMTP_PASSWORD")
    port = int(os.environ.get("SMTP_PORT", "587"))
    mail_from = os.environ.get("MAIL_FROM", user or "")
    if not (host and user and pwd and mail_from):
        raise RuntimeError("SMTP is not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, and MAIL_FROM env vars.")

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body or "Renderings attached.")

    for p in abs_paths:
        if not os.path.exists(p):
            continue
        with open(p, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="image", subtype="png", filename=os.path.basename(p))

    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, pwd)
        s.send_message(msg)


@app.route("/api/renderings/list")
@login_required
def api_renderings_list():
    if "user_id" not in session:
        return jsonify({"error": "auth required"}), 401
    rows = db_get_renderings(session["user_id"])
    out = [
        {
            "id": r[0],
            "filename": r[1],
            "url": url_for("uploaded_file", filename=r[1]),
            "liked": bool(r[2]),
            "favorited": bool(r[3]),
        }
        for r in rows
    ]
    return jsonify({"renderings": out})


@app.route("/api/renderings/like", methods=["POST"]) 
@login_required 
def api_renderings_like():
    if "user_id" not in session:
        return jsonify({"error": "auth required"}), 401
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    liked = data.get("liked", True)
    count = db_update_renderings_like_favorite(session["user_id"], ids, liked=bool(liked))
    return jsonify({"updated": count})


@app.route("/api/renderings/favorite", methods=["POST"]) 
@login_required 
def api_renderings_favorite():
    if "user_id" not in session:
        return jsonify({"error": "auth required"}), 401
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    favorited = data.get("favorited", True)
    count = db_update_renderings_like_favorite(session["user_id"], ids, favorited=bool(favorited))
    return jsonify({"updated": count, "favorites_count": db_count_favorites(session["user_id"])})


@app.route("/api/renderings/favorites/count")
@login_required
def api_renderings_fav_count():
    if "user_id" not in session:
        return jsonify({"error": "auth required"}), 401
    return jsonify({"count": db_count_favorites(session["user_id"])})


@app.route("/api/renderings/delete", methods=["POST"]) 
@login_required 
def api_renderings_delete():
    if "user_id" not in session:
        return jsonify({"error": "auth required"}), 401
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    # Collect paths first
    rows = db_get_filenames_for_ids(session["user_id"], ids)
    deleted = db_delete_renderings(session["user_id"], ids)
    # Remove files from disk
    for _, rel in rows:
        path = os.path.join(app.config["UPLOAD_FOLDER"], rel)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
    return jsonify({"deleted": deleted})


@app.route("/api/renderings/download-zip", methods=["POST"]) 
@login_required 
def api_renderings_download_zip():
    if "user_id" not in session:
        return jsonify({"error": "auth required"}), 401
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    rows = db_get_liked_filenames_for_ids(session["user_id"], ids)
    if not rows:
        return jsonify({"error": "No liked renderings selected."}), 400

    ts = int(time.time() * 1000)
    rel_zip = os.path.join("downloads", f"renderings_{ts}.zip")
    abs_zip = os.path.join(app.config["UPLOAD_FOLDER"], rel_zip)

    with zipfile.ZipFile(abs_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for _, rel in rows:
            abs_p = os.path.join(app.config["UPLOAD_FOLDER"], rel)
            if os.path.exists(abs_p):
                z.write(abs_p, arcname=os.path.basename(abs_p))

    return jsonify({"zip": rel_zip, "url": url_for("uploaded_file", filename=rel_zip)})


@app.route("/api/renderings/email", methods=["POST"]) 
@login_required 
def api_renderings_email():
    if "user_id" not in session:
        return jsonify({"error": "auth required"}), 401
    data = request.get_json(silent=True) or {}
    ids = data.get("ids", [])
    to_addr = data.get("to")
    subject = data.get("subject", "Your selected renderings")
    body = data.get("body", "Renderings attached.")
    if not to_addr:
        return jsonify({"error": "Missing 'to' email."}), 400

    rows = db_get_liked_filenames_for_ids(session["user_id"], ids)
    if not rows:
        return jsonify({"error": "No liked renderings selected."}), 400

    paths = [os.path.join(app.config["UPLOAD_FOLDER"], rel) for _, rel in rows]
    try:
        send_email_with_attachments(to_addr, subject, body, paths)
        return jsonify({"sent": True, "count": len(paths)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    # Serve files from the uploads folder
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# Legacy demo endpoint kept for compatibility, now stores b64 like others
@app.route("/generate_rendering", methods=["POST"])
@login_required
def generate_rendering():
    if "user_id" not in session:
        return redirect(url_for("login"))

    prompt = request.form.get("prompt")
    if not prompt:
        return "Prompt is required", 400

    try:
        b64 = ai_generate_image(prompt, size="512x512")
        rel = _save_b64_image(b64, prefix="quick")
        db_add_rendering(session["user_id"], rel)
        return jsonify({"image": rel})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================
# Templates (scaffold so routes won't 404)
# =========================
INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Architect 3D Home Modeler</title>
    <style>
        .card { border: 1px solid #ccc; padding: 12px; margin: 12px 0; max-width: 1040px; }
        .img-wrap { position: relative; display: inline-block; }
        .img-wrap.dark img { filter: brightness(0.6) contrast(1.1) saturate(0.9); }
        .actions { margin-top: 8px; }
        .row { display:flex; gap:24px; flex-wrap:wrap; }
        .hidden { display:none; }
    </style>
    <script>
        function startVoice(){
            const desc = document.getElementById('description');
            const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
            if(!SR){ alert('Voice not supported in this browser.'); return; }
            const rec = new SR();
            rec.lang = 'en-US';
            rec.onresult = (e)=>{ desc.value += (desc.value ? ' ' : '') + e.results[0][0].transcript; };
            rec.start();
        }
        async function generateExteriors(){
            const form = document.getElementById('genForm');
            const fd = new FormData(form);
            const btn = document.getElementById('genBtn');
            btn.disabled = true; btn.textContent = 'Generating...';
            const res = await fetch('/api/generate_exteriors', { method:'POST', body: fd });
            const data = await res.json();
            btn.disabled = false; btn.textContent = 'Generate House Plan';
            if(data.error){ alert(data.error); return; }
            const front = `/uploads/${data.front}`;
            const back = `/uploads/${data.back}`;
            document.getElementById('frontImg').src = front;
            document.getElementById('backImg').src = back;
            document.getElementById('results').classList.remove('hidden');
        }
        function toggleDark(id){ document.getElementById(id).classList.toggle('dark'); }
    </script>
</head>
<body>
    <h1>Architect 3D Home Modeler</h1>
    <form id="genForm" onsubmit="event.preventDefault(); generateExteriors();">
        <label>Home Description</label><br>
        <textarea id="description" name="description" rows="4" cols="80" placeholder="Describe the style, size, materials, rooms (mention basement if any)..." required></textarea><br>
        <button type="button" onclick="startVoice()">🎤 Voice Prompt</button>
        <br><br>
        <label>Upload Architectural Plan (optional)</label>
        <input type="file" name="plan" accept="image/*,application/pdf">
        <br><br>
        <button id="genBtn" type="submit">Generate House Plan</button>
        <span style="margin-left:12px">or <a href='{{ url_for("login") }}'>Sign in</a> to save your renderings</span>
    </form>

    <div id="results" class="hidden">
        <h2>Exteriors</h2>
        <div class="row">
            <div class="card">
                <h3>Front Exterior</h3>
                <div id="frontWrap" class="img-wrap"><img id="frontImg" width="512"></div>
                <div class="actions"><button onclick="toggleDark('frontWrap')">Dark Mode</button></div>
            </div>
            <div class="card">
                <h3>Back Exterior</h3>
                <div id="backWrap" class="img-wrap"><img id="backImg" width="512"></div>
                <div class="actions"><button onclick="toggleDark('backWrap')">Dark Mode</button></div>
            </div>
        </div>
        <p><a href='{{ url_for("login") }}'>Login</a> to like/favorite and manage your gallery.</p>
    </div>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Login - Architect 3D Home Modeler</title>
</head>
<body>
    <h1>Login</h1>
    <form method="POST">
        <label>Username:</label>
        <input type="text" name="username" required><br>
        <label>Password:</label>
        <input type="password" name="password" required><br>
        <button type="submit">Login</button>
    </form>
    <p>Don't have an account? <a href='{{ url_for("signup") }}'>Sign up</a></p>
</body>
</html>
"""

SIGNUP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Sign Up - Architect 3D Home Modeler</title>
</head>
<body>
    <h1>Create Account</h1>
    <form method="POST">
        <label>Username:</label>
        <input type="text" name="username" required><br>
        <label>Password:</label>
        <input type="password" name="password" required><br>
        <button type="submit">Sign Up</button>
    </form>
    <p>Already have an account? <a href='{{ url_for("login") }}'>Login</a></p>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Dashboard - Architect 3D Home Modeler</title>
    <style>
        .row { display:flex; gap:24px; flex-wrap:wrap; }
        .card { border:1px solid #ccc; padding:12px; max-width:1040px; }
        .img { max-width:512px; display:block; }
    </style>
    <script>
        async function loadCatalog(room) {
            const res = await fetch(`/catalog/${room}`);
            const catalog = await res.json();
            let html = `<h2>${room}</h2>`;
            for (const [cat, values] of Object.entries(catalog)) {
                html += `<label>${cat}:</label><select name=\"opt_${'${'}cat{'}'}\">`;
                for (const val of values) { html += `<option>${'${'}val{'}'}</option>`; }
                html += `</select><br>`;
            }
            html += `<br><label>Extra Description (optional)</label><br><textarea id=\"extraDesc\" rows=\"3\" cols=\"60\"></textarea>`;
            html += `<br><button onclick=\"generateRoom('${'${'}room{'}'}')\">Generate Room Rendering</button>`;
            document.getElementById("options").innerHTML = html;
        }
        function collectSelections(){
            const selects = document.querySelectorAll('#options select');
            const obj = {}; selects.forEach(s=>{ obj[s.name.replace('opt_','')] = s.value; });
            return obj;
        }
        async function generateRoom(room){
            const selections = collectSelections();
            const description = document.getElementById('extraDesc')?.value || '';
            const res = await fetch('/api/generate_room', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body: JSON.stringify({ room, selections, description })
            });
            const data = await res.json();
            if(data.error){ alert(data.error); return; }
            const img = document.getElementById('roomImg');
            img.src = `/uploads/${'${'}data.image{'}'}`;
            document.getElementById('roomResult').style.display = 'block';
        }
    </script>
</head>
<body>
    <h1>Your Dashboard</h1>
    <p>Welcome! You can view and manage your renderings here.</p>
    <h3>Select a Room to Customize:</h3>
    <ul>
        {% for room in rooms %}
        <li><button onclick="loadCatalog('{{room}}')">{{room}}</button></li>
        {% endfor %}
    </ul>
    <div id="options"></div>
    <div id="roomResult" style="display:none" class="card">
        <h3>Generated Room Rendering</h3>
        <img id="roomImg" class="img" />
    </div>
    <p>Sample Rendering: <a href='{{ url_for("uploaded_file", filename="sample_rendering.txt") }}'>View</a></p>
    <a href='{{ url_for("logout") }}'>Logout</a>
</body>
</html>
"""


# Ensure templates are written at runtime if missing
@app.before_request
def ensure_templates():
    tpl_dir = os.path.join(app.root_path, "templates")
    os.makedirs(tpl_dir, exist_ok=True)
    paths = {
        os.path.join(tpl_dir, "index.html"): INDEX_HTML,
        os.path.join(tpl_dir, "login.html"): LOGIN_HTML,
        os.path.join(tpl_dir, "signup.html"): SIGNUP_HTML,
        os.path.join(tpl_dir, "dashboard.html"): DASHBOARD_HTML,
    }
    for path, content in paths.items():
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(content)


# =========================
# Run
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=app.config.get("DEBUG", False))
