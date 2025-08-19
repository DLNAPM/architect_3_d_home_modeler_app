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
# ---------Recent Updates 08202025 -----------
- Redesigned landing page to match professional layout.
- Created a dedicated page for guest/session renderings.
- Added a conditional "View Session" button to the main navigation.
- FIXED: Restored the "Generate a New Room" section to the gallery page.
- Login is now only required to save (like/favorite) renderings.
- Exteriors can be modified immediately after generation.
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
        user_id INTEGER, -- NULL for guest renderings
        category TEXT NOT NULL,
        subcategory TEXT NOT NULL,
        options_json TEXT,
        prompt TEXT NOT NULL,
        image_path TEXT NOT NULL,
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
    init_fs_once()
    init_db_once()

def login_required(f):
    @wraps(f)
    def wrap(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to perform this action.", "warning")
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
BASIC_ROOMS = ["Living Room", "Kitchen", "Home Office", "Primary Bedroom", "Primary Bathroom", "Other Bedroom", "Half Bath", "Family Room"]
BASEMENT_ROOMS = ["Basement: Game Room", "Basement: Gym", "Basement: Theater Room", "Basement: Hallway"]

def build_room_list(description: str):
    rooms = BASIC_ROOMS.copy()
    if "basement" in (description or "").lower():
        rooms += BASEMENT_ROOMS
    return rooms

def build_prompt(subcategory: str, options_map: dict, description: str, plan_uploaded: bool):
    selections = ", ".join([f"{k}: {v}" for k, v in options_map.items() if v and v not in ["None", ""]])
    plan_hint = "Consider the uploaded architectural plan as a guide. " if plan_uploaded else ""
    if subcategory == "Front Exterior":
        description = description.replace("pool", "")
        selections = ", ".join([s for s in selections.split(", ") if "pool" not in s.lower()])
    base = (f"High-quality photorealistic {subcategory} rendering for a residential home. "
            f"{plan_hint}"
            f"Design intent: {description.strip() or 'Client unspecified style; pick tasteful contemporary.'} "
            f"Apply choices -> {selections or 'designer’s choice with cohesive style'}. "
            f"Balanced composition, realistic lighting, 4k detail, magazine quality.")
    return base

def save_image_bytes(png_bytes: bytes) -> str:
    uid = uuid.uuid4().hex
    filepath = RENDER_DIR / f"{uid}.png"
    with open(filepath, "wb") as f: f.write(png_bytes)
    return f"renderings/{filepath.name}"

def generate_image_via_openai(prompt: str) -> str:
    if openai_client is None or not OPENAI_API_KEY:
        raise RuntimeError("OpenAI client not configured. Set OPENAI_API_KEY.")
    try:
        result = openai_client.images.generate(model="dall-e-3", prompt=prompt, size="1024x1024", quality="standard", response_format="b64_json", n=1)
        b64 = result.data[0].b64_json
        if not b64: raise RuntimeError("No image data returned from OpenAI.")
        return save_image_bytes(base64.b64decode(b64))
    except Exception as e:
        raise RuntimeError(f"OpenAI image generation failed: {e}")

# ---------- Email ----------
def send_email_with_images(to_email: str, subject: str, body: str, image_paths: list):
    # (Implementation remains the same)
    pass

# ---------- Routes ----------

@app.route("/")
def index():
    return render_template("index.html", app_name=APP_NAME, user=current_user(), basic_rooms=BASIC_ROOMS)

@app.post("/generate")
def generate():
    description = request.form.get("description", "").strip()
    plan_file = request.files.get("plan_file")
    plan_uploaded = bool(plan_file and plan_file.filename)
    if plan_uploaded:
        (UPLOAD_DIR / f"{uuid.uuid4().hex}_{plan_file.filename}").write_bytes(plan_file.read())

    user_id = session.get("user_id")
    new_rendering_ids = []
    
    conn = get_db()
    cur = conn.cursor()
    
    for subcat in ["Front Exterior", "Back Exterior"]:
        try:
            prompt = build_prompt(subcat, {}, description, plan_uploaded)
            rel_path = generate_image_via_openai(prompt)
            now = datetime.utcnow().isoformat()
            cur.execute("""
                INSERT INTO renderings (user_id, category, subcategory, options_json, prompt, image_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (user_id, "EXTERIOR", subcat, json.dumps({}), prompt, rel_path, now))
            conn.commit()
            new_rendering_ids.append(cur.lastrowid)
        except Exception as e:
            conn.close()
            flash(str(e), "danger")
            return redirect(url_for("index"))
    
    conn.close()
    session['new_rendering_ids'] = new_rendering_ids
    if not user_id:
        guest_ids = session.get('guest_rendering_ids', [])
        guest_ids.extend(new_rendering_ids)
        session['guest_rendering_ids'] = guest_ids

    flash("Generated Front & Back exterior renderings!", "success")
    return redirect(url_for("gallery"))

@app.post("/generate_room")
def generate_room():
    subcategory = request.form.get("subcategory")
    description = request.form.get("description", "")
    selected = {opt_name: request.form.get(opt_name) for opt_name in OPTIONS.get(subcategory, {}).keys()}
    prompt = build_prompt(subcategory, selected, description, False)
    
    try:
        rel_path = generate_image_via_openai(prompt)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    user_id = session.get("user_id")
    conn = get_db()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO renderings (user_id, category, subcategory, options_json, prompt, image_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, "ROOM", subcategory, json.dumps(selected), prompt, rel_path, now))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()

    if not user_id:
        guest_ids = session.get('guest_rendering_ids', [])
        guest_ids.append(new_id)
        session['guest_rendering_ids'] = guest_ids
    
    return jsonify({"id": new_id, "path": url_for('static', filename=rel_path), "subcategory": subcategory, "message": f"Generated {subcategory} rendering!"})

@app.get("/gallery")
def gallery():
    user = current_user()
    if not user:
        # If not logged in, redirect to the session gallery
        return redirect(url_for('session_gallery'))

    items, new_items = [], []
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("SELECT * FROM renderings WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
    items = [dict(row) for row in cur.fetchall()]
    new_ids = session.pop('new_rendering_ids', [])
    new_items = [item for item in items if item['id'] in new_ids]
        
    conn.close()
    
    for item in items: item['options_dict'] = json.loads(item.get('options_json', '{}') or '{}')

    fav_count = sum(1 for r in items if r.get("favorited"))
    all_rooms = build_room_list("")

    return render_template("gallery.html", app_name=APP_NAME, user=user, items=items,
                           new_items=new_items, show_slideshow=(fav_count >= 2),
                           rooms=all_rooms, options=OPTIONS)

@app.get("/session_gallery")
def session_gallery():
    user = current_user()
    if user:
        return redirect(url_for('gallery')) # Logged-in users shouldn't be here

    items = []
    guest_ids = session.get('guest_rendering_ids', [])
    if guest_ids:
        conn = get_db()
        cur = conn.cursor()
        q_marks = ",".join("?" for _ in guest_ids)
        cur.execute(f"SELECT * FROM renderings WHERE id IN ({q_marks}) ORDER BY created_at DESC", guest_ids)
        items = [dict(row) for row in cur.fetchall()]
        conn.close()
        
    for item in items: item['options_dict'] = json.loads(item.get('options_json', '{}') or '{}')

    return render_template("session_gallery.html", app_name=APP_NAME, user=user, items=items, options=OPTIONS)

@app.post("/bulk_action")
@login_required
def bulk_action():
    # (Implementation remains the same)
    pass

@app.get("/slideshow")
@login_required
def slideshow():
    # (Implementation remains the same)
    pass

@app.post("/modify_rendering/<int:rid>")
def modify_rendering():
    # (Implementation remains the same)
    pass

# ---------- Auth Routes (Login, Register, Logout) ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    # (Implementation remains the same)
    pass

@app.route("/login", methods=["GET", "POST"])
def login():
    # (Implementation remains the same)
    pass

@app.get("/logout")
def logout():
    # (Implementation remains the same)
    pass


# ---------- Scaffolding and Main Execution ----------
def write_template_files_if_missing():
    # layout.html
    (TEMPLATES_DIR / "layout.html").write_text("""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_name }}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
</head>
<body>
  <header class="topbar">
    <a class="brand" href="{{ url_for('index') }}">{{ app_name }}</a>
    <nav class="nav">
      {% if user %}
        <a href="{{ url_for('gallery') }}">My Gallery</a>
      {% else %}
        {% if session.get('guest_rendering_ids') %}
        <a href="{{ url_for('session_gallery') }}" class="button-outline">
          View Session <span class="badge">{{ session['guest_rendering_ids']|length }}</span>
        </a>
        {% endif %}
      {% endif %}
      
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
    <div id="flash-container">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}{% for cat,msg in messages %}<div class="flash {{ cat }}">{{ msg }}</div>{% endfor %}{% endif %}
    {% endwith %}
    </div>
    {% block content %}{% endblock %}
  </main>
  <script>
    const IS_LOGGED_IN = {{ 'true' if user else 'false' }};
  </script>
  <script src="{{ url_for('static', filename='app.js') }}"></script>
</body>
</html>
""", encoding="utf-8")

    # index.html
    (TEMPLATES_DIR / "index.html").write_text("""{% extends "layout.html" %}{% block content %}
<div class="landing-content">
  <h1>Design Your Dream Home with AI</h1>
  <p>Bring your vision to life. Describe your ideal home, and our AI will generate stunning, photorealistic renderings in moments.</p>
  
  <div class="landing-grid">
    <div class="landing-column">
      <form class="card" action="{{ url_for('generate') }}" method="post" enctype="multipart/form-data">
        <h2>1. Describe Your Home</h2>
        <textarea id="description" name="description" rows="8" placeholder="e.g., A two-story modern farmhouse with a wrap-around porch, black metal roof, and large windows. Include a lush garden and a stone pathway..."></textarea>
        <div class="row gap">
          <button type="button" id="voiceBtn" class="button">🎤 Use Voice</button>
          <label class="file-label">
            <input type="file" name="plan_file" accept="image/*,.pdf">
            <span>📤 Upload Plan (Optional)</span>
          </label>
        </div>
        <h2>2. Generate Exteriors</h2>
        <button class="primary" type="submit">Generate House Exteriors</button>
      </form>
    </div>
    <div class="landing-column">
        <section class="card">
            <h2>Quick Rooms</h2>
            <p>After generating exteriors, you’ll be able to design individual rooms with detailed options.</p>
            <ul class="pill-list">
              {% for r in basic_rooms %}<li>{{ r }}</li>{% endfor %}
            </ul>
        </section>
    </div>
  </div>
</div>
{% endblock %}
""", encoding="utf-8")
    
    # gallery.html
    (TEMPLATES_DIR / "gallery.html").write_text("""{% extends "layout.html" %}

{% from "macros.html" import render_card %}

{% block content %}
<h1>My Renderings</h1>

{% if new_items %}
<div class="card">
  <h2>Newly Generated</h2>
  <div class="grid">
    {% for r in new_items %}{{ render_card(r, options, user) }}{% endfor %}
  </div>
</div>
{% endif %}

<div class="card bulk-actions">
    <div class="row space">
        <div>
            <label><input type="checkbox" id="selectAll"> Select All</label>
            <button id="likeBtn">❤️ Like</button>
            <button id="favBtn">⭐ Favorite</button>
            <button id="deleteBtn">🗑️ Delete</button>
        </div>
    </div>
    {% if show_slideshow %}<a href="{{ url_for('slideshow') }}" class="button primary">▶️ View Favorites Slideshow</a>{% endif %}
</div>

<h3>All My Renderings</h3>
<div id="renderingsGrid" class="grid">
    {% for r in items %}{{ render_card(r, options, user) }}{% endfor %}
</div>

<!-- BUG FIX: This section is now correctly displayed -->
<div class="card">
    <h2>Generate a New Room</h2>
    <form id="generateRoomForm">
        <select id="roomSelect" name="subcategory">
            {% for room in rooms %}<option value="{{ room }}">{{ room }}</option>{% endfor %}
        </select>
        <div id="roomOptionsContainer"></div>
        <button type="submit" class="primary">Generate Room</button>
    </form>
</div>

<div id="imageModal" class="modal"><span class="close-modal">&times;</span><img class="modal-content" id="modalImg"></div>

<script>
    const ROOM_OPTIONS = {{ options | tojson }};
</script>
{% endblock %}
""", encoding="utf-8")

    # NEW: session_gallery.html
    (TEMPLATES_DIR / "session_gallery.html").write_text("""{% extends "layout.html" %}

{% from "macros.html" import render_card %}

{% block content %}
<h1>Your Current Session</h1>
<div class="card info">
  <p>These are the renderings you've created in this session. They will be lost when you close your browser.</p>
  <p><strong><a href="{{ url_for('register') }}">Create an account</a> or <a href="{{ url_for('login') }}">log in</a> to save your work permanently.</strong></p>
</div>

{% if items %}
<div id="renderingsGrid" class="grid">
    {% for r in items %}{{ render_card(r, options, user) }}{% endfor %}
</div>
{% else %}
<div class="card">
    <p>You haven't generated any renderings in this session yet. <a href="{{ url_for('index') }}">Start designing!</a></p>
</div>
{% endif %}

<div id="imageModal" class="modal"><span class="close-modal">&times;</span><img class="modal-content" id="modalImg"></div>
{% endblock %}
""", encoding="utf-8")

    # NEW: macros.html for the reusable card
    (TEMPLATES_DIR / "macros.html").write_text("""
{% macro render_card(r, options, user) %}
<div class="render-card" data-id="{{ r['id'] }}">
    {% if user %}<input type="checkbox" name="rendering_id" class="rendering-checkbox">{% endif %}
    <img src="{{ url_for('static', filename=r['image_path']) }}" alt="{{ r['subcategory'] }}" class="render-img modal-trigger">
    <div class="meta">
        <span class="tag">{{ r['subcategory'] }}</span>
        <div class="actions">
            <button class="action-btn like-btn {% if r['liked'] %}active{% endif %}" title="Like">❤️</button>
            <button class="action-btn fav-btn {% if r['favorited'] %}active{% endif %}" title="Favorite">⭐</button>
            <button class="action-btn dark-toggle" title="Toggle Dark Mode">🌙</button>
        </div>
    </div>
    <div class="modify-section">
        <details>
            <summary>Modify This Rendering</summary>
            <form class="modify-form" data-id="{{ r['id'] }}">
                <textarea name="description" rows="2" placeholder="Describe changes... e.g., 'make the siding dark blue'"></textarea>
                {% if options[r['subcategory']] %}
                <div class="options-grid">
                  {% for opt, vals in options[r['subcategory']].items() %}
                  <label>{{ opt }}
                    <select name="{{ opt }}">
                      {% set current_val = r['options_dict'].get(opt) %}
                      <option value="">-- Default --</option>
                      {% for v in vals %}<option value="{{ v }}" {% if v == current_val %}selected{% endif %}>{{ v }}</option>{% endfor %}
                    </select>
                  </label>
                  {% endfor %}
                </div>
                {% endif %}
                <button type="submit" class="button">Regenerate</button>
            </form>
        </details>
    </div>
</div>
{% endmacro %}
""", encoding="utf-8")

def write_basic_static_if_missing():
    # CSS needs updates for landing page and badge
    (STATIC_DIR / "app.css").write_text("""
/* (previous styles...) */
:root { --bg: #f4f7fa; --text: #1a202c; --card-bg: #fff; --border: #e2e8f0; --primary: #4a6dff; --primary-text: #fff; --hover: #f0f3ff; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: var(--bg); color: var(--text); line-height: 1.6; }
.container { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; }
.topbar { display: flex; justify-content: space-between; align-items: center; padding: 1rem; border-bottom: 1px solid var(--border); background-color: var(--card-bg); }
.brand { font-weight: bold; text-decoration: none; color: var(--text); }
.nav a { margin-left: 1rem; text-decoration: none; color: var(--text); }
.card { background-color: var(--card-bg); border: 1px solid var(--border); border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
.button, button { background-color: #e2e8f0; color: #2d3748; border: none; padding: 0.75rem 1rem; border-radius: 6px; cursor: pointer; font-weight: bold; text-decoration: none; display: inline-block; }
.button.primary, button.primary { background-color: var(--primary); color: var(--primary-text); }
.row { display: flex; align-items: center; }
.gap > * { margin-right: 0.5rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem; }
/* ... (existing styles) */

/* --- NEW STYLES --- */
.landing-content { max-width: 1000px; margin: 2rem auto; text-align: center; }
.landing-grid { display: grid; grid-template-columns: 2fr 1fr; gap: 2rem; text-align: left; margin-top: 2rem; }
.landing-column .card { height: 100%; }
.pill-list { list-style: none; padding: 0; display: flex; flex-wrap: wrap; gap: 0.5rem; }
.pill-list li { background: #edf2f7; padding: 0.25rem 0.75rem; border-radius: 99px; font-size: 0.9em; }
.button-outline { background-color: transparent; border: 1px solid var(--primary); color: var(--primary); padding: 0.5rem 1rem; }
.badge { background-color: var(--primary); color: var(--primary-text); font-size: 0.75em; padding: 2px 6px; border-radius: 8px; margin-left: 4px; }
.info { background-color: #bee3f8; color: #2c5282; padding: 1rem; border-radius: 6px; }

@media (max-width: 768px) { .landing-grid { grid-template-columns: 1fr; } }
    """, encoding="utf-8")
    
    # (JS and other static files can remain the same)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
