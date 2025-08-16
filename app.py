from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

# ------------------------------------------------------
# Flask setup
# ------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret")

# ------------------------------------------------------
# One-time initialization (Flask 3.x safe)
# ------------------------------------------------------
init_done = False

@app.before_request
def run_once_on_startup():
    """Run one-time setup logic before the first request."""
    global init_done
    if not init_done:
        print("Running one-time initialization...")

        # Example: ensure static dirs exist
        os.makedirs("static/renders", exist_ok=True)
        os.makedirs("static/uploads", exist_ok=True)

        init_done = True

# ------------------------------------------------------
# Routes
# ------------------------------------------------------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/generate", methods=["POST"])
def generate():
    description = request.form.get("description")
    file = request.files.get("file")

    if file:
        upload_path = os.path.join("static", "uploads", file.filename)
        file.save(upload_path)

    renderings = [
        {"type": "front_exterior", "path": "/static/renders/front.jpg"},
        {"type": "back_exterior", "path": "/static/renders/back.jpg"},
    ]

    return render_template("results.html", description=description, renderings=renderings)

@app.route("/room/<room_key>")
def room(room_key):
    ROOM_DEFS = {
        "living_room": {"title": "Living Room"},
        "kitchen": {"title": "Kitchen"},
        "home_office": {"title": "Home Office"},
        "primary_bedroom": {"title": "Primary Bedroom"},
        "primary_bathroom": {"title": "Primary Bathroom"},
    }

    if room_key not in ROOM_DEFS:
        flash("Invalid room selected.", "error")
        return redirect(url_for("home"))

    opt_fields = ["Flooring", "Wall Color", "Lighting"]

    project = session.get("project", {})

    return render_template(
        "room.html",
        project=dict(project),
        room_key=room_key,
        room_title=ROOM_DEFS[room_key]["title"],
        option_fields=opt_fields,
        renderings=project.get("renderings", {}).get(room_key, []),
    )

# ------------------------------------------------------
# Email function
# ------------------------------------------------------
def send_email(recipient, subject, body, attachments=None):
    sender = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_PASS")

    if not sender or not password:
        print("❌ Missing Gmail SMTP credentials")
        return False

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    if attachments:
        for filepath in attachments:
            try:
                with open(filepath, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition",
                    f'attachment; filename="{os.path.basename(filepath)}"'
                )
                msg.attach(part)
            except Exception as e:
                print(f"Error attaching {filepath}: {e}")

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        print("✅ Email sent")
        return True
    except Exception as e:
        print(f"❌ Failed to send email: {e}")
        return False

# ------------------------------------------------------
# Entrypoint
# ------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
