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
# ---------Recent Updates 08182025 -----------
- Click any rendering and enlarde to fullscreen modal view
- Type or speak “Describe Changes” on any "Rooms" and generate a new rendering
- Modify any Generated Rendering by adding or taking away any configured options on the Renderting
- Click on a "Back to Rooms" button in slideshow
- Be able to modify the Front and Back Exterior Renderings as well
- Also make sure there are no "Swimming Pools" on the Front Exterior


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
    selections = ", ".join([f"{k}: {v}" for k, v in options_map.items() if v and v != "None"])
    plan_hint = "Consider the uploaded architectural plan as a guide. " if plan_uploaded else ""

    # Prevent pools on Front Exterior
    if subcategory == "Front Exterior" and "pool" in description.lower():
        description = description.replace("pool", "")
    if subcategory == "Front Exterior":
        selections = ", ".join([s for s in selections.split(", ") if "pool" not in s.lower()])

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
    Uses 'dall-e-3' by default.
    """
    if openai_client is None or not OPENAI_API_KEY:
        raise RuntimeError("OpenAI client not configured. Set OPENAI_API_KEY.")
    try:
        # Generate an image (1024x1024) using DALL-E 3
        result = openai_client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard", # Standard is fine for web use, HD is also an option
            response_format="b64_json",
            n=1,
        )
        b64 = result.data[0].b64_json
        if not b64:
            raise RuntimeError("No image data returned from OpenAI.")
        
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
@login_required
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
    front_prompt = build_prompt("Front Exterior", {}, description, plan_uploaded)
    back_prompt  = build_prompt("Back Exterior",  {},  description, plan_uploaded)

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

    flash("Generated Front & Back exterior renderings!", "success")
    return redirect(url_for("gallery"))


@app.post("/generate_room")
@login_required
def generate_room():
    """Generate a room rendering from dropdown options."""
    subcategory = request.form.get("subcategory")
    description = request.form.get("description", "")
    plan_uploaded = request.form.get("plan_uploaded") == "1"

    selected = {}
    if subcategory in OPTIONS:
        for opt_name in OPTIONS[subcategory].keys():
            selected[opt_name] = request.form.get(opt_name)

    prompt = build_prompt(subcategory, selected, description, plan_uploaded)
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
    
    return jsonify({
        "id": new_id,
        "path": url_for('static', filename=rel_path),
        "subcategory": subcategory,
        "message": f"Generated {subcategory} rendering!"
    })

@app.get("/gallery")
@login_required
def gallery():
    user = current_user()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT * FROM renderings WHERE user_id = ? ORDER BY created_at DESC""", (user["id"],))
    items = [dict(row) for row in cur.fetchall()]
    conn.close()

    # --- FIX ---
    # Parse the JSON string into a dictionary here, in the Python code
    for item in items:
        try:
            item['options_dict'] = json.loads(item.get('options_json', '{}') or '{}')
        except (json.JSONDecodeError, TypeError):
            item['options_dict'] = {} # Use an empty dict if JSON is invalid or null
    
    all_rooms = build_room_list("") # Assuming no basement by default on gallery load
    
    fav_count = sum(1 for r in items if r["favorited"])
    return render_template("gallery.html",
                           app_name=APP_NAME, user=user, items=items,
                           show_slideshow=(fav_count >= 2),
                           rooms=all_rooms,
                           options=OPTIONS)

@app.post("/bulk_action")
@login_required
def bulk_action():
    action = request.form.get("action")
    ids_str = request.form.get("ids")
    if not ids_str:
        return jsonify({"error": "No renderings selected."}), 400
    ids = json.loads(ids_str)
    
    conn = get_db()
    cur = conn.cursor()
    user_id = session["user_id"]

    if action == "delete":
        q_marks = ",".join("?" for _ in ids)
        cur.execute(f"SELECT image_path FROM renderings WHERE id IN ({q_marks}) AND user_id = ?", (*ids, user_id))
        paths = [row["image_path"] for row in cur.fetchall()]
        for rel in paths:
            try:
                (STATIC_DIR / rel).unlink(missing_ok=True)
            except Exception as e:
                print(f"Error deleting file: {e}")
        cur.execute(f"DELETE FROM renderings WHERE id IN ({q_marks}) AND user_id = ?", (*ids, user_id))
        conn.commit()
        conn.close()
        return jsonify({"message": f"Deleted {len(ids)} rendering(s)."}), 200

    elif action in ("like", "favorite"):
        field = "liked" if action == "like" else "favorited"
        q_marks = ",".join("?" for _ in ids)
        cur.execute(f"SELECT id, {field} FROM renderings WHERE id IN ({q_marks}) AND user_id = ?", (*ids, user_id))
        rows = cur.fetchall()
        # Toggle the value
        updates = []
        for row in rows:
            new_val = 1 - row[field]
            updates.append((new_val, row['id']))
        
        cur.executemany(f"UPDATE renderings SET {field} = ? WHERE id = ?", updates)
        conn.commit()
        conn.close()
        return jsonify({"message": f"Updated {len(ids)} rendering(s)."}), 200

    elif action == "email":
        to_email = request.form.get("to_email")
        if not to_email:
            conn.close()
            return jsonify({"error": "Please provide a destination email."}), 400

        q_marks = ",".join("?" for _ in ids)
        cur.execute(f"SELECT image_path, liked FROM renderings WHERE id IN ({q_marks}) AND user_id = ?", (*ids, user_id))
        rows = cur.fetchall()
        send_paths = [r["image_path"] for r in rows if r["liked"]]
        conn.close()

        if not send_paths:
            return jsonify({"error": "Only 'Liked' renderings can be emailed. None of the selected were liked."}), 400
        try:
            send_email_with_images(
                to_email,
                subject=f"{APP_NAME}: Selected Renderings",
                body="Here are the renderings you requested.",
                image_paths=send_paths
            )
            return jsonify({"message": f"Emailed {len(send_paths)} rendering(s) to {to_email}."}), 200
        except Exception as e:
            return jsonify({"error": f"Email failed: {e}"}), 500

    elif action == "download":
        q_marks = ",".join("?" for _ in ids)
        cur.execute(f"SELECT image_path, liked FROM renderings WHERE id IN ({q_marks}) AND user_id=?", (*ids, user_id))
        rows = cur.fetchall()
        conn.close()
        liked_paths = [r["image_path"] for r in rows if r["liked"]]
        if not liked_paths:
            return jsonify({"error": "Only 'Liked' renderings can be downloaded."}), 400
        urls = [url_for("static", filename=p) for p in liked_paths]
        return jsonify({"download_urls": urls})

    else:
        conn.close()
        return jsonify({"error": "Unknown action."}), 400


@app.get("/slideshow")
@login_required
def slideshow():
    user_id = session["user_id"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM renderings WHERE favorited = 1 AND user_id = ? ORDER BY created_at DESC", (user_id,))
    items = [dict(r) for r in cur.fetchall()]
    conn.close()
    if len(items) < 2:
        flash("Favorite at least two renderings to start a slideshow.", "info")
        return redirect(url_for("gallery"))
    return render_template("slideshow.html", app_name=APP_NAME, user=current_user(), items=items)

@app.post("/modify_rendering/<int:rid>")
@login_required
def modify_rendering(rid):
    """Take an existing rendering, apply new description/options, regenerate."""
    description = request.form.get("description", "")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM renderings WHERE id=? AND user_id=?", (rid, session["user_id"]))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Rendering not found."}), 404

    subcategory = row["subcategory"]
    original_options = json.loads(row["options_json"] or "{}")
    
    selected = {}
    if subcategory in OPTIONS:
        for opt_name in OPTIONS[subcategory].keys():
            # Use new value from form, or fall back to original, or None
            selected[opt_name] = request.form.get(opt_name) or original_options.get(opt_name)

    prompt = build_prompt(subcategory, selected, description, False)
    try:
        rel_path = generate_image_via_openai(prompt)
    except Exception as e:
        return jsonify({"error": f"Modification failed: {e}"}), 500

    now = datetime.utcnow().isoformat()
    cur.execute("""
        INSERT INTO renderings (user_id, category, subcategory, options_json, prompt, image_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (row["user_id"], row["category"], subcategory, json.dumps(selected), prompt, rel_path, now))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    
    return jsonify({
        "id": new_id,
        "path": url_for('static', filename=rel_path),
        "subcategory": subcategory,
        "message": f"Modified {subcategory} rendering!"
    })

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
        return redirect(url_for("gallery"))
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
            return redirect(nxt or url_for("gallery"))
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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ app_name }}</title>
  <link rel="stylesheet" href="{{ url_for('static', filename='app.css') }}">
</head>
<body class="page">
  <header class="topbar">
    <a class="brand" href="{{ url_for('index') }}">{{ app_name }}</a>
    <nav class="nav">
      {% if user %}
        <a href="{{ url_for('gallery') }}">My Gallery</a>
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
      {% if messages %}
          {% for cat,msg in messages %}
            <div class="flash {{ cat }}">{{ msg }}</div>
          {% endfor %}
      {% endif %}
    {% endwith %}
    </div>
    {% block content %}{% endblock %}
  </main>
  <footer class="footer">
    <small>&copy; 2025 {{ app_name }}</small>
  </footer>
  <script src="{{ url_for('static', filename='app.js') }}"></script>
</body>
</html>
""", encoding="utf-8")

    # index.html
    (TEMPLATES_DIR / "index.html").write_text("""{% extends "layout.html" %}{% block content %}
<div class="hero">
  <h1>Design Your Dream Home with AI</h1>
  <p>Bring your vision to life. Describe your ideal home, and our AI will generate stunning, photorealistic renderings in moments.</p>
</div>
<form class="card" action="{{ url_for('generate') }}" method="post" enctype="multipart/form-data">
  <h2>1. Describe Your Home</h2>
  <textarea id="description" name="description" rows="4" placeholder="e.g., A two-story modern farmhouse with a wrap-around porch, black metal roof, and large windows. Include a lush garden and a stone pathway..."></textarea>
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
{% endblock %}
""", encoding="utf-8")

    # gallery.html
    (TEMPLATES_DIR / "gallery.html").write_text("""{% extends "layout.html" %}{% block content %}
<h1>My Renderings</h1>

<!-- Bulk Actions -->
<div class="card bulk-actions">
    <div class="row space">
        <div>
            <label><input type="checkbox" id="selectAll"> Select All</label>
            <button id="likeBtn">❤️ Like</button>
            <button id="favBtn">⭐ Favorite</button>
            <button id="deleteBtn">🗑️ Delete</button>
            <button id="downloadBtn">📥 Download Liked</button>
        </div>
        <div class="row gap">
            <input type="email" id="emailInput" placeholder="recipient@example.com">
            <button id="emailBtn">📧 Email Liked</button>
        </div>
    </div>
    {% if show_slideshow %}
    <a href="{{ url_for('slideshow') }}" class="button primary">▶️ View Favorites Slideshow</a>
    {% endif %}
</div>

<!-- Renderings Grid -->
<div id="renderingsGrid" class="grid">
    {% for r in items %}
    <div class="render-card" data-id="{{ r['id'] }}">
        <input type="checkbox" name="rendering_id" class="rendering-checkbox">
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
                    <textarea name="description" rows="2" placeholder="Describe changes... e.g., 'make the walls light gray'"></textarea>
                    {% if options[r['subcategory']] %}
                    <div class="options-grid">
                      {% for opt, vals in options[r['subcategory']].items() %}
                      <label>{{ opt }}
                        <select name="{{ opt }}">
                          {# --- FIX --- Use the pre-parsed 'options_dict' and .get() for safety #}
                          {% set current_val = r['options_dict'].get(opt) %}
                          <option value="">-- Default --</option>
                          {% for v in vals %}
                          <option value="{{ v }}" {% if v == current_val %}selected{% endif %}>{{ v }}</option>
                          {% endfor %}
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
    {% endfor %}
</div>

<!-- Room Generation -->
<div class="card">
    <h2>Generate a New Room</h2>
    <form id="generateRoomForm">
        <select id="roomSelect" name="subcategory">
            {% for room in rooms %}
            <option value="{{ room }}">{{ room }}</option>
            {% endfor %}
        </select>
        <div id="roomOptionsContainer"></div>
        <button type="submit" class="primary">Generate Room</button>
    </form>
</div>

<!-- Modal -->
<div id="imageModal" class="modal"><span class="close-modal">&times;</span><img class="modal-content" id="modalImg"></div>

<script>
    const ROOM_OPTIONS = {{ options | tojson }};
</script>
{% endblock %}
""", encoding="utf-8")

    # slideshow.html
    (TEMPLATES_DIR / "slideshow.html").write_text("""{% extends "layout.html" %}{% block content %}
<div class="slideshow-container">
  <h1>Favorites Slideshow</h1>
  <div class="slideshow">
    {% for r in items %}
      <div class="slide" {% if not loop.first %}style="display:none;"{% endif %}>
        <img src="{{ url_for('static', filename=r['image_path']) }}" alt="{{ r['subcategory'] }}">
        <div class="caption">{{ r['subcategory'] }}</div>
      </div>
    {% endfor %}
  </div>
  <div class="row gap center">
    <button id="prev" class="button">❮ Prev</button>
    <a href="{{ url_for('gallery') }}" class="button">Back to Gallery</a>
    <button id="toggleDark" class="button">Toggle Dark 🌙</button>
    <button id="next" class="button">Next ❯</button>
  </div>
</div>
<script>
  const slides=[...document.querySelectorAll('.slide')];
  let idx=0;
  function show(i){slides.forEach((s,j)=>s.style.display=(i===j?'block':'none'));}
  document.getElementById('prev').onclick=()=>{idx=(idx-1+slides.length)%slides.length;show(idx);}
  document.getElementById('next').onclick=()=>{idx=(idx+1)%slides.length;show(idx);}
  document.getElementById('toggleDark').onclick=()=>{
      const currentImg = slides[idx].querySelector('img');
      if (currentImg) currentImg.classList.toggle('dark');
  };
</script>
{% endblock %}
""", encoding="utf-8")

    # login.html
    (TEMPLATES_DIR / "login.html").write_text("""{% extends "layout.html" %}{% block content %}
<div class="auth-form">
  <h1>Login</h1>
  <form class="card" method="post">
    <label>Email</label>
    <input type="email" name="email" required>
    <label>Password</label>
    <input type="password" name="password" required>
    <button class="primary">Login</button>
  </form>
  <p>No account? <a href="{{ url_for('register') }}">Register here</a>.</p>
</div>
{% endblock %}
""", encoding="utf-8")

    # register.html
    (TEMPLATES_DIR / "register.html").write_text("""{% extends "layout.html" %}{% block content %}
<div class="auth-form">
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
</div>
{% endblock %}
""", encoding="utf-8")

def write_basic_static_if_missing():
    """Create minimal static assets safely."""
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    
    (STATIC_DIR / "app.css").write_text("""
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
.center { justify-content: center; }
.space { justify-content: space-between; }
input, textarea, select { width: 100%; padding: 0.75rem; border: 1px solid var(--border); border-radius: 6px; margin-bottom: 1rem; box-sizing: border-box; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 1rem; }
.render-card { position: relative; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.render-img { width: 100%; height: auto; display: block; aspect-ratio: 1/1; object-fit: cover; cursor: pointer; }
.render-img.dark { filter: invert(1) hue-rotate(180deg); }
.meta { display: flex; justify-content: space-between; align-items: center; padding: 0.5rem; }
.actions { display: flex; gap: 0.5rem; }
.action-btn { background: none; border: none; font-size: 1.2rem; cursor: pointer; padding: 0; }
.action-btn.active { color: #ff5252; } /* Example for liked/favorited */
.fav-btn.active { color: #fdd835; }
.dark-toggle { font-size: 1.2rem; }
.rendering-checkbox { position: absolute; top: 10px; left: 10px; width: 20px; height: 20px; }
.modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(0,0,0,0.9); }
.modal-content { margin: auto; display: block; max-width: 90%; max-height: 90%; }
.close-modal { position: absolute; top: 15px; right: 35px; color: #f1f1f1; font-size: 40px; font-weight: bold; cursor: pointer; }
.slideshow-container { text-align: center; }
.slide { display: none; }
.slide img { max-width: 100%; max-height: 70vh; border-radius: 8px; }
.flash { padding: 1rem; margin-bottom: 1rem; border-radius: 6px; }
.flash.success { background-color: #c6f6d5; color: #22543d; }
.flash.danger { background-color: #fed7d7; color: #822727; }
.flash.warning { background-color: #feebc8; color: #9c4221; }
.flash.info { background-color: #bee3f8; color: #2c5282; }
.options-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; }
.modify-section details { margin-top: 0.5rem; }
.modify-section summary { cursor: pointer; font-weight: bold; }
.modify-form { padding-top: 1rem; }
.auth-form { max-width: 400px; margin: 2rem auto; }
.file-label input[type="file"] { display: none; }
.file-label span { border: 1px solid #ccc; padding: 0.75rem 1rem; border-radius: 6px; cursor: pointer; background: #f9f9f9; }
    """, encoding="utf-8")

    (STATIC_DIR / "app.js").write_text("""
document.addEventListener('DOMContentLoaded', function() {
    // --- MODAL ---
    const modal = document.getElementById('imageModal');
    if (modal) {
        document.addEventListener('click', e => {
            if (e.target.classList.contains('modal-trigger')) {
                modal.style.display = 'block';
                document.getElementById('modalImg').src = e.target.src;
            }
            if (e.target.classList.contains('close-modal')) {
                modal.style.display = 'none';
            }
        });
        window.addEventListener('click', e => {
            if (e.target === modal) {
                modal.style.display = 'none';
            }
        });
    }

    // --- VOICE PROMPT ---
    const voiceBtn = document.getElementById('voiceBtn');
    const description = document.getElementById('description');
    if (voiceBtn && description) {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (SpeechRecognition) {
            const recognition = new SpeechRecognition();
            recognition.onresult = (event) => {
                description.value = event.results[0][0].transcript;
            };
            voiceBtn.addEventListener('click', () => recognition.start());
        } else {
            voiceBtn.style.display = 'none';
        }
    }
    
    // --- GALLERY PAGE ---
    if (document.getElementById('renderingsGrid')) {
        const grid = document.getElementById('renderingsGrid');
        const selectAll = document.getElementById('selectAll');

        // Individual card actions (Like, Fav, Dark Mode)
        grid.addEventListener('click', e => {
            const card = e.target.closest('.render-card');
            if (!card) return;
            const id = card.dataset.id;

            if (e.target.classList.contains('like-btn') || e.target.classList.contains('fav-btn')) {
                const action = e.target.classList.contains('like-btn') ? 'like' : 'favorite';
                handleBulkAction(action, [id]).then(() => {
                    e.target.classList.toggle('active');
                });
            } else if (e.target.classList.contains('dark-toggle')) {
                card.querySelector('.render-img').classList.toggle('dark');
            }
        });
        
        // Select All
        selectAll.addEventListener('change', e => {
            document.querySelectorAll('.rendering-checkbox').forEach(cb => cb.checked = e.target.checked);
        });

        // Bulk action buttons
        setupBulkActionBtn('likeBtn', 'like');
        setupBulkActionBtn('favBtn', 'favorite');
        setupBulkActionBtn('deleteBtn', 'delete', true);
        setupBulkActionBtn('downloadBtn', 'download');
        setupBulkActionBtn('emailBtn', 'email');

        // Modify Rendering Form Submission
        document.querySelectorAll('.modify-form').forEach(form => {
            form.addEventListener('submit', async e => {
                e.preventDefault();
                const id = form.dataset.id;
                const formData = new FormData(form);
                const button = form.querySelector('button');
                button.textContent = 'Generating...';
                button.disabled = true;

                try {
                    const response = await fetch(`/modify_rendering/${id}`, { method: 'POST', body: formData });
                    const result = await response.json();
                    if (!response.ok) throw new Error(result.error);
                    showFlash(result.message, 'success');
                    // Add new card to grid
                    const newCard = createRenderCard(result.id, result.path, result.subcategory);
                    grid.insertAdjacentElement('afterbegin', newCard);
                } catch (error) {
                    showFlash(error.message, 'danger');
                } finally {
                    button.textContent = 'Regenerate';
                    button.disabled = false;
                }
            });
        });
        
        // Generate New Room
        const roomForm = document.getElementById('generateRoomForm');
        const roomSelect = document.getElementById('roomSelect');
        const roomOptionsContainer = document.getElementById('roomOptionsContainer');
        
        function updateRoomOptions() {
            const subcategory = roomSelect.value;
            const options = ROOM_OPTIONS[subcategory];
            roomOptionsContainer.innerHTML = '';
            if (options) {
                const container = document.createElement('div');
                container.className = 'options-grid';
                for (const [opt, vals] of Object.entries(options)) {
                    const label = document.createElement('label');
                    label.textContent = opt;
                    const select = document.createElement('select');
                    select.name = opt;
                    vals.forEach(v => {
                        const option = document.createElement('option');
                        option.value = v;
                        option.textContent = v;
                        select.appendChild(option);
                    });
                    label.appendChild(select);
                    container.appendChild(label);
                }
                roomOptionsContainer.appendChild(container);
            }
        }
        
        roomSelect.addEventListener('change', updateRoomOptions);
        updateRoomOptions();

        roomForm.addEventListener('submit', async e => {
            e.preventDefault();
            const formData = new FormData(roomForm);
            const button = roomForm.querySelector('button');
            button.textContent = 'Generating...';
            button.disabled = true;

            try {
                const response = await fetch('/generate_room', { method: 'POST', body: formData });
                const result = await response.json();
                if (!response.ok) throw new Error(result.error);
                showFlash(result.message, 'success');
                const newCard = createRenderCard(result.id, result.path, result.subcategory);
                grid.insertAdjacentElement('afterbegin', newCard);
            } catch (error) {
                showFlash(error.message, 'danger');
            } finally {
                button.textContent = 'Generate Room';
                button.disabled = false;
            }
        });
    }
});

function setupBulkActionBtn(btnId, action, reload = false) {
    const btn = document.getElementById(btnId);
    if(btn) {
        btn.addEventListener('click', () => {
            const ids = getSelectedIds();
            if (ids.length > 0) {
                handleBulkAction(action, ids, reload);
            } else {
                showFlash('Please select one or more renderings.', 'warning');
            }
        });
    }
}

async function handleBulkAction(action, ids, reload = false) {
    const body = new FormData();
    body.append('action', action);
    body.append('ids', JSON.stringify(ids));

    if (action === 'email') {
        const email = document.getElementById('emailInput').value;
        if (!email) {
            showFlash('Please enter an email address.', 'warning');
            return;
        }
        body.append('to_email', email);
    }
    
    try {
        const response = await fetch('/bulk_action', { method: 'POST', body: body });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error);
        
        if (action === 'download') {
            result.download_urls.forEach(url => {
                const a = document.createElement('a');
                a.href = url;
                a.download = url.split('/').pop();
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
            });
        }
        
        showFlash(result.message || 'Action completed!', 'success');
        if (reload) {
            window.location.reload();
        }
    } catch (error) {
        showFlash(error.message, 'danger');
    }
}

function getSelectedIds() {
    return [...document.querySelectorAll('.rendering-checkbox:checked')]
        .map(cb => cb.closest('.render-card').dataset.id);
}

function showFlash(message, category) {
    const container = document.getElementById('flash-container');
    const flash = document.createElement('div');
    flash.className = `flash ${category}`;
    flash.textContent = message;
    container.prepend(flash);
    setTimeout(() => flash.remove(), 5000);
}

function createRenderCard(id, path, subcategory) {
    const card = document.createElement('div');
    card.className = 'render-card';
    card.dataset.id = id;
    card.innerHTML = `
        <input type="checkbox" class="rendering-checkbox">
        <img src="${path}" alt="${subcategory}" class="render-img modal-trigger">
        <div class="meta">
            <span class="tag">${subcategory}</span>
            <div class="actions">
                <button class="action-btn like-btn" title="Like">❤️</button>
                <button class="action-btn fav-btn" title="Favorite">⭐</button>
                <button class="action-btn dark-toggle" title="Toggle Dark Mode">🌙</button>
            </div>
        </div>
        <div class="modify-section">
            <details><summary>Modify This Rendering</summary>...omitted for brevity...</details>
        </div>
    `;
    return card;
}
    """, encoding="utf-8")
    
    ico = STATIC_DIR / "favicon.ico"
    if not ico.exists():
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (32, 32))
        d = ImageDraw.Draw(img)
        d.rectangle([4, 4, 28, 28], fill="#4a6dff")
        d.text((8, 8), "A3D", fill="#fff")
        img.save(ico, format="ICO")


# ---------- Main ----------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
