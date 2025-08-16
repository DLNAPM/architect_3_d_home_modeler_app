"""
Architect 3D Home Modeler — Flask App (single file)
===================================================

Features implemented per request:
- Home page: describe home or upload an architectural design to "Generate House Plan".
- After generation, creates 2 exterior renderings (Front / Back) and shows room categories.
- Room categories include: Living Room, Kitchen, Home Office, Primary Bedroom, Primary Bathroom,
  Bedroom2, Bedroom3, Family Room, Half Bath. If description mentions basement, also include:
  Basement w/ Bar (Game Room), Theater Room, Exercise Room (Gym), Steam Room, Basement Hallway.
- Each room has its own option panel (as specified) and can generate multiple renderings.
- Users can select multiple renderings for bulk actions: Delete, Like, Favorite, Download (ZIP), Email.
- Liked renderings can be downloaded and/or emailed (enforced by the bulk action endpoints).
- When 2 or more Favorites exist for a project, a Slideshow button appears.
- Basic auth with account registration/login. Likes/Favorites/Renderings are saved per user.
- Rendering images are generated as placeholders with PIL (text-overlaid) but are pluggable for real AI.

How to run (dev):
-----------------
1) Create and activate a virtualenv (recommended)
   python -m venv .venv && source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
2) Install deps:
   pip install flask pillow python-dotenv
3) (Optional) Configure email by creating a .env file in project root with values:
   SMTP_HOST=your.smtp.host
   SMTP_PORT=587
   SMTP_USER=your_username
   SMTP_PASSWORD=your_password
   SENDER_EMAIL=your_from_address@example.com
4) Run:
   python app.py
5) Visit: http://127.0.0.1:5000

Notes:
- This app writes its Jinja templates and minimal CSS/JS to ./templates and ./static on first run.
- For production, use a proper WSGI server and persistent storage.
- Replace the placeholder image generator with real AI model integration where noted.
"""
from __future__ import annotations
import os
import io
import json
import zipfile
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from flask import (
    Flask, render_template, request, redirect, url_for, flash, session,
    send_file, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from PIL import Image, ImageDraw, ImageFont
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "instance" / "architect3d.sqlite3"
UPLOAD_DIR = BASE_DIR / "uploads"
RENDER_DIR = BASE_DIR / "static" / "renderings"
TEMPLATE_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

load_dotenv(BASE_DIR / ".env")

app = Flask(__name__, template_folder=str(TEMPLATE_DIR), static_folder=str(STATIC_DIR))
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25MB upload limit

# Email (optional)
SMTP_HOST = os.environ.get('SMTP_HOST')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SMTP_USER = os.environ.get('SMTP_USER')
SMTP_PASSWORD = os.environ.get('SMTP_PASSWORD')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL')

# ------------------------------------------------------------
# Utility: ensure folders & templates exist
# ------------------------------------------------------------

def ensure_dirs_and_templates():
    (BASE_DIR / "instance").mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "css").mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "js").mkdir(parents=True, exist_ok=True)

    # --- base.html ---
    base_html = r"""{% macro nav() %}
<nav class="navbar navbar-expand-lg navbar-dark bg-dark">
  <div class="container-fluid">
    <a class="navbar-brand" href="{{ url_for('index') }}">Architect 3D Home Modeler</a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div class="collapse navbar-collapse" id="navbarNav">
      <ul class="navbar-nav me-auto mb-2 mb-lg-0">
        {% if session.get('user_id') %}
        <li class="nav-item"><a class="nav-link" href="{{ url_for('dashboard') }}">My Projects</a></li>
        {% endif %}
      </ul>
      <ul class="navbar-nav">
        {% if session.get('user_id') %}
          <li class="nav-item"><span class="navbar-text me-3">Hi, {{ session.get('username') }}</span></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('logout') }}">Logout</a></li>
        {% else %}
          <li class="nav-item"><a class="nav-link" href="{{ url_for('login') }}">Login</a></li>
          <li class="nav-item"><a class="nav-link" href="{{ url_for('register') }}">Register</a></li>
        {% endif %}
      </ul>
    </div>
  </div>
</nav>
{% endmacro %}

<!doctype html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title or 'Architect 3D Home Modeler' }}</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="{{ url_for('static', filename='css/app.css') }}" rel="stylesheet">
</head>
<body class="bg-black text-light">
  {{ nav() }}
  <main class="container py-4">
    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        {% for category, message in messages %}
          <div class="alert alert-{{ 'warning' if category=='error' else category }} alert-dismissible fade show" role="alert">
            {{ message }}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
          </div>
        {% endfor %}
      {% endif %}
    {% endwith %}

    {% block content %}{% endblock %}
  </main>

  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
  <script src="{{ url_for('static', filename='js/app.js') }}"></script>
</body>
</html>
"""
    (TEMPLATE_DIR / 'base.html').write_text(base_html, encoding='utf-8')

    # --- index.html ---
    index_html = r"""{% extends 'base.html' %}
{% block content %}
<div class="row g-4">
  <div class="col-12 col-lg-7">
    <div class="card shadow-lg border-0 bg-dark-subtle">
      <div class="card-body">
        <h3 class="card-title">Design your Dream Home with AI</h3>
        <p class="text-muted">Describe your home or upload a plan. Click <strong>Generate House Plan</strong> to begin. Two exterior renderings (Front/Back) will be created automatically.</p>
        <form method="post" action="{{ url_for('generate') }}" enctype="multipart/form-data">
          <div class="mb-3">
            <label class="form-label">Home Description</label>
            <textarea class="form-control" name="description" rows="5" placeholder="e.g., 2-story modern farmhouse, 4 bedrooms, family room, basement with gym and theater..."></textarea>
          </div>
          <div class="mb-3">
            <label class="form-label">Upload Architectural Design (optional)</label>
            <input type="file" class="form-control" name="plan_file" accept="image/*,.pdf">
          </div>
          {% if not session.get('user_id') %}
            <div class="alert alert-info">Create an account or log in to save your likes and favorites.</div>
          {% endif %}
          <button class="btn btn-primary btn-lg" type="submit">Generate House Plan</button>
        </form>
      </div>
    </div>
  </div>
  <div class="col-12 col-lg-5">
    <div class="card shadow-lg border-0 h-100 bg-dark-subtle">
      <div class="card-body">
        <h5>What you can do</h5>
        <ul>
          <li>Create multiple renderings for Exteriors and Rooms.</li>
          <li>Select renderings to <strong>Delete</strong>, <strong>Like</strong>, <strong>Favorite</strong>, <strong>Download</strong>, or <strong>Email</strong>.</li>
          <li><strong>Liked</strong> renderings can be downloaded or emailed.</li>
          <li>When you have 2+ <strong>Favorites</strong> in a project, a <em>Slideshow</em> button appears.</li>
        </ul>
        <hr>
        <p class="small text-muted mb-0">Reference app: <a href="#" class="link-light" onclick="alert('This demo is inspired by your reference.');return false;">provided link</a>.</p>
      </div>
    </div>
  </div>
</div>
{% endblock %}
"""
    (TEMPLATE_DIR / 'index.html').write_text(index_html, encoding='utf-8')

    # --- auth templates ---
    login_html = r"""{% extends 'base.html' %}
{% block content %}
<div class="row justify-content-center">
  <div class="col-12 col-md-6 col-lg-5">
    <div class="card shadow-lg border-0 bg-dark-subtle">
      <div class="card-body">
        <h3 class="card-title">Login</h3>
        <form method="post">
          <div class="mb-3">
            <label class="form-label">Email</label>
            <input type="email" class="form-control" name="email" required>
          </div>
          <div class="mb-3">
            <label class="form-label">Password</label>
            <input type="password" class="form-control" name="password" required>
          </div>
          <button class="btn btn-primary" type="submit">Login</button>
          <a class="btn btn-link" href="{{ url_for('register') }}">Create an account</a>
        </form>
      </div>
    </div>
  </div>
</div>
{% endblock %}
"""
    (TEMPLATE_DIR / 'login.html').write_text(login_html, encoding='utf-8')

    register_html = r"""{% extends 'base.html' %}
{% block content %}
<div class="row justify-content-center">
  <div class="col-12 col-md-6 col-lg-5">
    <div class="card shadow-lg border-0 bg-dark-subtle">
      <div class="card-body">
        <h3 class="card-title">Register</h3>
        <form method="post">
          <div class="mb-3">
            <label class="form-label">Username</label>
            <input type="text" class="form-control" name="username" required>
          </div>
          <div class="mb-3">
            <label class="form-label">Email</label>
            <input type="email" class="form-control" name="email" required>
          </div>
          <div class="mb-3">
            <label class="form-label">Password</label>
            <input type="password" class="form-control" name="password" required>
          </div>
          <button class="btn btn-primary" type="submit">Create Account</button>
        </form>
      </div>
    </div>
  </div>
</div>
{% endblock %}
"""
    (TEMPLATE_DIR / 'register.html').write_text(register_html, encoding='utf-8')

    # --- dashboard ---
    dashboard_html = r"""{% extends 'base.html' %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h3>Your Projects</h3>
  <a class="btn btn-success" href="{{ url_for('index') }}">New Project</a>
</div>
{% if projects %}
  <div class="row g-3">
    {% for p in projects %}
    <div class="col-12 col-md-6 col-lg-4">
      <div class="card h-100 bg-dark-subtle border-0 shadow-sm">
        <div class="card-body">
          <h5>{{ p['title'] }}</h5>
          <p class="small text-muted">Created {{ p['created_at'] }}</p>
          <a class="btn btn-primary" href="{{ url_for('plan', project_id=p['id']) }}">Open</a>
        </div>
      </div>
    </div>
    {% endfor %}
  </div>
{% else %}
  <div class="alert alert-secondary">No projects yet. Start a new one from the home page.</div>
{% endif %}
{% endblock %}
"""
    (TEMPLATE_DIR / 'dashboard.html').write_text(dashboard_html, encoding='utf-8')

    # --- plan.html ---
    plan_html = r"""{% extends 'base.html' %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <div>
    <h3>Project: {{ project['title'] }}</h3>
    <p class="text-muted small mb-0">{{ project['description'] or 'No description provided.' }}</p>
  </div>
  <div>
    {% if favorites_count >= 2 %}
      <a class="btn btn-warning" href="{{ url_for('slideshow', project_id=project['id']) }}">Slideshow ({{ favorites_count }})</a>
    {% else %}
      <button class="btn btn-secondary" disabled>Slideshow</button>
    {% endif %}
  </div>
</div>

<h5 class="mt-4">Exteriors</h5>
<div class="row g-3 mb-4">
  {% for exterior in exteriors %}
  <div class="col-12 col-md-6 col-lg-4">
    <div class="card h-100 bg-dark-subtle border-0 shadow-sm">
      <img class="card-img-top" src="{{ url_for('static', filename='renderings/' ~ exterior['image_filename']) }}" alt="{{ exterior['title'] }}">
      <div class="card-body">
        <h5 class="card-title">{{ exterior['title'] }}</h5>
        <a class="btn btn-outline-light" href="{{ url_for('room', project_id=project['id'], room_key=exterior['room_key']) }}">Open</a>
      </div>
    </div>
  </div>
  {% endfor %}
</div>

<h5>Rooms</h5>
<div class="row g-3">
  {% for room in rooms %}
  <div class="col-12 col-md-6 col-lg-4">
    <div class="card h-100 bg-dark-subtle border-0 shadow-sm">
      <div class="card-body d-flex flex-column">
        <h5 class="card-title">{{ room['title'] }}</h5>
        <p class="small text-muted">Generate multiple renderings with different options.</p>
        <div class="mt-auto">
          <a class="btn btn-outline-primary" href="{{ url_for('room', project_id=project['id'], room_key=room['room_key']) }}">Open</a>
        </div>
      </div>
    </div>
  </div>
  {% endfor %}
</div>
{% endblock %}
"""
    (TEMPLATE_DIR / 'plan.html').write_text(plan_html, encoding='utf-8')

    # --- room.html ---
    room_html = r"""{% extends 'base.html' %}
{% block content %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h3>{{ room_title }}</h3>
  <a class="btn btn-secondary" href="{{ url_for('plan', project_id=project['id']) }}">Back to Project</a>
</div>

<div class="card mb-4 bg-dark-subtle border-0 shadow-sm">
  <div class="card-body">
    <h5 class="mb-3">Generate a New Rendering</h5>
    <form method="post" action="{{ url_for('generate_rendering', project_id=project['id'], room_key=room_key) }}">
      <div class="row g-3">
        {% for field in option_fields %}
        <div class="col-12 col-md-6">
          <label class="form-label">{{ field.label }}</label>
          <input class="form-control" name="{{ field.name }}" placeholder="{{ field.placeholder }}">
        </div>
        {% endfor %}
      </div>
      <div class="mt-3">
        <button class="btn btn-primary" type="submit">Generate Rendering</button>
      </div>
    </form>
  </div>
</div>

<div class="card bg-dark-subtle border-0 shadow-sm">
  <div class="card-body">
    <div class="d-flex justify-content-between align-items-center mb-2">
      <h5 class="mb-0">Your Renderings ({{ renderings|length }})</h5>
      <form class="d-flex gap-2" method="post" action="{{ url_for('bulk_action', project_id=project['id'], room_key=room_key) }}">
        <input type="hidden" name="selected_ids" id="selected_ids_input">
        <button name="action" value="like" class="btn btn-outline-success" type="submit">Like</button>
        <button name="action" value="favorite" class="btn btn-outline-warning" type="submit">Favorite</button>
        <button name="action" value="delete" class="btn btn-outline-danger" type="submit" onclick="return confirm('Delete selected renderings?')">Delete</button>
        <button name="action" value="download" class="btn btn-outline-light" type="submit">Download (ZIP)</button>
        <button name="action" value="email" class="btn btn-outline-info" type="button" data-bs-toggle="modal" data-bs-target="#emailModal">Email</button>
      </form>
    </div>

    {% if renderings %}
    <div class="row g-3" id="renderings-grid">
      {% for r in renderings %}
      <div class="col-12 col-md-6 col-lg-4">
        <div class="card h-100 bg-dark border-0 shadow-sm position-relative">
          <img class="card-img-top" src="{{ url_for('static', filename='renderings/' ~ r['image_filename']) }}" alt="Rendering">
          <div class="card-body">
            <div class="form-check">
              <input class="form-check-input rendering-check" type="checkbox" value="{{ r['id'] }}" id="chk{{ r['id'] }}">
              <label class="form-check-label" for="chk{{ r['id'] }}">Select</label>
            </div>
            <p class="small text-muted mt-2 mb-1">Created {{ r['created_at'] }}</p>
            <span class="badge bg-success me-1{% if not r['liked'] %} d-none{% endif %}" id="liked{{ r['id'] }}">Liked</span>
            <span class="badge bg-warning text-dark{% if not r['favorite'] %} d-none{% endif %}" id="fav{{ r['id'] }}">Favorite</span>
          </div>
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
      <div class="alert alert-secondary">No renderings yet. Use the form above to generate one.</div>
    {% endif %}
  </div>
</div>

<!-- Email Modal -->
<div class="modal fade" id="emailModal" tabindex="-1">
  <div class="modal-dialog">
    <div class="modal-content bg-dark-subtle">
      <form method="post" action="{{ url_for('bulk_action', project_id=project['id'], room_key=room_key) }}">
        <div class="modal-header">
          <h5 class="modal-title">Email Selected Renderings</h5>
          <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
        </div>
        <div class="modal-body">
          <input type="hidden" name="selected_ids" id="selected_ids_modal">
          <div class="mb-3">
            <label class="form-label">Recipient Email</label>
            <input type="email" class="form-control" name="recipient" required>
            <div class="form-text">Only <em>Liked</em> renderings are eligible for email.</div>
          </div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
          <button name="action" value="email" class="btn btn-primary" type="submit">Send</button>
        </div>
      </form>
    </div>
  </div>
</div>

<script>
  // Gather selected IDs into hidden inputs before submitting bulk forms
  const updateSelected = () => {
    const ids = Array.from(document.querySelectorAll('.rendering-check:checked')).map(c => c.value);
    document.getElementById('selected_ids_input').value = ids.join(',');
    const modalField = document.getElementById('selected_ids_modal');
    if (modalField) modalField.value = ids.join(',');
  };
  document.addEventListener('change', (e) => {
    if (e.target.classList.contains('rendering-check')) updateSelected();
  });
  document.addEventListener('DOMContentLoaded', updateSelected);
</script>
{% endblock %}
"""
    (TEMPLATE_DIR / 'room.html').write_text(room_html, encoding='utf-8')

    # --- slideshow.html ---
    slideshow_html = r"""{% extends 'base.html' %}
{% block content %}
<h3>Slideshow — Favorites ({{ images|length }})</h3>
<div id="slideshow" class="position-relative" style="max-width: 1000px; margin:auto;">
  {% for img in images %}
  <img src="{{ url_for('static', filename='renderings/' ~ img) }}" class="slide-item w-100 rounded-3 shadow mb-3" style="display: none;">
  {% endfor %}
</div>
<div class="text-center mt-3">
  <button class="btn btn-light me-2" onclick="prevSlide()">Prev</button>
  <button class="btn btn-primary" onclick="nextSlide()">Next</button>
</div>
<script>
  let current = 0;
  const slides = document.querySelectorAll('.slide-item');
  function show(n){
    if (!slides.length) return;
    slides.forEach((s,i)=> s.style.display = (i===n? 'block':'none'));
  }
  function nextSlide(){ current = (current + 1) % slides.length; show(current); }
  function prevSlide(){ current = (current - 1 + slides.length) % slides.length; show(current); }
  document.addEventListener('DOMContentLoaded', ()=> show(0));
</script>
{% endblock %}
"""
    (TEMPLATE_DIR / 'slideshow.html').write_text(slideshow_html, encoding='utf-8')

    # --- static/css/app.css ---
    css = r"""
body { background: #0b0f17; }
.card { border-radius: 1rem; }
.card-img-top { border-top-left-radius: 1rem; border-top-right-radius: 1rem; }
"""
    (STATIC_DIR / 'css' / 'app.css').write_text(css, encoding='utf-8')

    # --- static/js/app.js ---
    js = r"""
// Placeholder for future enhancements
"""
    (STATIC_DIR / 'js' / 'app.js').write_text(js, encoding='utf-8')

# ------------------------------------------------------------
# Database helpers
# ------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT NOT NULL,
            description TEXT,
            plan_filename TEXT,
            has_basement INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS renderings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            user_id INTEGER,
            room_key TEXT NOT NULL,
            title TEXT,
            options_json TEXT,
            image_filename TEXT NOT NULL,
            liked INTEGER DEFAULT 0,
            favorite INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.commit()
    conn.close()

# ------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------

def current_user_id():
    return session.get('user_id')


def login_required():
    if not current_user_id():
        flash('Please log in to access that page.', 'error')
        return redirect(url_for('login'))
    return None

# ------------------------------------------------------------
# Domain: Rooms & Options
# ------------------------------------------------------------

ROOM_DEFS: Dict[str, Dict] = {
    'front_exterior': {
        'title': 'Front Exterior',
        'options': [
            {'name':'style','label':'Style','placeholder':'Modern farmhouse'},
            {'name':'siding','label':'Siding','placeholder':'Board & batten'},
            {'name':'roof','label':'Roof','placeholder':'Metal standing seam'},
        ]
    },
    'back_exterior': {
        'title': 'Back Exterior',
        'options': [
            {'name':'patio','label':'Patio/Deck','placeholder':'Covered patio with pergola'},
            {'name':'pool','label':'Pool','placeholder':'Infinity pool'},
            {'name':'landscape','label':'Landscape','placeholder':'Evergreen shrubs'},
        ]
    },
    'living_room': {
        'title': 'Living Room',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Wide-plank oak'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Warm white'},
            {'name':'lighting','label':'Lighting','placeholder':'Recessed + chandelier'},
            {'name':'furniture_style','label':'Furniture Style','placeholder':'Modern minimal'},
            {'name':'chairs','label':'Chairs','placeholder':'Two accent chairs'},
            {'name':'coffee_table','label':'Coffee Table','placeholder':'Marble top'},
            {'name':'wine_storage','label':'Wine Storage','placeholder':'Built-in shelves'},
            {'name':'fireplace','label':'Fireplace','placeholder':'Linear gas fireplace'},
            {'name':'door_style','label':'Door Style','placeholder':'Steel-framed glass'}
        ]
    },
    'kitchen': {
        'title': 'Kitchen',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Porcelain tile'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Soft gray'},
            {'name':'lighting','label':'Lighting','placeholder':'Pendants + recessed'},
            {'name':'cabinet_style','label':'Cabinet Style','placeholder':'Shaker with crown'},
            {'name':'countertop','label':'Countertop Material','placeholder':'Quartz - Calacatta'},
            {'name':'appliances','label':'Appliances','placeholder':'Panel-ready set'},
            {'name':'backsplash','label':'Backsplash','placeholder':'Herringbone marble'},
            {'name':'sink','label':'Kitchen Sink','placeholder':'Farmhouse apron'},
            {'name':'island_lights','label':'Lights above the Island','placeholder':'3 brass pendants'}
        ]
    },
    'home_office': {
        'title': 'Home Office',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Walnut'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Deep navy'},
            {'name':'lighting','label':'Lighting','placeholder':'Task + sconces'},
            {'name':'desk_style','label':'Desk Style','placeholder':'Standing desk'},
            {'name':'office_chair','label':'Office Chair','placeholder':'Ergonomic leather'},
            {'name':'storage','label':'Storage','placeholder':'Built-ins with glass'}
        ]
    },
    'primary_bedroom': {
        'title': 'Primary Bedroom',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Carpet - plush'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Greige'},
            {'name':'lighting','label':'Lighting','placeholder':'Cove + reading lights'},
            {'name':'bed_style','label':'Bed Style','placeholder':'Upholstered king'},
            {'name':'furniture_style','label':'Furniture Style','placeholder':'Modern walnut'},
            {'name':'closet','label':'Closet Design','placeholder':'His & Hers built-ins'},
            {'name':'ceiling_fan','label':'Ceiling Fan','placeholder':'52\" matte black'}
        ]
    },
    'primary_bathroom': {
        'title': 'Primary Bathroom',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Heated tile'},
            {'name':'wall_color','label':'Wall Color','placeholder':'White'},
            {'name':'lighting','label':'Lighting','placeholder':'Sconces + recessed'},
            {'name':'vanity','label':'Vanity Style','placeholder':'Floating double vanity'},
            {'name':'shower_or_tub','label':'Shower or Tub','placeholder':'Wet room'},
            {'name':'tile_style','label':'Tile Style','placeholder':'Zellige'},
            {'name':'sink','label':'Bathroom Sink','placeholder':'Vessel sinks'},
            {'name':'mirror','label':'Mirror Style','placeholder':'Arched framed'},
            {'name':'balcony','label':'Balcony (If on 2nd Floor)','placeholder':'Juliet balcony'}
        ]
    },
    'bedroom2': {
        'title': 'Bedroom 2',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Carpet'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Pastel blue'},
            {'name':'lighting','label':'Lighting','placeholder':'Pendant'},
            {'name':'bed_style','label':'Bed Style','placeholder':'Twin bunk'},
            {'name':'furniture_style','label':'Furniture Style','placeholder':'Scandi oak'},
            {'name':'ceiling_fan','label':'Ceiling Fan','placeholder':'Yes'},
            {'name':'balcony','label':'Balcony (If on 2nd Floor)','placeholder':'No'}
        ]
    },
    'bedroom3': {
        'title': 'Bedroom 3',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Carpet'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Sage'},
            {'name':'lighting','label':'Lighting','placeholder':'Flush mount'},
            {'name':'bed_style','label':'Bed Style','placeholder':'Queen platform'},
            {'name':'furniture_style','label':'Furniture Style','placeholder':'Mid-century'},
            {'name':'ceiling_fan','label':'Ceiling Fan','placeholder':'Yes'},
            {'name':'balcony','label':'Balcony (If on 2nd Floor)','placeholder':'No'}
        ]
    },
    'family_room': {
        'title': 'Family Room',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Engineered wood'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Cream'},
            {'name':'lighting','label':'Lighting','placeholder':'Recessed'},
            {'name':'seating','label':'Seating','placeholder':'Sectional sofa'},
            {'name':'media','label':'Media','placeholder':'Built-in TV wall'}
        ]
    },
    'half_bath': {
        'title': 'Half Bath / Powder Room',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Patterned tile'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Bold wallpaper'},
            {'name':'lighting','label':'Lighting','placeholder':'Sconce pair'},
            {'name':'vanity','label':'Vanity Style','placeholder':'Pedestal'},
            {'name':'tile_style','label':'Tile Style','placeholder':'Mosaic'},
            {'name':'mirror','label':'Mirror Style','placeholder':'Round brass'}
        ]
    },
    # Basement-related
    'basement_game_room': {
        'title': 'Basement — Game Room (Bar)',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'LVP'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Charcoal'},
            {'name':'lighting','label':'Lighting','placeholder':'Track lights'},
            {'name':'pool_table','label':'Pool Table','placeholder':'Yes'},
            {'name':'wine_bar','label':'Wine Bar','placeholder':'Backlit shelves'},
            {'name':'arcade_games','label':'Types of Arcade Games','placeholder':'Pinball, retro cabinet'},
            {'name':'table_games','label':'Other Table Games','placeholder':'Foosball, air hockey'}
        ]
    },
    'basement_gym': {
        'title': 'Basement — Gym',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Rubber tiles'},
            {'name':'wall_color','label':'Wall Color','placeholder':'White'},
            {'name':'lighting','label':'Lighting','placeholder':'Bright LEDs'},
            {'name':'equipment','label':'Types of Equipment','placeholder':'Treadmill, rack, bike'},
            {'name':'gym_station','label':'Gym Station','placeholder':'Cable crossover'},
            {'name':'steam_room','label':'Steam Room','placeholder':'Yes'}
        ]
    },
    'basement_theater': {
        'title': 'Basement — Theater Room',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'Dark carpet'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Blackout paint'},
            {'name':'lighting','label':'Lighting','placeholder':'LED strips'},
            {'name':'wall_treatment','label':'Wall Treatment','placeholder':'Acoustic panels'},
            {'name':'seating','label':'Seating','placeholder':'Recliners, 2 rows'},
            {'name':'popcorn','label':'Popcorn Machine','placeholder':'Vintage style'},
            {'name':'sound_system','label':'Sound System','placeholder':'Dolby Atmos'},
            {'name':'screen_type','label':'Screen Type','placeholder':'Projector 120\"'},
            {'name':'movie_posters','label':'Movie Posters on Walls','placeholder':'Classic films'},
            {'name':'show_movie','label':'Show Movie on Screen','placeholder':'Yes'}
        ]
    },
    'basement_hallway': {
        'title': 'Basement — Hallway',
        'options': [
            {'name':'flooring','label':'Flooring','placeholder':'LVP'},
            {'name':'wall_color','label':'Wall Color','placeholder':'Light gray'},
            {'name':'lighting','label':'Lighting','placeholder':'Sconces'}
        ]
    },
}

# ------------------------------------------------------------
# Placeholder image generator (replace with real AI integration)
# ------------------------------------------------------------

def create_placeholder_image(text_lines: List[str], dest_path: Path, size=(1200, 800)) -> None:
    img = Image.new('RGB', size, color=(20, 25, 35))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 30)
        big = ImageFont.truetype("DejaVuSans-Bold.ttf", 48)
    except Exception:
        font = ImageFont.load_default()
        big = ImageFont.load_default()
    y = 40
    draw.text((40, y), "Architect 3D Home Modeler", fill=(200, 220, 255), font=big)
    y += 80
    for line in text_lines:
        draw.text((40, y), line, fill=(230, 230, 230), font=font)
        y += 40
    img.save(dest_path)

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------

@app.before_first_request
def setup():
    ensure_dirs_and_templates()
    init_db()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        conn = get_db(); cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE email=?', (email,))
        row = cur.fetchone()
        conn.close()
        if row and check_password_hash(row['password_hash'], password):
            session['user_id'] = row['id']
            session['username'] = row['username']
            flash('Welcome back!', 'success')
            return redirect(url_for('dashboard'))
        flash('Invalid credentials.', 'error')
    return render_template('login.html')

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip().lower()
        password = request.form['password']
        password_hash = generate_password_hash(password)
        now = datetime.utcnow().isoformat()
        conn = get_db(); cur = conn.cursor()
        try:
            cur.execute('INSERT INTO users (username,email,password_hash,created_at) VALUES (?,?,?,?)',
                        (username, email, password_hash, now))
            conn.commit()
            flash('Account created. Please log in.', 'success')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Email already registered.', 'error')
        finally:
            conn.close()
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'success')
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if not current_user_id():
        return redirect(url_for('login'))
    uid = current_user_id()
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM projects WHERE user_id=? ORDER BY id DESC', (uid,))
    projects = [dict(r) for r in cur.fetchall()]
    conn.close()
    return render_template('dashboard.html', projects=projects)

@app.route('/generate', methods=['POST'])
def generate():
    description = request.form.get('description','').strip()
    plan_file = request.files.get('plan_file')
    plan_filename = None
    if plan_file and plan_file.filename:
        safe_name = f"plan_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{plan_file.filename.replace(' ','_')}"
        plan_path = UPLOAD_DIR / safe_name
        plan_file.save(plan_path)
        plan_filename = safe_name

    has_basement = int('basement' in description.lower())
    uid = current_user_id()
    now = datetime.utcnow().isoformat()

    title = f"Home Plan {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"

    conn = get_db(); cur = conn.cursor()
    cur.execute('INSERT INTO projects (user_id,title,description,plan_filename,has_basement,created_at) VALUES (?,?,?,?,?,?)',
                (uid, title, description, plan_filename, has_basement, now))
    project_id = cur.lastrowid

    # Create 2 exterior renderings (Front / Back)
    for key in ['front_exterior','back_exterior']:
        room = ROOM_DEFS[key]
        options = {o['name']: '' for o in room['options']}
        filename = f"proj{project_id}_{key}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.jpg"
        img_path = RENDER_DIR / filename
        lines = [room['title'], 'Initial AI render']
        create_placeholder_image(lines, img_path)
        cur.execute('''
            INSERT INTO renderings (project_id,user_id,room_key,title,options_json,image_filename,created_at)
            VALUES (?,?,?,?,?,?,?)
        ''', (project_id, uid, key, room['title'], json.dumps(options), filename, now))

    conn.commit(); conn.close()
    return redirect(url_for('plan', project_id=project_id))

@app.route('/plan/<int:project_id>')
def plan(project_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM projects WHERE id=?', (project_id,))
    project = cur.fetchone()
    if not project: conn.close(); abort(404)

    # Build exterior cards using latest rendering image for the key (or placeholder)
    exteriors = []
    for key in ['front_exterior','back_exterior']:
        cur.execute('SELECT * FROM renderings WHERE project_id=? AND room_key=? ORDER BY id DESC LIMIT 1', (project_id, key))
        r = cur.fetchone()
        image_filename = r['image_filename'] if r else ''
        exteriors.append({
            'title': ROOM_DEFS[key]['title'],
            'image_filename': image_filename,
            'room_key': key
        })

    # Build rooms list
    base_rooms = ['living_room','kitchen','home_office','primary_bedroom','primary_bathroom','bedroom2','bedroom3','family_room','half_bath']
    rooms = [{'room_key': k, 'title': ROOM_DEFS[k]['title']} for k in base_rooms]
    if project['has_basement']:
        rooms += [
            {'room_key':'basement_game_room','title':ROOM_DEFS['basement_game_room']['title']},
            {'room_key':'basement_gym','title':ROOM_DEFS['basement_gym']['title']},
            {'room_key':'basement_theater','title':ROOM_DEFS['basement_theater']['title']},
            {'room_key':'basement_hallway','title':ROOM_DEFS['basement_hallway']['title']},
        ]

    # favorites count
    cur.execute('SELECT COUNT(*) as c FROM renderings WHERE project_id=? AND favorite=1', (project_id,))
    favorites_count = cur.fetchone()['c']

    conn.close()
    return render_template('plan.html', project=dict(project), exteriors=exteriors, rooms=rooms, favorites_count=favorites_count)

@app.route('/plan/<int:project_id>/room/<room_key>')
def room(project_id, room_key):
    if room_key not in ROOM_DEFS:
        abort(404)
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT * FROM projects WHERE id=?', (project_id,))
    project = cur.fetchone()
    if not project: conn.close(); abort(404)
    cur.execute('SELECT * FROM renderings WHERE project_id=? AND room_key=? ORDER BY id DESC', (project_id, room_key))
    renderings = [dict(r) for r in cur.fetchall()]
    conn.close()
    opt_fields = ROOM_DEFS[room_key]['options']
    return render_template('room.html', project=dict(project), room_key=room_key, room_title=ROOM_DEFS[room_key]['title'], option_fields=opt_fields, renderings=renderings)

@app.route('/plan/<int:project_id>/room/<room_key>/generate', methods=['POST'])
def generate_rendering(project_id, room_key):
    if room_key not in ROOM_DEFS:
        abort(404)
    uid = current_user_id()
    now = datetime.utcnow().isoformat()
    # Collect options
    options = {}
    for f in ROOM_DEFS[room_key]['options']:
        options[f['name']] = request.form.get(f['name'], '').strip()
    # Generate placeholder image
    filename = f"proj{project_id}_{room_key}_{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}.jpg"
    img_path = RENDER_DIR / filename
    title = ROOM_DEFS[room_key]['title']
    # Text lines summarize key options
    lines = [title, 'AI Rendering'] + [f"{k}: {v}" for k,v in options.items() if v]
    create_placeholder_image(lines, img_path)

    conn = get_db(); cur = conn.cursor()
    cur.execute('''
        INSERT INTO renderings (project_id,user_id,room_key,title,options_json,image_filename,created_at)
        VALUES (?,?,?,?,?,?,?)
    ''', (project_id, uid, room_key, title, json.dumps(options), filename, now))
    conn.commit(); conn.close()
    flash('Rendering created.', 'success')
    return redirect(url_for('room', project_id=project_id, room_key=room_key))

@app.route('/plan/<int:project_id>/room/<room_key>/bulk', methods=['POST'])
def bulk_action(project_id, room_key):
    action = request.form.get('action')
    selected_ids = [int(x) for x in request.form.get('selected_ids','').split(',') if x.strip().isdigit()]
    if not selected_ids:
        flash('No renderings selected.', 'error')
        return redirect(url_for('room', project_id=project_id, room_key=room_key))

    conn = get_db(); cur = conn.cursor()

    if action == 'delete':
        cur.execute(f"SELECT id, image_filename FROM renderings WHERE project_id=? AND room_key=? AND id IN ({','.join('?'*len(selected_ids))})",
                    (project_id, room_key, *selected_ids))
        for row in cur.fetchall():
            img_file = RENDER_DIR / row['image_filename']
            if img_file.exists():
                img_file.unlink(missing_ok=True)
        cur.execute(f"DELETE FROM renderings WHERE project_id=? AND room_key=? AND id IN ({','.join('?'*len(selected_ids))})",
                    (project_id, room_key, *selected_ids))
        conn.commit()
        flash('Deleted selected renderings.', 'success')

    elif action in ('like','favorite'):
        field = 'liked' if action=='like' else 'favorite'
        cur.execute(f"UPDATE renderings SET {field}=1 WHERE project_id=? AND room_key=? AND id IN ({','.join('?'*len(selected_ids))})",
                    (project_id, room_key, *selected_ids))
        conn.commit()
        flash(f'Marked as {field}.', 'success')

    elif action == 'download':
        # Only allow liked renderings in download per spec
        cur.execute(f"SELECT image_filename, liked FROM renderings WHERE project_id=? AND room_key=? AND id IN ({','.join('?'*len(selected_ids))})",
                    (project_id, room_key, *selected_ids))
        files = [r['image_filename'] for r in cur.fetchall() if r['liked']]
        if not files:
            flash('Only Liked renderings can be downloaded. None selected.', 'error')
            return redirect(url_for('room', project_id=project_id, room_key=room_key))
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, mode='w', compression=zipfile.ZIP_DEFLATED) as zf:
            for fn in files:
                fp = RENDER_DIR / fn
                if fp.exists():
                    zf.write(fp, arcname=fn)
        mem.seek(0)
        return send_file(mem, as_attachment=True, download_name=f"renderings_p{project_id}_{room_key}.zip")

    elif action == 'email':
        recipient = request.form.get('recipient','').strip()
        if not recipient:
            flash('Recipient email required.', 'error')
            return redirect(url_for('room', project_id=project_id, room_key=room_key))
        cur.execute(f"SELECT image_filename, liked FROM renderings WHERE project_id=? AND room_key=? AND id IN ({','.join('?'*len(selected_ids))})",
                    (project_id, room_key, *selected_ids))
        rows = cur.fetchall()
        files = [r['image_filename'] for r in rows if r['liked']]
        if not files:
            flash('Only Liked renderings can be emailed. None selected.', 'error')
            return redirect(url_for('room', project_id=project_id, room_key=room_key))
        try:
            send_email_with_attachments(recipient, f"Architect 3D Renderings — Project {project_id}",
                                        "Here are your selected renderings.", [RENDER_DIR / f for f in files])
            flash('Email sent.', 'success')
        except Exception as e:
            flash(f'Email failed: {e}', 'error')

    else:
        flash('Unknown action.', 'error')

    conn.close()
    return redirect(url_for('room', project_id=project_id, room_key=room_key))

@app.route('/slideshow/<int:project_id>')
def slideshow(project_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute('SELECT image_filename FROM renderings WHERE project_id=? AND favorite=1 ORDER BY id DESC', (project_id,))
    images = [r['image_filename'] for r in cur.fetchall()]
    conn.close()
    if len(images) < 2:
        flash('Need at least 2 favorites to start a slideshow.', 'error')
        return redirect(url_for('plan', project_id=project_id))
    return render_template('slideshow.html', images=images)

# ------------------------------------------------------------
# Email helper
# ------------------------------------------------------------

def send_email_with_attachments(to_email: str, subject: str, body: str, attachments: List[Path]):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SENDER_EMAIL):
        raise RuntimeError('SMTP not configured. Set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SENDER_EMAIL in .env')
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = SENDER_EMAIL
    msg['To'] = to_email
    msg.set_content(body)
    for p in attachments:
        with open(p, 'rb') as f:
            data = f.read()
        msg.add_attachment(data, maintype='image', subtype='jpeg', filename=p.name)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)

# ------------------------------------------------------------
# CLI entry
# ------------------------------------------------------------

if __name__ == '__main__':
    ensure_dirs_and_templates()
    init_db()
    app.run(debug=True)
