import os
import io
import base64
import zipfile
import sqlite3
from contextlib import closing
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, redirect, url_for, render_template_string, session,
    send_file, flash, jsonify, abort
)
from werkzeug.security import generate_password_hash, check_password_hash

# --- OpenAI (Images) ---
try:
    from openai import OpenAI
    openai_client = OpenAI()
except Exception as e:  # Allow the app to boot even if SDK isn't installed yet
    openai_client = None

# -----------------------------
# Config
# -----------------------------
APP_NAME = "Architect 3D Home Modeler"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "architect3d.db")
IMAGES_DIR = os.path.join(BASE_DIR, "static", "images")
os.makedirs(IMAGES_DIR, exist_ok=True)

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", SMTP_USER)

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")

# -----------------------------
# Flask App
# -----------------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY

# -----------------------------
# DB Helpers
# -----------------------------
SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS renderings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  title TEXT,
  category TEXT NOT NULL, -- exterior_front, exterior_back, living_room, kitchen, etc
  prompt TEXT NOT NULL,
  image_path TEXT NOT NULL,
  liked INTEGER DEFAULT 0,
  favorited INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id)
);
"""

def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.executescript(SCHEMA_SQL)
        conn.commit()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# -----------------------------
# Auth Utilities
# -----------------------------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            flash("Please sign in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with closing(get_db()) as db:
        cur = db.execute("SELECT * FROM users WHERE id = ?", (uid,))
        return cur.fetchone()


# -----------------------------
# OpenAI Image Generation
# -----------------------------

def generate_image_b64(prompt: str, size: str = "1024x1024") -> bytes:
    """Generate an image with OpenAI Images API, return raw bytes.
    Requires OPENAI_API_KEY env var and openai SDK installed.
    """
    if openai_client is None:
        raise RuntimeError("OpenAI SDK not installed. Add 'openai' to requirements.txt")

    # gpt-image-1 returns base64 data
    resp = openai_client.images.generate(
        model="gpt-image-1",
        prompt=prompt,
        size=size
    )
    b64 = resp.data[0].b64_json
    return base64.b64decode(b64)


def save_image(image_bytes: bytes, filename_prefix: str = "render") -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    filename = f"{filename_prefix}_{ts}.png"
    path = os.path.join(IMAGES_DIR, filename)
    with open(path, "wb") as f:
        f.write(image_bytes)
    # Return relative path for serving
    return os.path.join("static", "images", filename)


# -----------------------------
# Room / Option Catalogs (5 types each where applicable)
# -----------------------------
ROOM_CATEGORIES = [
    "Living Room", "Kitchen", "Home Office",
    "Primary Bedroom", "Primary Bathroom",
    "Bedroom2", "Bedroom3", "Family Room",
]

BASEMENT_EXTRA_ROOMS = {
    "Basement w/ Bar": {},
    "Theater Room": {},
    "Exercise Room": {},
    "Steam Room": {},
    "Game Room": {},
    "Gym": {},
    "Basement Hallway": {},
}

OPTIONS = {
    "Front Exterior": {
        "Siding Material": ["Brick", "Stone", "Stucco", "Wood", "Fiber Cement"],
        "Roof Style": ["Gable", "Hip", "Flat", "Mansard", "Shed"],
        "Window Trim Color": ["Black", "White", "Bronze", "Gray", "Natural Wood"],
        "Landscaping": ["Modern", "Lush", "Xeriscape", "Minimalist", "Cottage"],
        "Vehicle": ["SUV", "Sedan", "Truck", "EV", "No Vehicle"],
        "Driveway Material": ["Concrete", "Pavers", "Asphalt", "Gravel", "Stamped Concrete"],
        "Driveway Shape": ["Straight", "Curved", "Circular", "Side Entry", "Split"],
        "Gate Style": ["Modern", "Wrought Iron", "Wood", "No Gate", "Hedge"],
        "Garage Style": ["Two-Car", "Three-Car", "Carriage", "Glass Panel", "Side Entry"],
    },
    "Back Exterior": {
        "Siding Material": ["Brick", "Stone", "Stucco", "Wood", "Fiber Cement"],
        "Roof Style": ["Gable", "Hip", "Flat", "Mansard", "Shed"],
        "Window Trim Color": ["Black", "White", "Bronze", "Gray", "Natural Wood"],
        "Landscaping": ["Modern", "Lush", "Xeriscape", "Minimalist", "Cottage"],
        "Swimming Pool": ["Rectangular", "Freeform", "Lap", "Infinity", "No Pool"],
        "Paradise Grills": ["Straight Island", "L-Shaped", "U-Shaped", "Pergola", "None"],
        "Basketball Court": ["Half Court", "Key Only", "Portable Hoop", "Full Court", "None"],
        "Water Fountain": ["Tiered", "Modern Bowl", "Wall", "Pond", "None"],
        "Putting Green": ["Small", "Medium", "Large", "Undulating", "None"],
    },
    "Living Room": {
        "Flooring": ["Hardwood", "Polished Concrete", "Large Tile", "Carpet", "Luxury Vinyl"],
        "Wall Color": ["Warm White", "Greige", "Soft Gray", "Navy Accent", "Sage"],
        "Lighting": ["Recessed", "Linear Pendant", "Chandeliers", "Floor Lamps", "Wall Sconces"],
        "Furniture Style": ["Modern", "Transitional", "Mid-Century", "Traditional", "Scandinavian"],
        "Chairs": ["Lounge", "Accent", "Club", "Wingback", "Recliner"],
        "Coffee Tables": ["Glass", "Wood", "Marble", "Nesting", "Lift-Top"],
        "Wine Storage": ["Built-In Wall", "Credenza", "Under-Stairs", "Display Rack", "Hidden"],
        "Fireplace": ["Yes", "No"],
        "Door Style": ["French", "Pocket", "Barn", "Sliding", "Standard"],
    },
    "Kitchen": {
        "Flooring": ["Hardwood", "Tile", "Polished Concrete", "Cork", "Luxury Vinyl"],
        "Wall Color": ["Warm White", "Cool White", "Pale Gray", "Soft Green", "Clay"],
        "Lighting": ["Recessed", "Island Pendants", "Under-Cabinet", "Track", "Chandelier"],
        "Cabinet Style": ["Shaker", "Flat Panel", "Inset", "Beadboard", "Glass Front"],
        "Countertops": ["Quartz", "Marble", "Granite", "Butcher Block", "Concrete"],
        "Appliances": ["Stainless", "Panel-Ready", "Black Steel", "Mixed Metals", "Retro"],
        "Backsplash": ["Subway Tile", "Slab", "Herringbone", "Mosaic", "Zellige"],
        "Sink": ["Farmhouse", "Undermount", "Integrated", "Trough", "Double Bowl"],
        "Island Lights": ["3 Pendants", "Single Linear", "Flush Mounts", "Track", "Chandelier"],
    },
    "Home Office": {
        "Flooring": ["Hardwood", "Carpet", "Cork", "Luxury Vinyl", "Tile"],
        "Wall Color": ["Muted Blue", "Deep Green", "Gray", "Warm White", "Taupe"],
        "Lighting": ["Task Lamps", "Recessed", "Pendant", "Wall Washers", "Track"],
        "Desk Style": ["Standing", "Executive", "Wall-Mounted", "L-Shaped", "Minimalist"],
        "Office Chair": ["Ergonomic Mesh", "Executive Leather", "Task Chair", "Kneeling", "Stool"],
        "Storage": ["Built-ins", "Shelving", "Credenza", "Cabinets", "Closet"],
    },
    "Primary Bedroom": {
        "Flooring": ["Carpet", "Hardwood", "Luxury Vinyl", "Cork", "Bamboo"],
        "Wall Color": ["Warm White", "Muted Blue", "Dusty Rose", "Sage", "Charcoal"],
        "Lighting": ["Chandelier", "Sconces", "Recessed", "Lamps", "Cove"],
        "Bed Style": ["Upholstered", "Wood Platform", "Canopy", "Sleigh", "Storage"],
        "Furniture Style": ["Modern", "Transitional", "Traditional", "Minimal", "Rustic"],
        "Closet Design": ["Walk-In", "Reach-In", "Open System", "Island", "Wardrobe"],
        "Ceiling Fan": ["Yes", "No", "With Light", "Large", "Low-Profile"],
    },
    "Primary Bathroom": {
        "Flooring": ["Large Tile", "Marble", "Porcelain", "Natural Stone", "Heated"],
        "Wall Color": ["Warm White", "Pale Gray", "Greige", "Muted Green", "Taupe"],
        "Lighting": ["Sconces", "Recessed", "Cove", "Pendant", "Mirror Lights"],
        "Vanity Style": ["Floating", "Furniture-Style", "Double", "Open Shelf", "Integrated"],
        "Shower/Tub": ["Large Shower", "Freestanding Tub", "Shower + Tub", "Steam Shower", "Wet Room"],
        "Tile Style": ["Subway", "Large-Format", "Herringbone", "Mosaic", "Terrazzo"],
        "Sink": ["Vessel", "Undermount", "Integrated", "Double", "Console"],
        "Mirror Style": ["Round", "Rectangular", "Backlit", "Arched", "Framed"],
        "Balcony": ["Yes", "No"],
    },
    "Other Bedrooms": {
        "Flooring": ["Carpet", "Hardwood", "Luxury Vinyl", "Cork", "Bamboo"],
        "Wall Color": ["Warm White", "Muted Blue", "Soft Gray", "Sage", "Clay"],
        "Lighting": ["Chandelier", "Sconces", "Recessed", "Lamps", "Pendant"],
        "Bed Style": ["Upholstered", "Wood Platform", "Bunk", "Daybed", "Storage"],
        "Furniture Style": ["Modern", "Transitional", "Traditional", "Minimal", "Rustic"],
        "Ceiling Fan": ["Yes", "No", "With Light", "Large", "Low-Profile"],
        "Balcony": ["Yes", "No"],
    },
    "Half Bath": {
        "Flooring": ["Tile", "Marble", "Concrete", "Luxury Vinyl", "Stone"],
        "Wall Color": ["Warm White", "Deep Blue", "Sage", "Blush", "Charcoal"],
        "Lighting": ["Sconces", "Pendant", "Recessed", "Strip", "Mirror Lights"],
        "Vanity Style": ["Floating", "Pedestal", "Console", "Furniture", "Integrated"],
        "Tile Style": ["Subway", "Mosaic", "Large-Format", "Herringbone", "Zellige"],
        "Mirror Style": ["Round", "Rectangular", "Backlit", "Arched", "Framed"],
    },
    "Game Room": {
        "Flooring": ["Carpet Tile", "Rubber", "Luxury Vinyl", "Laminate", "Epoxy"],
        "Wall Color": ["Charcoal", "Navy", "Red", "Gray", "White"],
        "Lighting": ["LED Strips", "Pendant", "Track", "Recessed", "Neon"],
        "Pool Table": ["Black", "Oak", "Walnut", "White", "Industrial"],
        "Wine Bar": ["Backlit", "Stone", "Wood", "Glass", "Concrete"],
        "Arcade Games": ["Racing", "Fighting", "Pinball", "Shooter", "Retro"],
        "Other Table Games": ["Air Hockey", "Foosball", "Table Tennis", "Shuffleboard", "Cards"],
    },
    "Gym": {
        "Flooring": ["Rubber", "Foam", "Cork", "Vinyl", "Carpet Tile"],
        "Wall Color": ["White", "Gray", "Blue", "Black", "Green"],
        "Lighting": ["Bright LED", "Track", "Recessed", "Wall Washers", "Mirror Lights"],
        "Equipment": ["Racks", "Cardio", "Free Weights", "Cable Machine", "Kettlebells"],
        "Gym Station": ["Power Rack", "Smith Machine", "Functional Trainer", "Pilates", "Rowing"],
        "Steam Room": ["Yes", "No"],
    },
    "Theater Room": {
        "Flooring": ["Carpet", "Acoustic Wood", "Luxury Vinyl", "Cork", "Risers"],
        "Wall Color": ["Black", "Deep Navy", "Burgundy", "Charcoal", "Chocolate"],
        "Lighting": ["Sconces", "LED Strips", "Step Lights", "Recessed", "Fiber Optic Ceiling"],
        "Wall Treatment": ["Acoustic Panels", "Fabric", "Wood Slats", "Foam", "Wallpaper"],
        "Seating": ["Recliners", "Sofas", "Loveseats", "Chaise", "Stadium"],
        "Popcorn Machine": ["Yes", "No"],
        "Sound System": ["5.1", "7.1", "Atmos", "Soundbar", "Hidden"],
        "Screen Type": ["Projector", "OLED", "MicroLED", "Ultra Short Throw", "Acoustic Screen"],
        "Movie Posters": ["Yes", "No"],
        "Show Movie": ["Yes", "No"],
    },
    "Basement Hallway": {
        "Flooring": ["Tile", "Carpet", "Luxury Vinyl", "Concrete", "Wood"],
        "Wall Color": ["White", "Gray", "Taupe", "Blue", "Green"],
        "Lighting": ["Sconces", "Recessed", "Cove", "Track", "Wall Washers"],
        "Stairs": ["Open Riser", "Carpeted", "Wood", "Glass Rail", "Metal"],
    },
}


# -----------------------------
# Templates (inline for single-file deploy)
# -----------------------------
BASE_HTML = r"""
<!doctype html>
<html lang="en" class="h-full" data-theme="light">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{{ title or app_name }}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
      function toggleTheme(){
        const html = document.documentElement;
        const current = html.getAttribute('data-theme') || 'light';
        const next = current === 'light' ? 'dark' : 'light';
        html.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
        document.body.classList.toggle('bg-gray-950');
        document.body.classList.toggle('text-gray-100');
      }
      document.addEventListener('DOMContentLoaded',()=>{
        const saved = localStorage.getItem('theme');
        if(saved==='dark'){ toggleTheme(); }
      });
      // Voice input using Web Speech API
      function startVoice(targetId){
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if(!SpeechRecognition){ alert('Voice input not supported in this browser.'); return; }
        const recog = new SpeechRecognition();
        recog.lang = 'en-US';
        recog.onresult = (e)=>{
          const text = e.results[0][0].transcript;
          const el = document.getElementById(targetId);
          el.value = (el.value? el.value+' ' : '') + text;
        };
        recog.start();
      }
    </script>
  </head>
  <body class="min-h-screen bg-white">
    <div class="max-w-7xl mx-auto p-4">
      <header class="flex items-center justify-between py-4">
        <h1 class="text-2xl font-bold">{{ app_name }}</h1>
        <div class="flex items-center gap-2">
          <button onclick="toggleTheme()" class="px-3 py-1 rounded-xl border">Toggle Dark Mode</button>
          {% if user %}
            <span class="text-sm">{{ user['email'] }}</span>
            <a href="{{ url_for('logout') }}" class="px-3 py-1 rounded-xl border">Logout</a>
          {% else %}
            <a href="{{ url_for('login') }}" class="px-3 py-1 rounded-xl border">Login</a>
            <a href="{{ url_for('register') }}" class="px-3 py-1 rounded-xl border">Register</a>
          {% endif %}
        </div>
      </header>
      {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}
          <div class="space-y-2">
            {% for cat,msg in messages %}
              <div class="p-3 rounded-xl border {{ 'bg-green-50' if cat=='success' else 'bg-yellow-50' }}">{{ msg }}</div>
            {% endfor %}
          </div>
        {% endif %}
      {% endwith %}
      {% block content %}{% endblock %}
    </div>
  </body>
</html>
"""

INDEX_HTML = r"""
{% extends 'base.html' %}
{% block content %}
  <section class="grid md:grid-cols-2 gap-6">
    <form action="{{ url_for('generate') }}" method="post" enctype="multipart/form-data" class="p-4 rounded-2xl border">
      <h2 class="text-xl font-semibold mb-2">Describe Your Dream Home</h2>
      <textarea id="home_desc" name="home_desc" rows="5" class="w-full border rounded-xl p-3" placeholder="e.g., Modern 2-story with 4 bedrooms, walkout basement, theater room, and a backyard pool."></textarea>
      <div class="mt-2">
        <button type="button" onclick="startVoice('home_desc')" class="px-3 py-1 rounded-xl border">🎤 Voice Prompt</button>
      </div>
      <div class="my-4">
        <label class="block font-medium">Upload Architectural Plan (optional)</label>
        <input type="file" name="plan_file" accept="image/*,.pdf" class="mt-1" />
      </div>
      <div class="flex items-center gap-3">
        <button class="px-4 py-2 rounded-2xl bg-black text-white">Generate House Plan</button>
        <a href="{{ url_for('gallery') }}" class="px-4 py-2 rounded-2xl border">Go to Gallery</a>
      </div>
      <p class="text-xs text-gray-500 mt-2">Pressing Generate will create 2 exterior renderings (Front & Back). You can refine interiors via room categories below.</p>
    </form>

    <div class="p-4 rounded-2xl border">
      <h2 class="text-xl font-semibold mb-2">Room Categories</h2>
      <form id="room-form" action="{{ url_for('generate_room') }}" method="post" class="space-y-3">
        <label class="block">Select Room
          <select class="w-full border rounded-xl p-2" name="room" id="room-select">
            <option value="Front Exterior">Front Exterior</option>
            <option value="Back Exterior">Back Exterior</option>
            {% for r in base_rooms %}
              <option value="{{ r }}">{{ r }}</option>
            {% endfor %}
            {% if 'basement' in (pre_desc or '').lower() %}
              <option disabled>──────── Basement ────────</option>
              <option value="Game Room">Game Room</option>
              <option value="Gym">Gym</option>
              <option value="Theater Room">Theater Room</option>
              <option value="Basement Hallway">Basement Hallway</option>
            {% endif %}
          </select>
        </label>
        <div id="room-options" class="space-y-2"></div>
        <input type="hidden" name="home_desc" value="{{ pre_desc or '' }}" />
        <button class="px-4 py-2 rounded-2xl bg-black text-white">Generate Room Rendering</button>
      </form>
    </div>
  </section>

  <script>
    const ALL_OPTIONS = {{ options_json | safe }};
    const roomSelect = document.getElementById('room-select');
    const optionsContainer = document.getElementById('room-options');

    function renderOptions(room){
      optionsContainer.innerHTML = '';
      const opts = ALL_OPTIONS[room];
      if(!opts){ return; }
      Object.entries(opts).forEach(([label, arr])=>{
        const wrap = document.createElement('label');
        wrap.className = 'block';
        const sel = document.createElement('select');
        sel.name = label;
        sel.className = 'w-full border rounded-xl p-2';
        (arr || []).forEach(v=>{
          const o = document.createElement('option');
          o.value = v; o.textContent = v; sel.appendChild(o);
        });
        wrap.innerHTML = `<span class='font-medium'>${label}</span>`;
        wrap.appendChild(sel);
        optionsContainer.appendChild(wrap);
      });
    }

    renderOptions(roomSelect.value);
    roomSelect.addEventListener('change', e=> renderOptions(e.target.value));
  </script>
{% endblock %}
"""

GALLERY_HTML = r"""
{% extends 'base.html' %}
{% block content %}
  <div class="flex items-center justify-between mb-3">
    <h2 class="text-xl font-semibold">Your Renderings</h2>
    <div class="flex items-center gap-2">
      <form id="bulk-actions" method="post" action="{{ url_for('bulk_action') }}" class="flex items-center gap-2">
        <input type="hidden" name="ids" id="bulk-ids" />
        <select name="action" class="border rounded-xl p-2">
          <option value="like">Like</option>
          <option value="favorite">Favorite</option>
          <option value="delete">Delete</option>
          <option value="download">Download</option>
          <option value="email">Email</option>
        </select>
        <input type="email" name="email_to" class="border rounded-xl p-2" placeholder="email (for emailing)" />
        <button class="px-3 py-2 rounded-2xl bg-black text-white">Apply</button>
      </form>
      {% if favorite_count >= 2 %}
        <a href="{{ url_for('slideshow') }}" class="px-3 py-2 rounded-2xl border">▶ Slideshow</a>
      {% endif %}
    </div>
  </div>

  <form id="select-form" class="grid md:grid-cols-3 gap-4">
    {% for r in items %}
      <div class="border rounded-2xl overflow-hidden group">
        <div class="relative">
          <img src="/{{ r['image_path'] }}" alt="{{ r['title'] or r['category'] }}" class="w-full aspect-square object-cover" />
          <button type="button" onclick="toggleCardDark(this)" class="absolute top-2 right-2 px-2 py-1 text-xs bg-white/80 rounded">Dark Mode</button>
        </div>
        <div class="p-3 space-y-1">
          <label class="flex items-center gap-2">
            <input type="checkbox" value="{{ r['id'] }}" class="select-box" />
            <span class="text-sm text-gray-600">Select</span>
          </label>
          <div class="text-sm font-medium">{{ r['title'] or r['category'] }}</div>
          <div class="text-xs text-gray-500">{{ r['created_at'] }}</div>
          <div class="flex items-center gap-2 text-xs">
            <span class="px-2 py-1 rounded-full border {{ 'bg-green-100' if r['liked'] else '' }}">❤ Like</span>
            <span class="px-2 py-1 rounded-full border {{ 'bg-yellow-100' if r['favorited'] else '' }}">★ Favorite</span>
          </div>
        </div>
      </div>
    {% else %}
      <p class="text-gray-500">No renderings yet. Go to the home page and generate your first design!</p>
    {% endfor %}
  </form>

  <script>
    function toggleCardDark(btn){
      const card = btn.closest('.border');
      card.classList.toggle('invert');
      card.classList.toggle('bg-black');
    }
    const selectBoxes = document.querySelectorAll('.select-box');
    const bulkIds = document.getElementById('bulk-ids');
    const bulkForm = document.getElementById('bulk-actions');
    function updateIds(){
      const ids = Array.from(selectBoxes).filter(cb=>cb.checked).map(cb=>cb.value);
      bulkIds.value = ids.join(',');
    }
    selectBoxes.forEach(cb=> cb.addEventListener('change', updateIds));
    bulkForm.addEventListener('submit', (e)=>{
      updateIds();
      if(!bulkIds.value){ e.preventDefault(); alert('Select at least one rendering.'); }
    });
  </script>
{% endblock %}
"""

SLIDESHOW_HTML = r"""
{% extends 'base.html' %}
{% block content %}
  <h2 class="text-xl font-semibold mb-3">Favorites Slideshow</h2>
  <div id="slides" class="relative w-full max-w-4xl mx-auto">
    {% for r in items %}
      <img src="/{{ r['image_path'] }}" class="w-full rounded-2xl hidden" />
    {% endfor %}
  </div>
  <div class="flex items-center justify-center gap-2 mt-3">
    <button id="prev" class="px-3 py-2 rounded-2xl border">Prev</button>
    <button id="next" class="px-3 py-2 rounded-2xl border">Next</button>
    <button id="play" class="px-3 py-2 rounded-2xl border">Play</button>
  </div>
  <script>
    const slides = Array.from(document.querySelectorAll('#slides img'));
    let idx = 0; let timer = null;
    function show(i){ slides.forEach((img,j)=> img.classList.toggle('hidden', j!==i)); }
    function next(){ idx = (idx+1)%slides.length; show(idx); }
    function prev(){ idx = (idx-1+slides.length)%slides.length; show(idx); }
    document.getElementById('next').onclick = next;
    document.getElementById('prev').onclick = prev;
    document.getElementById('play').onclick = ()=>{
      if(timer){ clearInterval(timer); timer=null; return; }
      timer = setInterval(next, 2500);
    };
    if(slides.length){ show(0); }
  </script>
{% endblock %}
"""

AUTH_HTML = r"""
{% extends 'base.html' %}
{% block content %}
  <div class="max-w-md mx-auto p-6 rounded-2xl border">
    <h2 class="text-xl font-semibold mb-4">{{ heading }}</h2>
    <form method="post">
      <label class="block mb-2">
        <span class="text-sm">Email</span>
        <input name="email" type="email" required class="w-full border rounded-xl p-2"/>
      </label>
      <label class="block mb-4">
        <span class="text-sm">Password</span>
        <input name="password" type="password" required class="w-full border rounded-xl p-2"/>
      </label>
      <button class="px-4 py-2 rounded-2xl bg-black text-white w-full">{{ cta }}</button>
    </form>
  </div>
{% endblock %}
"""

# Register templates with Flask's loader API
app.jinja_loader.mapping = {
    'base.html': BASE_HTML,
    'index.html': INDEX_HTML,
    'gallery.html': GALLERY_HTML,
    'slideshow.html': SLIDESHOW_HTML,
    'auth.html': AUTH_HTML,
}


# -----------------------------
# Routes
# -----------------------------
@app.route('/')
def index():
    user = current_user()
    pre_desc = session.get('last_desc', '')
    return render_template_string(
        app.jinja_loader.get_source(app.jinja_env, 'index.html')[0],
        app_name=APP_NAME,
        title=APP_NAME,
        user=user,
        base_rooms=ROOM_CATEGORIES,
        pre_desc=pre_desc,
        options_json=OPTIONS,
    )


@app.post('/generate')
@login_required
def generate():
    desc = request.form.get('home_desc','').strip()
    session['last_desc'] = desc

    # Check for basement keyword to influence prompts
    has_basement = 'basement' in desc.lower()

    # Process plan file (optional) - not parsed, just noted
    plan_file = request.files.get('plan_file')
    plan_hint = ""
    if plan_file and plan_file.filename:
        plan_hint = " Architectural plan provided."

    prompts = [
        f"Front exterior architectural rendering, {desc}. Photorealistic, golden hour, ultra-detailed.{plan_hint}",
        f"Back exterior architectural rendering, {desc}. Photorealistic, high detail backyard amenities.{plan_hint}",
    ]

    created_paths = []
    for i, p in enumerate(prompts):
        try:
            img_bytes = generate_image_b64(p)
            rel = save_image(img_bytes, filename_prefix=('front' if i==0 else 'back'))
            created_paths.append((rel, 'Front Exterior' if i==0 else 'Back Exterior'))
        except Exception as e:
            app.logger.exception("Image generation failed: %s", e)
            flash(f"Image generation failed: {e}", "warning")

    # Save records
    with closing(get_db()) as db:
        for rel, cat in created_paths:
            db.execute(
                "INSERT INTO renderings (user_id, title, category, prompt, image_path, created_at) VALUES (?,?,?,?,?,?)",
                (session['user_id'], cat, cat.lower().replace(' ', '_'), desc, rel, datetime.utcnow().isoformat())
            )
        db.commit()

    if created_paths:
        flash("Generated exterior renderings!", "success")
    return redirect(url_for('gallery'))


@app.post('/generate-room')
@login_required
def generate_room():
    room = request.form.get('room')
    desc = request.form.get('home_desc','').strip()
    # Build prompt from selected options
    details = []
    for key in request.form.keys():
        if key in ("room", "home_desc"):
            continue
        val = request.form.get(key)
        if val:
            details.append(f"{key}: {val}")
    details_text = ", ".join(details)

    prompt = f"{room} interior design rendering. {desc}. Style options: {details_text}. Photorealistic, 4k, natural lighting."

    try:
        img_bytes = generate_image_b64(prompt)
        rel = save_image(img_bytes, filename_prefix=room.lower().replace(' ', ''))
        with closing(get_db()) as db:
            db.execute(
                "INSERT INTO renderings (user_id, title, category, prompt, image_path, created_at) VALUES (?,?,?,?,?,?)",
                (session['user_id'], room, room.lower().replace(' ', '_'), prompt, rel, datetime.utcnow().isoformat())
            )
            db.commit()
        flash(f"Generated {room} rendering!", "success")
    except Exception as e:
        app.logger.exception("Image generation failed: %s", e)
        flash(f"Image generation failed: {e}", "warning")

    return redirect(url_for('gallery'))


@app.get('/gallery')
@login_required
def gallery():
    with closing(get_db()) as db:
        cur = db.execute(
            "SELECT * FROM renderings WHERE user_id = ? ORDER BY created_at DESC",
            (session['user_id'],)
        )
        items = cur.fetchall()
        cur2 = db.execute(
            "SELECT COUNT(*) as c FROM renderings WHERE user_id = ? AND favorited = 1",
            (session['user_id'],)
        )
        favorite_count = cur2.fetchone()[0]
    return render_template_string(
        app.jinja_loader.get_source(app.jinja_env, 'gallery.html')[0],
        app_name=APP_NAME, title="Gallery", user=current_user(), items=items, favorite_count=favorite_count
    )


@app.post('/bulk')
@login_required
def bulk_action():
    ids_raw = request.form.get('ids','')
    action = request.form.get('action','')
    email_to = request.form.get('email_to','').strip()
    if not ids_raw:
        flash("No items selected.", "warning")
        return redirect(url_for('gallery'))
    try:
        ids = [int(x) for x in ids_raw.split(',') if x]
    except:
        flash("Invalid selection.", "warning")
        return redirect(url_for('gallery'))

    with closing(get_db()) as db:
        if action in ("like", "favorite"):
            field = 'liked' if action=='like' else 'favorited'
            placeholders = ','.join('?'*len(ids))
            db.execute(f"UPDATE renderings SET {field}=1 WHERE user_id=? AND id IN ({placeholders})", (session['user_id'], *ids))
            db.commit()
            flash(f"Marked {len(ids)} as {action}d.", "success")
        elif action == 'delete':
            placeholders = ','.join('?'*len(ids))
            cur = db.execute(f"SELECT image_path FROM renderings WHERE user_id=? AND id IN ({placeholders})", (session['user_id'], *ids))
            paths = [row['image_path'] for row in cur.fetchall()]
            db.execute(f"DELETE FROM renderings WHERE user_id=? AND id IN ({placeholders})", (session['user_id'], *ids))
            db.commit()
            # Try delete files
            for p in paths:
                fp = os.path.join(BASE_DIR, p)
                if os.path.exists(fp):
                    try: os.remove(fp)
                    except: pass
            flash(f"Deleted {len(ids)} renderings.", "success")
        elif action == 'download':
            # stream a zip
            mem = io.BytesIO()
            with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
                cur = db.execute(
                    f"SELECT id, image_path, title FROM renderings WHERE user_id=? AND id IN ({','.join('?'*len(ids))})",
                    (session['user_id'], *ids)
                )
                for row in cur.fetchall():
                    fp = os.path.join(BASE_DIR, row['image_path'])
                    if os.path.exists(fp):
                        zf.write(fp, arcname=os.path.basename(fp))
            mem.seek(0)
            return send_file(mem, as_attachment=True, download_name='renderings.zip')
        elif action == 'email':
            if not email_to:
                flash("Provide an email address.", "warning")
                return redirect(url_for('gallery'))
            try:
                send_selected_via_email(ids, email_to)
                flash(f"Emailed {len(ids)} renderings to {email_to}.", "success")
            except Exception as e:
                app.logger.exception("Email error: %s", e)
                flash(f"Email failed: {e}", "warning")
        else:
            flash("Unknown action.", "warning")
    return redirect(url_for('gallery'))


@app.get('/slideshow')
@login_required
def slideshow():
    with closing(get_db()) as db:
        cur = db.execute(
            "SELECT * FROM renderings WHERE user_id=? AND favorited=1 ORDER BY created_at DESC",
            (session['user_id'],)
        )
        items = cur.fetchall()
    return render_template_string(
        app.jinja_loader.get_source(app.jinja_env, 'slideshow.html')[0],
        app_name=APP_NAME, title="Slideshow", user=current_user(), items=items
    )


# -----------------------------
# Email Helper
# -----------------------------

def send_selected_via_email(ids, email_to):
    import smtplib
    from email.message import EmailMessage

    with closing(get_db()) as db:
        cur = db.execute(
            f"SELECT title, image_path FROM renderings WHERE user_id=? AND id IN ({','.join('?'*len(ids))})",
            (session['user_id'], *ids)
        )
        rows = cur.fetchall()

    msg = EmailMessage()
    msg['Subject'] = f"{APP_NAME} – Your Selected Renderings"
    msg['From'] = SENDER_EMAIL
    msg['To'] = email_to
    msg.set_content("Attached are your selected renderings.")

    for row in rows:
        fp = os.path.join(BASE_DIR, row['image_path'])
        if os.path.exists(fp):
            with open(fp, 'rb') as f:
                data = f.read()
            msg.add_attachment(data, maintype='image', subtype='png', filename=os.path.basename(fp))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        if SMTP_USER and SMTP_PASS:
            server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


# -----------------------------
# Auth Routes
# -----------------------------
@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        with closing(get_db()) as db:
            cur = db.execute("SELECT * FROM users WHERE email=?", (email,))
            row = cur.fetchone()
        if row and check_password_hash(row['password_hash'], password):
            session['user_id'] = row['id']
            flash("Welcome back!", "success")
            next_url = request.args.get('next') or url_for('index')
            return redirect(next_url)
        flash("Invalid credentials.", "warning")
    return render_template_string(
        app.jinja_loader.get_source(app.jinja_env, 'auth.html')[0],
        app_name=APP_NAME, title="Login", user=current_user(), heading="Sign In", cta="Sign In"
    )


@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        with closing(get_db()) as db:
            try:
                db.execute(
                    "INSERT INTO users (email, password_hash, created_at) VALUES (?,?,?)",
                    (email, generate_password_hash(password), datetime.utcnow().isoformat())
                )
                db.commit()
            except sqlite3.IntegrityError:
                flash("Email already registered.", "warning")
                return redirect(url_for('register'))
        flash("Account created. Please sign in.", "success")
        return redirect(url_for('login'))
    return render_template_string(
        app.jinja_loader.get_source(app.jinja_env, 'auth.html')[0],
        app_name=APP_NAME, title="Register", user=current_user(), heading="Create Account", cta="Create Account"
    )


@app.get('/logout')
@login_required
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for('index'))


# -----------------------------
# Health / Init
# -----------------------------
@app.get('/healthz')
def healthz():
    return {'status':'ok','time': datetime.utcnow().isoformat()}


@app.before_first_request
def setup():
    init_db()


# -----------------------------
# Error Handlers
# -----------------------------
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(500)
def errs(e):
    flash(str(e), 'warning')
    return redirect(url_for('index'))


# -----------------------------
# Entry
# -----------------------------
if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
