#!/usr/bin/env python3
"""
Architect 3D Home Modeler – Flask 3.x single-file app
- Auth (register/login/logout)
- House plan generation (2 exterior images) via OpenAI
- Room categories w/ dropdown options (as specified)
- Multi-select actions: Delete, Like, Favorite, Download, Email
- Slideshow for 2+ favorites
- Voice prompt (Web Speech API)
- Dark mode toggle per rendering (CSS filter)
- @app.before_request + guard for one-time init (Flask 3.x safe)
- Auto-scaffold templates/ and static/ on first run

Requirements (create requirements.txt with these):
-------------------------------------------------
Flask>=3.0
Werkzeug>=3.0
itsdangerous>=2.2
Jinja2>=3.1
python-dotenv>=1.0
openai>=1.30.0
Pillow>=10.0
email-validator>=2.1
"""

import os
import sqlite3
import uuid
import json
import base64
from datetime import datetime
from functools import wraps
from pathlib import Path
from io import BytesIO
from email.utils import formataddr

from flask import (
    Flask, request, render_template, redirect, url_for,
    flash, session, send_from_directory, jsonify, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image
from email.message import EmailMessage
import smtplib

# ---------- Config ----------
APP_NAME = "Architect 3D Home Modeler"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "architect.db"
UPLOAD_DIR = BASE_DIR / "uploads"
RENDER_DIR = BASE_DIR / "static" / "renderings"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

# Create Flask app
app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=str(STATIC_DIR))

# Secret key
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or os.urandom(32)

# One-time init guard
app.config.setdefault("DB_INITIALIZED", False)
app.config.setdefault("FS_INITIALIZED", False)

# Email envs
MAIL_SERVER = os.getenv("MAIL_SERVER")
MAIL_PORT = int(os.getenv("MAIL_PORT") or "587")
MAIL_USERNAME = os.getenv("MAIL_USERNAME")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "1") in ("1", "true", "True")
MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER") or f"no-reply@{APP_NAME.replace(' ', '').lower()}.local"

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    print("WARNING: OPENAI_API_KEY not set. Image generation will fail until you set it.")

# Use OpenAI Images API via latest SDK
try:
    from openai import OpenAI
    openai_client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    openai_client = None
    print("OpenAI SDK not available yet:", e)

# ---------- Helpers ----------

def init_fs_once():
    """Make sure folders & templates exist once."""
    if not app.config["FS_INITIALIZED"]:
        for p in [UPLOAD_DIR, RENDER_DIR, STATIC_DIR, TEMPLATES_DIR]:
            p.mkdir(parents=True, exist_ok=True)
        write_template_files_if_missing()
        write_basic_static_if_missing()
        app.config["FS_INITIALIZED"] = True

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db_once():
    """Initialize SQLite tables once (Flask 3-safe)."""
    if app.config["DB_INITIALIZED"]:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS renderings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        category TEXT NOT NULL,     -- EXTERIOR or ROOM
        subcategory TEXT NOT NULL,  -- Front Exterior, Back Exterior, Living Room, etc.
        options_json TEXT,          -- saved dropdown choices
        prompt TEXT NOT NULL,       -- final prompt used for generation
        image_path TEXT NOT NULL,   -- relative path under static/
        liked INTEGER DEFAULT 0,
        favorited INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)
    conn.commit()
    conn.close()
    app.config["DB_INITIALIZED"] = True

@app.before_request
def before_request():
    # One-time filesystem & DB init, Flask 3.x safe
    init_fs_once()
    init_db_once()

def login_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrap

def current_user():
    if "user_id" in session:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],))
        row = cur.fetchone()
        conn.close()
        return row
    return None

# ---------- Domain: Options & Prompting ----------

# Exactly 5 per category as requested (examples are varied but you can tune)
OPTIONS = {
    "Front Exterior": {
        "Siding Material": ["Brick", "Stucco", "Fiber-cement", "Wood plank", "Stone veneer"],
        "Roof Style": ["Gable", "Hip", "Flat parapet", "Dutch gable", "Modern shed"],
        "Window Trim Color": ["Matte black", "Crisp white", "Bronze", "Charcoal gray", "Forest green"],
        "Landscaping": ["Boxwood hedges", "Desert xeriscape", "Lush tropical", "Minimalist gravel", "Cottage garden"],
        "Vehicle": ["None", "Luxury sedan", "Pickup truck", "SUV", "Sports car"],
        "Driveway Material": ["Concrete", "Pavers", "Gravel", "Stamped concrete", "Asphalt"],
        "Driveway Shape": ["Straight", "Curved", "Circular", "Side-load", "Split"],
        "Gate Style": ["No gate", "Modern slat", "Wrought iron", "Farm style", "Privacy panel"],
        "Garage Style": ["Single", "Double", "Carriage", "Glass-paneled", "Side-load"]
    },
    "Back Exterior": {
        "Siding Material": ["Brick", "Stucco", "Fiber-cement", "Wood plank", "Stone veneer"],
        "Roof Style": ["Gable", "Hip", "Flat parapet", "Dutch gable", "Modern shed"],
        "Window Trim Color": ["Matte black", "Crisp white", "Bronze", "Charcoal gray", "Forest green"],
        "Landscaping": ["Boxwood hedges", "Desert xeriscape", "Lush tropical", "Minimalist gravel", "Cottage garden"],
        "Swimming Pool": ["None", "Rectangular", "Freeform", "Infinity edge", "Lap pool"],
        "Paradise Grills": ["None", "Compact island", "L-shaped", "U-shaped", "Pergola bar"],
        "Basketball Court": ["None", "Half court", "Key only", "Sport tile pad", "Full court"],
        "Water Fountain": ["None", "Tiered stone", "Modern sheetfall", "Bubbling urns", "Pond with jets"],
        "Putting Green": ["None", "Single hole", "Two hole", "Wavy 3-hole", "Chipping fringe"]
    },
    "Living Room": {
        "Flooring": ["Wide oak", "Walnut herringbone", "Polished concrete", "Natural stone", "Eco bamboo"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Recessed", "Chandelier", "Floor lamps", "Track", "Wall sconces"],
        "Furniture Style": ["Modern", "Transitional", "Traditional", "Scandinavian", "Industrial"],
        "Chairs": ["Lounge pair", "Wingback", "Accent swivel", "Mid-century", "Club chairs"],
        "Coffee Tables": ["Marble slab", "Glass oval", "Reclaimed wood", "Nested set", "Stone drum"],
        "Wine Storage": ["None", "Built-in wall", "Freestanding rack", "Glass wine room", "Under-stairs"],
        "Fireplace": ["No", "Yes"],
        "Door Style": ["French", "Pocket", "Barn", "Glass pivot", "Standard panel"]
    },
    "Kitchen": {
        "Flooring": ["Wide oak", "Walnut herringbone", "Polished concrete", "Porcelain tile", "Terrazzo"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Recessed", "Linear pendant", "Island pendants", "Ceiling fixtures", "Under-cabinet"],
        "Cabinet Style": ["Shaker", "Flat-slab", "Inset", "Beaded", "Glass front"],
        "Countertops": ["Quartz", "Marble", "Granite", "Butcher block", "Concrete"],
        "Appliances": ["Stainless", "Panel-ready", "Black stainless", "Mixed metals", "Pro-grade"],
        "Backsplash": ["Subway", "Herringbone", "Slab stone", "Zellige", "Hex tile"],
        "Sink": ["Farmhouse", "Undermount SS", "Integrated stone", "Workstation", "Apron copper"],
        "Island Lights": ["Three pendants", "Linear bar", "Two globes", "Can lights", "Mixed fixtures"]
    },
    "Home Office": {
        "Flooring": ["Wide oak", "Carpet tile", "Polished concrete", "Cork", "Laminate"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Task lamp", "Track", "Recessed", "Pendant", "Wall sconces"],
        "Desk Style": ["Standing", "Executive wood", "Minimalist metal", "L-shaped", "Dual sit-stand"],
        "Office Chair": ["Ergonomic mesh", "Leather executive", "Task chair", "Stool", "Kneeling"],
        "Storage": ["Open shelves", "Closed cabinets", "Mixed", "Credenza", "Wall system"]
    },
    "Primary Bedroom": {
        "Flooring": ["Plush carpet", "Wide oak", "Cork", "Laminate", "Engineered wood"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Recessed", "Chandelier", "Wall sconces", "Ceiling fixture", "Bedside lamps"],
        "Bed Style": ["Upholstered", "Canopy", "Platform wood", "Metal frame", "Storage bed"],
        "Furniture Style": ["Modern", "Transitional", "Traditional", "Scandinavian", "Industrial"],
        "Closet Design": ["Reach-in", "Walk-in", "Wardrobe wall", "His/Hers", "Island closet"],
        "Ceiling Fan": ["None", "Modern", "Wood blade", "Industrial", "Retractable"]
    },
    "Primary Bathroom": {
        "Flooring": ["Porcelain tile", "Marble", "Terrazzo", "Natural stone", "Concrete"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Sconces", "Backlit mirror", "Recessed", "Pendant", "Chandelier"],
        "Vanity Style": ["Floating", "Furniture look", "Double", "Open shelf", "Integrated"],
        "Shower or Tub": ["Large shower", "Freestanding tub", "Tub-shower", "Wet room", "Steam shower"],
        "Tile Style": ["Subway", "Hex", "Slab stone", "Zellige", "Mosaic"],
        "Bathroom Sink": ["Undermount", "Vessel", "Integrated", "Pedestal", "Trough"],
        "Mirror Style": ["Framed", "Backlit", "Arched", "Round", "Edge-lit"],
        "Balcony": ["No", "Yes"]
    },
    "Other Bedroom": {
        "Flooring": ["Plush carpet", "Wide oak", "Cork", "Laminate", "Engineered wood"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Recessed", "Chandelier", "Wall sconces", "Ceiling fixture", "Bedside lamps"],
        "Bed Style": ["Upholstered", "Canopy", "Platform wood", "Metal frame", "Storage bed"],
        "Furniture Style": ["Modern", "Transitional", "Traditional", "Scandinavian", "Industrial"],
        "Ceiling Fan": ["None", "Modern", "Wood blade", "Industrial", "Retractable"],
        "Balcony": ["No", "Yes"]
    },
    "Half Bath": {
        "Flooring": ["Porcelain tile", "Marble", "Terrazzo", "Natural stone", "Concrete"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Sconces", "Backlit mirror", "Recessed", "Pendant", "Chandelier"],
        "Vanity Style": ["Floating", "Furniture look", "Single", "Pedestal", "Console"],
        "Tile Style": ["Subway", "Hex", "Slab stone", "Zellige", "Mosaic"],
        "Mirror Style": ["Framed", "Backlit", "Arched", "Round", "Edge-lit"]
    },
    "Basement: Game Room": {
        "Flooring": ["Carpet tile", "Vinyl plank", "Cork", "Concrete stain", "Rubber tile"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Track", "Recessed", "Neon accent", "Pendant", "Sconces"],
        "Pool Table": ["Classic wood", "Modern black", "Industrial", "Contemporary white", "Tournament"],
        "Wine Bar": ["None", "Back bar", "Wet bar", "Island bar", "Wall niche"],
        "Arcade Games": ["Pinball", "Racing", "Fighting", "Retro cabinets", "Skeeball"],
        "Other Table Games": ["Air hockey", "Foosball", "Shuffleboard", "Darts", "Poker"]
    },
    "Basement: Gym": {
        "Flooring": ["Rubber tile", "Vinyl plank", "Cork", "Foam mat", "Concrete seal"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Track", "Recessed", "Neon accent", "Pendant", "Sconces"],
        "Equipment": ["Treadmill", "Bike", "Rowing", "Cable station", "Free weights"],
        "Gym Station": ["Smith machine", "Power rack", "Functional trainer", "Multi-gym", "Calisthenics"],
        "Steam Room": ["No", "Yes"]
    },
    "Basement: Theater Room": {
        "Flooring": ["Carpet tile", "Plush carpet", "Cork", "Laminate", "Acoustic floor"],
        "Wall Color": ["Warm white", "Charcoal", "Burgundy", "Navy", "Chocolate brown"],
        "Lighting": ["Step lights", "Wall sconces", "Star ceiling", "Recessed", "LED strips"],
        "Wall Treatment": ["Acoustic panels", "Fabric", "Wood slats", "Velvet", "Painted drywall"],
        "Seating": ["Recliners", "Sofas", "Stadium rows", "Bean bags", "Mixed"],
        "Popcorn Machine": ["No", "Yes"],
        "Sound System": ["5.1", "7.1", "Atmos", "Soundbar", "Hidden in-wall"],
        "Screen Type": ["Projector", "MicroLED", "OLED", "Ultra-short-throw", "Acoustically transparent"],
        "Movie Posters": ["No", "Yes"],
        "Show Movie": ["No", "Yes"]
    },
    "Basement: Hallway": {
        "Flooring": ["Carpet tile", "Vinyl plank", "Cork", "Concrete stain", "Rubber tile"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Track", "Recessed", "Neon accent", "Pendant", "Sconces"],
        "Stairs": ["Open riser", "Closed", "Glass rail", "Wood rail", "Metal rail"]
    },
    "Family Room": {
        "Flooring": ["Wide oak", "Walnut herringbone", "Polished concrete", "Natural stone", "Eco bamboo"],
        "Wall Color": ["Warm white", "Greige", "Deep navy", "Sage", "Charcoal"],
        "Lighting": ["Recessed", "Chandelier", "Floor lamps", "Track", "Wall sconces"],
        "Furniture Style": ["Modern", "Transitional", "Traditional", "Scandinavian", "Industrial"],
        "Chairs": ["Lounge pair", "Wingback", "Accent swivel", "Mid-century", "Club chairs"]
    }
}

BASIC_ROOMS = [
    "Living Room", "Kitchen", "Home Office",
    "Primary Bedroom", "Primary Bathroom",
    "Other Bedroom", "Half Bath", "Family Room"
]

BASEMENT_ROOMS = [
    "Basement: Game Room", "Basement: Gym", "Basement: Theater Room", "Basement: Hallway"
]

def detect_basement(description: str) -> bool:
    return "basement" in (description or "").lower()

def build_room_list(description: str):
    rooms = BASIC_ROOMS.copy()
    if detect_basement(description):
        rooms += BASEMENT_ROOMS
    return rooms

def build_prompt(subcategory: str, options_map: dict, description: str, plan_uploaded: bool):
    """Create a concise, directive prompt for image generation."""
    selections = ", ".join([f"{k}: {v}" for k, v in options_map.items() if v and v != "None"])
    plan_hint = "Consider the uploaded architectural plan as a guide. " if plan_uploaded else ""
    base = (
        f"High-quality photorealistic {subcategory} rendering for a residential home. "
        f"{plan_hint}"
        f"Design intent: {description.strip() or 'Client unspecified style; pick tasteful contemporary.'} "
        f"Apply choices -> {selections or 'designer’s choice with cohesive style'}. "
        f"Balanced composition, realistic lighting, 4k detail, magazine quality."
    )
    return base

def save_image_bytes(png_bytes: bytes) -> str:
    """Save PNG bytes to static/renderings and return relative path."""
    uid = uuid.uuid4().hex
    filepath = RENDER_DIR / f"{uid}.png"
    with open(filepath, "wb") as f:
        f.write(png_bytes)
    # Return path relative to static/
    rel = f"renderings/{filepath.name}"
    return rel

def generate_image_via_openai(prompt: str) -> str:
    """
    Calls OpenAI Images API and returns relative image path under static/.
    Uses 'gpt-image-1' (available in the 2024+ SDK). If unavailable locally,
    raises an informative error.
    """
    if openai_client is None or not OPENAI_API_KEY:
        raise RuntimeError("OpenAI client not configured. Set OPENAI_API_KEY.")
    try:
        # Generate an image (1024x1024)
        result = openai_client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            quality="high",
            n=1,
        )
        b64 = result.data[0].b64_json
        png_bytes = base64.b64decode(b64)
        return save_image_bytes(png_bytes)
    except Exception as e:
        raise RuntimeError(f"OpenAI image generation failed: {e}")

def ensure_user_dir(uid: int):
    (RENDER_DIR / f"user_{uid}").mkdir(parents=True, exist_ok=True)

# ---------- Email ----------

def send_email_with_images(to_email: str, subject: str, body: str, image_paths: list):
    if not (MAIL_SERVER and MAIL_USERNAME and MAIL_PASSWORD):
        raise RuntimeError("Email not configured. Set MAIL_* environment variables.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((APP_NAME, MAIL_DEFAULT_SENDER))
    msg["To"] = to_email
    msg.set_content(body)

    for rel_path in image_paths:
        abs_path = STATIC_DIR / rel_path
        with open(abs_path, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="image", subtype="png", filename=os.path.basename(abs_path))

    with smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=30) as s:
        if MAIL_USE_TLS:
            s.starttls()
        s.login(MAIL_USERNAME, MAIL_PASSWORD)
        s.send_message(msg)

# ---------- Routes ----------

@app.route("/")
def index():
    user = current_user()
    return render_template("index.html",
                           app_name=APP_NAME,
                           user=user,
                           options=OPTIONS,
                           basic_rooms=BASIC_ROOMS)

@app.post("/generate")
def generate():
    """Generate Front & Back exteriors immediately, then show rooms."""
    description = request.form.get("description", "").strip()
    plan_file = request.files.get("plan_file")
    plan_uploaded = False

    if plan_file and plan_file.filename:
        plan_uploaded = True
        safe_name = f"{uuid.uuid4().hex}_{plan_file.filename}"
        plan_path = UPLOAD_DIR / safe_name
        plan_file.save(plan_path)

    # FRONT & BACK prompts
    front_prompt = build_prompt("Front Exterior", OPTIONS["Front Exterior"], description, plan_uploaded)
    back_prompt  = build_prompt("Back Exterior",  OPTIONS["Back Exterior"],  description, plan_uploaded)

    paths = []
    for subcat, prompt in [("Front Exterior", front_prompt), ("Back Exterior", back_prompt)]:
        try:
            rel_path = generate_image_via_openai(prompt)
            paths.append((subcat, rel_path, prompt))
        except Exception as e:
            flash(str(e), "danger")
            return redirect(url_for("index"))

    user_id = session.get("user_id")
    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()

    for subcat, rel_path, prompt in paths:
        cur.execute("""
            INSERT INTO renderings (user_id, category, subcategory, options_json, prompt, image_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, "EXTERIOR", subcat, json.dumps({}), prompt, rel_path, now))
    conn.commit()
    conn.close()

    rooms = build_room_list(description)
    flash("Generated Front & Back exterior renderings!", "success")
    # Pass options so gallery template JS always has it
    return render_template("gallery.html",
                           app_name=APP_NAME,
                           user=current_user(),
                           new_images=[{"subcategory": s, "path": p} for s, p, _ in paths],
                           rooms=rooms,
                           options=OPTIONS)

@app.post("/generate_room")
def generate_room():
    """Generate a room rendering from dropdown options."""
    subcategory = request.form.get("subcategory")  # e.g., "Living Room"
    description = request.form.get("description", "")
    plan_uploaded = request.form.get("plan_uploaded") == "1"  # from hidden field if needed

    # Collect selected room options
    selected = {}
    if subcategory in OPTIONS:
        for opt_name in OPTIONS[subcategory].keys():
            selected[opt_name] = request.form.get(opt_name)

    prompt = build_prompt(subcategory, selected, description, plan_uploaded)
    try:
        rel_path = generate_image_via_openai(prompt)
    except Exception as e:
        flash(str(e), "danger")
        return redirect(url_for("index"))

    user_id = session.get("user_id")
    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO renderings (user_id, category, subcategory, options_json, prompt, image_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, "ROOM", subcategory, json.dumps(selected), prompt, rel_path, now))
    conn.commit()
    conn.close()

    flash(f"Generated {subcategory} rendering!", "success")
    return redirect(url_for("gallery"))

@app.get("/gallery")
def gallery():
    user = current_user()
    conn = get_db()
    cur = conn.cursor()
    # show all (or user-scoped if logged in)
    if user:
        cur.execute("""SELECT * FROM renderings WHERE user_id IS NULL OR user_id = ? ORDER BY created_at DESC""", (user["id"],))
    else:
        cur.execute("""SELECT * FROM renderings ORDER BY created_at DESC""")
    items = [dict(row) for row in cur.fetchall()]
    conn.close()

    fav_count = sum(1 for r in items if r["favorited"])
    return render_template("gallery.html",
                           app_name=APP_NAME, user=user, items=items,
                           show_slideshow=(fav_count >= 2),
                           options=OPTIONS)

@app.post("/bulk_action")
def bulk_action():
    """Handle multi-select actions: like, favorite, delete, email, download list."""
    action = request.form.get("action")
    ids = request.form.getlist("rendering_ids")
    if not ids:
        flash("No renderings selected.", "warning")
        return redirect(url_for("gallery"))

    conn = get_db()
    cur = conn.cursor()

    if action == "delete":
        q_marks = ",".join("?" for _ in ids)
        cur.execute(f"SELECT image_path FROM renderings WHERE id IN ({q_marks})", ids)
        paths = [row["image_path"] for row in cur.fetchall()]
        for rel in paths:
            try:
                os.remove(STATIC_DIR / rel)
            except Exception:
                pass
        cur.execute(f"DELETE FROM renderings WHERE id IN ({q_marks})", ids)
        conn.commit()
        conn.close()
        flash(f"Deleted {len(ids)} rendering(s).", "success")

    elif action in ("like", "unlike", "favorite", "unfavorite"):
        val = 1 if action in ("like", "favorite") else 0
        field = "liked" if "like" in action else "favorited"
        q_marks = ",".join("?" for _ in ids)
        cur.execute(f"UPDATE renderings SET {field} = ? WHERE id IN ({q_marks})", (val, *ids))
        conn.commit()
        conn.close()
        verb = "Liked" if field == "liked" and val else "Unliked" if field == "liked" else "Favorited" if val else "Unfavorited"
        flash(f"{verb} {len(ids)} rendering(s).", "success")

    elif action == "email":
        to_email = request.form.get("to_email")
        if not to_email:
            conn.close()
            flash("Please provide a destination email.", "warning")
            return redirect(url_for("gallery"))
        # Only send liked renderings
        q_marks = ",".join("?" for _ in ids)
        cur.execute(f"SELECT image_path, liked FROM renderings WHERE id IN ({q_marks})", ids)
        rows = cur.fetchall()
        send_paths = [r["image_path"] for r in rows if r["liked"]]
        conn.close()
        if not send_paths:
            flash("Only 'Liked' renderings can be emailed. None selected were liked.", "warning")
            return redirect(url_for("gallery"))
        try:
            send_email_with_images(
                to_email,
                subject=f"{APP_NAME}: Selected Renderings",
                body="Here are the renderings you requested.",
                image_paths=send_paths
            )
            flash(f"Emailed {len(send_paths)} rendering(s) to {to_email}.", "success")
        except Exception as e:
            flash(f"Email failed: {e}", "danger")

    elif action == "download":
        # Return a JSON list of static URLs to download individually (UI handles)
        q_marks = ",".join("?" for _ in ids)
        cur.execute(f"SELECT id, image_path FROM renderings WHERE id IN ({q_marks})", ids)
        rows = cur.fetchall()
        conn.close()
        urls = [url_for("static", filename=row["image_path"], _external=False) for row in rows]
        return jsonify({"download_urls": urls})

    else:
        conn.close()
        flash("Unknown action.", "danger")

    return redirect(url_for("gallery"))

@app.get("/slideshow")
def slideshow():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM renderings WHERE favorited = 1 ORDER BY created_at DESC")
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    if len(items) < 2:
        flash("Favorite at least two renderings to start a slideshow.", "info")
        return redirect(url_for("gallery"))
    return render_template("slideshow.html", app_name=APP_NAME, user=current_user(), items=items)

# ---------- Auth ----------

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        name = (request.form.get("name") or "").strip()
        password = request.form.get("password") or ""
        if not email or not password:
            flash("Email and password are required.", "warning")
            return redirect(url_for("register"))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email = ?", (email,))
        if cur.fetchone():
            conn.close()
            flash("Email already registered.", "warning")
            return redirect(url_for("register"))
        pwd_hash = generate_password_hash(password)
        cur.execute("INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (email, name, pwd_hash, datetime.utcnow().isoformat()))
        conn.commit()
        user_id = cur.lastrowid
        conn.close()
        session["user_id"] = user_id
        session["user_email"] = email
        flash("Welcome! Account created.", "success")
        return redirect(url_for("index"))
    return render_template("register.html", app_name=APP_NAME, user=current_user())

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE email = ?", (email,))
        user = cur.fetchone()
        conn.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_email"] = user["email"]
            flash("Logged in successfully.", "success")
            nxt = request.args.get("next")
            return redirect(nxt or url_for("index"))
        flash("Invalid credentials.", "danger")
        return redirect(url_for("login"))
    return render_template("login.html", app_name=APP_NAME, user=current_user())

@app.get("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("index"))

# ---------- Utility (serve favicon) ----------
@app.get("/favicon.ico")
def favicon():
    return send_from_directory(app.static_folder, "favicon.ico")

# ---------- Template & Static Scaffolding ----------

def write_template_files_if_missing():
    # layout.html
    (TEMPLATES_DIR / "layout.html").write_text("""<!doctype html>
<html lang="en" data-theme="light">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_name }}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
  <script defer src="{{ url_for('static', filename='app.js') }}"></script>
</head>
<body class="page">
  <header class="topbar">
    <a class="brand" href="{{ url_for('index') }}">{{ app_name }}</a>
    <nav class="nav">
      <a href="{{ url_for('gallery') }}">Gallery</a>
      {% if user %}
        <span class="user">Hi {{ user['name'] or user['email'] }}</span>
        <a href="{{ url_for('logout') }}">Logout</a>
      {% else %}
        <a href="{{ url_for('login') }}">Login</a>
        <a href="{{ url_for('register') }}">Register</a>
      {% endif %}
    </nav>
  </header>
  <main class="container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        <div class="flashes">
          {% for cat,msg in messages %}
            <div class="flash {{ cat }}">{{ msg }}</div>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}
    {% block content %}{% endblock %}
  </main>
  <footer class="footer">
    <small>&copy; {{ 2025 }} {{ app_name }} • Built with Flask 3</small>
  </footer>
</body>
</html>
""", encoding="utf-8")

    # index.html
    (TEMPLATES_DIR / "index.html").write_text("""{% extends "layout.html" %}{% block content %}
<h1>Design Your Dream Home</h1>
<form class="card" action="{{ url_for('generate') }}" method="post" enctype="multipart/form-data">
  <label>Home Description (or use Voice)</label>
  <textarea id="description" name="description" rows="4" placeholder="e.g., Modern farmhouse with warm wood, metal roof, indoor-outdoor living..."></textarea>
  <div class="row gap">
    <button type="button" id="voiceBtn">🎤 Voice Prompt</button>
    <label class="file">
      <input type="file" name="plan_file" accept="image/*,.pdf">
      <span>Upload Architectural Plan (optional)</span>
    </label>
  </div>
  <button class="primary" type="submit">Generate House Plan (Front & Back)</button>
</form>

<section class="card">
  <h2>Quick Rooms</h2>
  <p>After you generate exteriors, you’ll see a list of rooms. You can then choose options and render each.</p>
  <ul class="pill-list">
    {% for r in basic_rooms %}
      <li>{{ r }}</li>
    {% endfor %}
  </ul>
</section>
{% endblock %}
""", encoding="utf-8")

    # gallery.html
    (TEMPLATES_DIR / "gallery.html").write_text("""{% extends "layout.html" %}{% block content %}
<h1>Your Renderings</h1>

{% if new_images %}
<div class="grid">
  {% for img in new_images %}
    <figure class="render-card">
      <img src="{{ url_for('static', filename=img.path) }}" class="render-img" data-darkable>
      <figcaption>{{ img.subcategory }}</figcaption>
      <button class="dark-toggle" data-target="prev">🌙 Dark Mode</button>
    </figure>
  {% endfor %}
</div>
{% endif %}

{% if rooms %}
<hr/>
<section class="card">
  <h2>Rooms</h2>
  <form action="{{ url_for('generate_room') }}" method="post">
    <div class="row gap">
      <label>Room</label>
      <select name="subcategory" id="roomSelect" required>
        {% for r in rooms %}
          <option value="{{ r }}">{{ r }}</option>
        {% endfor %}
      </select>
    </div>
    <div id="roomOptions" class="options-grid"></div>
    <input type="hidden" name="description" value="{{ request.form.get('description','') or request.args.get('description','') }}">
    <button class="primary" type="submit">Render Room</button>
  </form>
</section>
<script>
  // Fixed: safe default for missing options
  const OPTIONS = {{ options|default({})|tojson }};
</script>
{% endif %}

<hr/>
<form class="card" action="{{ url_for('bulk_action') }}" method="post" id="bulkForm">
  <div class="row space">
    <div class="row gap">
      <button name="action" value="like" type="submit">👍 Like</button>
      <button name="action" value="unlike" type="submit">👎 Unlike</button>
      <button name="action" value="favorite" type="submit">⭐ Favorite</button>
      <button name="action" value="unfavorite" type="submit">☆ Unfavorite</button>
      <button name="action" value="delete" type="submit" class="danger" onclick="return confirm('Delete selected renderings?')">🗑️ Delete</button>
      <button name="action" value="download" type="button" id="downloadBtn">⬇️ Download</button>
      <label class="row gap">
        <input type="email" name="to_email" placeholder="email@domain.com">
        <button name="action" value="email" type="submit">✉️ Email (Liked only)</button>
      </label>
    </div>
    {% if show_slideshow %}
      <a class="button" href="{{ url_for('slideshow') }}">🎞️ Slideshow (Favorites)</a>
    {% endif %}
  </div>
  <div class="grid">
    {% for r in items %}
    <label class="render-card selectable">
      <input type="checkbox" name="rendering_ids" value="{{ r['id'] }}">
      <img src="{{ url_for('static', filename=r['image_path']) }}" class="render-img" data-darkable>
      <div class="meta">
        <span class="tag">{{ r['subcategory'] }}</span>
        {% if r['liked'] %}<span class="tag like">Liked</span>{% endif %}
        {% if r['favorited'] %}<span class="tag fav">Fav</span>{% endif %}
      </div>
      <button class="dark-toggle" data-target="prev">🌙 Dark Mode</button>
    </label>
    {% endfor %}
  </div>
</form>

<script>
  // Room dynamic options injection
  const ALL = {{ options|default({})|tojson }};
  const select = document.getElementById('roomSelect');
  const container = document.getElementById('roomOptions');
  function renderRoomOptions() {
    if (!select || !container) return;
    container.innerHTML = '';
    const sub = select.value;
    if (!ALL[sub]) return;
    Object.entries(ALL[sub]).forEach(([k, vals])=>{
      const wrap = document.createElement('div');
      wrap.className = 'opt';
      const label = document.createElement('label');
      label.textContent = k;
      const sel = document.createElement('select');
      sel.name = k;
      vals.forEach(v=>{
        const o = document.createElement('option');
        o.value = v; o.textContent = v;
        sel.appendChild(o);
      });
      wrap.appendChild(label); wrap.appendChild(sel);
      container.appendChild(wrap);
    });
  }
  if (select) {
    select.addEventListener('change', renderRoomOptions);
    renderRoomOptions();
  }

  // Bulk download via AJAX -> open each URL
  const dlBtn = document.getElementById('downloadBtn');
  if (dlBtn) {
    dlBtn.addEventListener('click', async ()=>{
      const form = document.getElementById('bulkForm');
      const data = new FormData(form);
      data.append('action','download');
      const res = await fetch('{{ url_for("bulk_action") }}', {method:'POST', body:data});
      const j = await res.json();
      (j.download_urls||[]).forEach(u=>{
        const a = document.createElement('a');
        a.href = u; a.download = '';
        document.body.appendChild(a); a.click(); a.remove();
      });
    });
  }

  // Dark mode toggle per image (CSS filter)
  document.querySelectorAll('.dark-toggle').forEach(btn=>{
    btn.addEventListener('click', (e)=>{
      e.preventDefault();
      // If data-target="prev", toggle previous <img>
      let img = btn.previousElementSibling;
      if (!(img && img.tagName === 'IMG')) {
        img = btn.closest('.render-card')?.querySelector('img');
      }
      if (img) img.classList.toggle('dark');
    });
  });
</script>
{% endblock %}
""", encoding="utf-8")

    # slideshow.html
    (TEMPLATES_DIR / "slideshow.html").write_text("""{% extends "layout.html" %}{% block content %}
<h1>Favorites Slideshow</h1>
<div class="slideshow">
  {% for r in items %}
    <img src="{{ url_for('static', filename=r['image_path']) }}" class="slide" {% if not loop.first %}style="display:none"{% endif %}>
  {% endfor %}
</div>
<div class="row gap">
  <button id="prev">Prev</button>
  <button id="next">Next</button>
  <button id="toggleDark">🌙 Toggle Dark</button>
</div>
<script>
  const slides = Array.from(document.querySelectorAll('.slide'));
  let idx = 0;
  function show(i){
    slides.forEach((s, j)=> s.style.display = (i===j?'block':'none'));
  }
  document.getElementById('prev').onclick=()=>{ idx=(idx-1+slides.length)%slides.length; show(idx); };
  document.getElementById('next').onclick=()=>{ idx=(idx+1)%slides.length; show(idx); };
  document.getElementById('toggleDark').onclick=()=>{ slides[idx].classList.toggle('dark'); };
</script>
{% endblock %}
""", encoding="utf-8")

    # login.html
    (TEMPLATES_DIR / "login.html").write_text("""{% extends "layout.html" %}{% block content %}
<h1>Login</h1>
<form class="card" method="post">
  <label>Email</label>
  <input type="email" name="email" required>
  <label>Password</label>
  <input type="password" name="password" required>
  <button class="primary">Login</button>
</form>
<p>No account? <a href="{{ url_for('register') }}">Register here</a>.</p>
{% endblock %}
""", encoding="utf-8")

    # register.html
    (TEMPLATES_DIR / "register.html").write_text("""{% extends "layout.html" %}{% block content %}
<h1>Create Account</h1>
<form class="card" method="post">
  <label>Name</label>
  <input type="text" name="name" placeholder="(optional)">
  <label>Email</label>
  <input type="email" name="email" required>
  <label>Password</label>
  <input type="password" name="password" required>
  <button class="primary">Create Account</button>
</form>
{% endblock %}
""", encoding="utf-8")

def write_basic_static_if_missing():
    # CSS
    (STATIC_DIR / "app.css").write_text("""
:root { --bg:#0b0f14; --card:#141a22; --text:#e8eef7; --muted:#9fb0c6; --accent:#6aa6ff; --danger:#ff6a6a; }
*{box-sizing:border-box} body.page{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,Segoe UI,Roboto,Arial}
.topbar{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-bottom:1px solid #1f2835;background:#0e131a}
.brand{color:var(--text);text-decoration:none;font-weight:700}
.nav a{color:var(--muted);margin-left:14px;text-decoration:none}
.container{max-width:1100px;margin:20px auto;padding:0 16px}
.card{background:var(--card);border:1px solid #1f2835;padding:16px;border-radius:10px;margin-bottom:16px}
.row{display:flex;align-items:center} .space{justify-content:space-between} .gap>*{margin-right:8px}
.file input{display:none} .file span{border:1px dashed #2a3546;padding:10px;border-radius:6px;cursor:pointer}
.primary{background:var(--accent);color:#06101e;border:0;padding:10px 14px;border-radius:8px;cursor:pointer}
.button{background:#2a3546;color:var(--text);padding:8px 12px;border-radius:8px;text-decoration:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:12px}
.render-card{position:relative;background:#0e141b;border:1px solid #1d2533;border-radius:12px;padding:10px}
.render-card.selectable{cursor:pointer}
.render-card input[type=checkbox]{position:absolute;top:8px;left:8px;transform:scale(1.2)}
.render-img{width:100%;height:auto;border-radius:8px;display:block;transition:filter .2s ease}
.render-img.dark{filter:brightness(.7) contrast(1.1)}
.dark-toggle{margin-top:8px;background:#223047;color:#cfe3ff;border:0;padding:6px 8px;border-radius:6px;cursor:pointer}
.meta{margin-top:6px} .tag{font-size:12px;color:#c5d3e9;background:#212c3b;padding:3px 6px;border-radius:999px;margin-right:6px}
.tag.like{background:#1d3c2a;color:#9ef2b0} .tag.fav{background:#3a2a4a;color:#e3b8ff}
.flashes .flash{padding:10px;margin:8px 0;border-radius:8px}
.flash.success{background:#14301d;color:#9ef2b0}
.flash.warning{background:#332c17;color:#ffe28a}
.flash.info{background:#173246;color:#a5d9ff}
.flash.danger{background:#351b1b;color:#ffb6b6}
.pill-list{list-style:none;padding:0;display:flex;flex-wrap:wrap}
.pill-list li{padding:6px 10px;background:#202b3a;border:1px solid #2a3546;margin:6px;border-radius:999px}
.options-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-top:12px}
.opt label{display:block;font-size:13px;margin-bottom:6px;color:#cfe3ff}
.opt select, textarea, input[type=text], input[type=email], input[type=password], select {width:100%;padding:8px;border:1px solid #2a3546;border-radius:6px;background:#0c1219;color:#dbe6f6}
.footer{padding:24px;text-align:center;color:#88a2c2}
h1,h2{margin-top:0}
""", encoding="utf-8")

    # JS (voice input + small helpers)
    (STATIC_DIR / "app.js").write_text("""
window.addEventListener('DOMContentLoaded', ()=>{
  const voiceBtn = document.getElementById('voiceBtn');
  if (voiceBtn && 'webkitSpeechRecognition' in window) {
    const rec = new webkitSpeechRecognition();
    rec.continuous = false; rec.interimResults = false; rec.lang = 'en-US';
    const area = document.getElementById('description');
    voiceBtn.addEventListener('click', ()=>{
      try{ rec.start(); }catch(e){}
    });
    rec.onresult = (e)=>{
      const txt = Array.from(e.results).map(r=>r[0].transcript).join(' ');
      if (area) area.value = (area.value? area.value + ' ' : '') + txt;
    };
  } else if (voiceBtn) {
    voiceBtn.disabled = true; voiceBtn.title = 'Voice input not supported in this browser';
  }
});
""", encoding="utf-8")

    # Favicon placeholder
    from PIL import Image, ImageDraw
    ico = STATIC_DIR / "favicon.ico"
    if not ico.exists():
        img = Image.new("RGBA", (64,64), (10,16,24,255))
        d = ImageDraw.Draw(img)
        d.rectangle([8,8,56,56], outline=(106,166,255,255), width=3)
        d.text((16,22), "A3", fill=(200,220,255,255))
        img.save(ico, format="ICO")

# ---------- Main ----------

if __name__ == "__main__":
    # For local dev
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
