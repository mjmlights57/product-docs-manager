# Product Cut Sheets & Certifications Manager
# =========================================
# A single-file Flask app to manage product cut sheets (PDF), certification screenshots (image/PDF),
# and product images with preview.
#
# What changed
# - UI label: SKU -> Model Number (DB stays backward-compatible; `sku` retained/mirrored).
# - Added Product Image upload + inline thumbnail preview.
# - Ready for Render: supports DATABASE_URL (Postgres) and PRODUCT_DOCS_BASE_DIR for files.
#   Light auto-migrations add `model_number` & `image_filename` and copy old `sku` into `model_number`.
#
# Windows CMD quick start
#   cd C:\Users\suppo\Desktop\Cutsheet_DLC_App
#   python -m venv .venv
#   .venv\Scripts\activate.bat
#   pip install -r requirements.txt
#   set MODE=server
#   set HOST=127.0.0.1
#   set PORT=5000
#   set PRODUCT_DOCS_BASE_DIR=C:\Users\suppo\Desktop\Cutsheet_DLC_App
#   python product_cut_sheets_certifications_manager_flask_app.py
#
from __future__ import annotations

import csv
import io
import os
import sqlite3
import zipfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from uuid import uuid4

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    render_template_string,
    request,
    send_file,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from PyPDF2 import PdfMerger, PdfReader
from PIL import Image
from jinja2 import ChoiceLoader, DictLoader

# ----------------------------
# Config helpers
# ----------------------------

def _default_base_dir() -> Path:
    env = os.environ.get("PRODUCT_DOCS_BASE_DIR")
    if env:
        return Path(env).expanduser().resolve()
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd()


BASE_DIR = _default_base_dir()
UPLOAD_DIR = BASE_DIR / "uploads"
CUTSHEETS_DIR = UPLOAD_DIR / "cutsheets"
CERTS_DIR = UPLOAD_DIR / "certifications"
IMAGES_DIR = UPLOAD_DIR / "images"  # NEW: product images
DB_PATH = BASE_DIR / "products.db"

ALLOWED_CUTSHEET_EXT = {".pdf"}
ALLOWED_CERT_EXT = {".pdf", ".png", ".jpg", ".jpeg"}
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}

MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB per request

app = Flask(__name__)
# Prefer DATABASE_URL if present (e.g., Render Postgres), else fallback to SQLite
_sqlite_url = f"sqlite:///{DB_PATH}"
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret"),
    SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", _sqlite_url),
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
)

db = SQLAlchemy(app)

# Ensure upload folders exist
for d in (UPLOAD_DIR, CUTSHEETS_DIR, CERTS_DIR, IMAGES_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ----------------------------
# Models
# ----------------------------
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    # Back-compat: keep `sku` so existing DBs work; prefer `model_number` moving forward
    sku = db.Column(db.String(128), nullable=True, unique=False, index=True)
    model_number = db.Column(db.String(128), nullable=True, unique=False, index=True)  # NEW
    notes = db.Column(db.Text, nullable=True)
    cutsheet_filename = db.Column(db.String(512), nullable=True)
    cert_filename = db.Column(db.String(512), nullable=True)
    image_filename = db.Column(db.String(512), nullable=True)  # NEW: product image
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def cutsheet_path(self) -> Optional[Path]:
        return (CUTSHEETS_DIR / self.cutsheet_filename) if self.cutsheet_filename else None

    def cert_path(self) -> Optional[Path]:
        return (CERTS_DIR / self.cert_filename) if self.cert_filename else None

    def image_path(self) -> Optional[Path]:
        return (IMAGES_DIR / self.image_filename) if self.image_filename else None


with app.app_context():
    db.create_all()


# ----------------------------
# Light "migration" to keep old DBs compatible
# ----------------------------

def _ensure_columns_and_copy_sku_to_model_number():
    engine = db.get_engine()
    url = str(engine.url)
    try:
        # Works for SQLite; for Postgres the CREATEs are no-ops if columns exist due to SQLAlchemy schema
        insp = db.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("product")}
        to_add = []
        if "model_number" not in cols:
            to_add.append("ALTER TABLE product ADD COLUMN model_number VARCHAR(128)")
        if "image_filename" not in cols:
            to_add.append("ALTER TABLE product ADD COLUMN image_filename VARCHAR(512)")
        if to_add and engine.dialect.name == "sqlite":
            with engine.begin() as conn:
                for ddl in to_add:
                    conn.exec_driver_sql(ddl)
        # Copy sku → model_number if destination empty
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "UPDATE product SET model_number = COALESCE(model_number, sku) WHERE (model_number IS NULL OR model_number='') AND sku IS NOT NULL"
            )
    except Exception:
        # Best effort; ignore if inspection fails on some envs
        pass

with app.app_context():
    _ensure_columns_and_copy_sku_to_model_number()


# ----------------------------
# Utility helpers
# ----------------------------

def _ext_ok(filename: str, allowed: set[str]) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in allowed


def _safe_unique_name(original_filename: str) -> str:
    base = secure_filename(Path(original_filename).stem) or "file"
    ext = Path(original_filename).suffix.lower()
    return f"{base}-{uuid4().hex}{ext}"


def _validate_cutsheet(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        raise ValueError("No cutsheet file provided.")
    filename = file_storage.filename
    if not _ext_ok(filename, ALLOWED_CUTSHEET_EXT):
        raise ValueError("Cutsheet must be a PDF.")
    return filename


def _validate_cert(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        raise ValueError("No certification file provided.")
    filename = file_storage.filename
    if not _ext_ok(filename, ALLOWED_CERT_EXT):
        raise ValueError("Certification must be a PDF or image (png/jpg/jpeg).")
    return filename


def _validate_image(file_storage) -> str:
    if not file_storage or not file_storage.filename:
        raise ValueError("No image file provided.")
    filename = file_storage.filename
    if not _ext_ok(filename, ALLOWED_IMAGE_EXT):
        raise ValueError("Image must be png/jpg/jpeg/webp.")
    return filename


def _image_to_pdf_bytes(image_path: Path) -> io.BytesIO:
    with Image.open(image_path) as im:
        rgb = im.convert("RGB")
        out = io.BytesIO()
        rgb.save(out, format="PDF")
        out.seek(0)
        return out


def _cert_as_pdf_stream(product: Product) -> io.BytesIO:
    cert = product.cert_path()
    if not cert or not cert.exists():
        raise FileNotFoundError("Certification file missing.")
    if cert.suffix.lower() == ".pdf":
        return io.BytesIO(cert.read_bytes())
    return _image_to_pdf_bytes(cert)


def _merge_pdfs_stream(streams: Iterable[io.BytesIO]) -> io.BytesIO:
    merger = PdfMerger()
    try:
        for s in streams:
            merger.append(PdfReader(s))
        out = io.BytesIO()
        merger.write(out)
        out.seek(0)
        return out
    finally:
        merger.close()


# ----------------------------
# Routes - Views
# ----------------------------
@app.get("/")
def index():
    q = (request.args.get("q") or "").strip()
    missing = request.args.get("missing")

    query = Product.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(
                Product.name.ilike(like),
                Product.model_number.ilike(like),
                Product.sku.ilike(like),
                Product.notes.ilike(like),
            )
        )
    products = query.order_by(Product.created_at.desc()).all()

    if missing == "cutsheet":
        products = [p for p in products if not p.cutsheet_filename]
    elif missing == "cert":
        products = [p for p in products if not p.cert_filename]

    return render_template("index.html", products=products, q=q, missing=missing)


@app.get("/product/new")
def product_new():
    return render_template("form.html", product=None)


@app.post("/product/create")
def product_create():
    name = (request.form.get("name") or "").strip()
    # Accept either field name for back-compat with tests
    model_number = (request.form.get("model_number") or request.form.get("sku") or "").strip()
    notes = (request.form.get("notes") or "").strip()

    if not name:
        flash("Name is required", "error")
        return redirect(url_for("product_new"))

    cutsheet_file = request.files.get("cutsheet")
    cert_file = request.files.get("cert")
    image_file = request.files.get("image")  # NEW

    cutsheet_filename = None
    cert_filename = None
    image_filename = None

    if cutsheet_file and cutsheet_file.filename:
        _validate_cutsheet(cutsheet_file)
        cutsheet_filename = _safe_unique_name(cutsheet_file.filename)
        cutsheet_file.save(CUTSHEETS_DIR / cutsheet_filename)

    if cert_file and cert_file.filename:
        _validate_cert(cert_file)
        cert_filename = _safe_unique_name(cert_file.filename)
        cert_file.save(CERTS_DIR / cert_filename)

    if image_file and image_file.filename:
        _validate_image(image_file)
        image_filename = _safe_unique_name(image_file.filename)
        image_file.save(IMAGES_DIR / image_filename)

    p = Product(
        name=name,
        sku=model_number or None,  # keep sku for tests/back-compat
        model_number=model_number or None,
        notes=notes or None,
        cutsheet_filename=cutsheet_filename,
        cert_filename=cert_filename,
        image_filename=image_filename,
    )
    db.session.add(p)
    db.session.commit()

    flash("Product created", "success")
    return redirect(url_for("index"))


@app.get("/product/<int:pid>/edit")
def product_edit(pid: int):
    p = Product.query.get_or_404(pid)
    return render_template("form.html", product=p)


@app.post("/product/<int:pid>/update")
def product_update(pid: int):
    p = Product.query.get_or_404(pid)
    p.name = (request.form.get("name") or "").strip() or p.name
    new_model = (request.form.get("model_number") or request.form.get("sku") or "").strip() or None
    p.model_number = new_model
    p.sku = new_model  # keep mirrored
    p.notes = (request.form.get("notes") or "").strip() or None

    # Optional replacements
    cutsheet_file = request.files.get("cutsheet")
    cert_file = request.files.get("cert")
    image_file = request.files.get("image")

    if cutsheet_file and cutsheet_file.filename:
        _validate_cutsheet(cutsheet_file)
        if p.cutsheet_filename:
            old = CUTSHEETS_DIR / p.cutsheet_filename
            if old.exists():
                try:
                    old.unlink()
                except Exception:
                    pass
        newname = _safe_unique_name(cutsheet_file.filename)
        cutsheet_file.save(CUTSHEETS_DIR / newname)
        p.cutsheet_filename = newname

    if cert_file and cert_file.filename:
        _validate_cert(cert_file)
        if p.cert_filename:
            old = CERTS_DIR / p.cert_filename
            if old.exists():
                try:
                    old.unlink()
                except Exception:
                    pass
        newname = _safe_unique_name(cert_file.filename)
        cert_file.save(CERTS_DIR / newname)
        p.cert_filename = newname

    if image_file and image_file.filename:
        _validate_image(image_file)
        if p.image_filename:
            old = IMAGES_DIR / p.image_filename
            if old.exists():
                try:
                    old.unlink()
                except Exception:
                    pass
        newname = _safe_unique_name(image_file.filename)
        image_file.save(IMAGES_DIR / newname)
        p.image_filename = newname

    db.session.commit()
    flash("Product updated", "success")
    return redirect(url_for("index"))


@app.post("/product/<int:pid>/delete")
def product_delete(pid: int):
    p = Product.query.get_or_404(pid)
    for path in (p.cutsheet_path(), p.cert_path(), p.image_path()):
        if path and path.exists():
            try:
                path.unlink()
            except Exception:
                pass
    db.session.delete(p)
    db.session.commit()
    flash("Product deleted", "success")
    return redirect(url_for("index"))


# ----------------------------
# Downloads (single)
# ----------------------------
@app.get("/download/cutsheet/<int:pid>")
def download_cutsheet(pid: int):
    p = Product.query.get_or_404(pid)
    path = p.cutsheet_path()
    if not path or not path.exists():
        abort(404, "Cutsheet not found")
    label = p.model_number or p.sku or p.name
    return send_file(path, as_attachment=True, download_name=f"{label}-cutsheet.pdf")


@app.get("/download/cert/<int:pid>")
def download_cert(pid: int):
    p = Product.query.get_or_404(pid)
    path = p.cert_path()
    if not path or not path.exists():
        abort(404, "Certification not found")
    label = p.model_number or p.sku or p.name
    return send_file(path, as_attachment=True, download_name=f"{label}-cert{path.suffix.lower()}")


@app.get("/download/combined/<int:pid>")
def download_combined(pid: int):
    p = Product.query.get_or_404(pid)
    cspath = p.cutsheet_path()
    if not cspath or not cspath.exists():
        abort(404, "Cutsheet is required to create combined file.")
    if not p.cert_path() or not p.cert_path().exists():
        abort(404, "Certification file is required to create combined file.")

    streams: list[io.BytesIO] = []
    try:
        streams.append(io.BytesIO(cspath.read_bytes()))
        streams.append(_cert_as_pdf_stream(p))
        combined = _merge_pdfs_stream(streams)
        label = p.model_number or p.sku or p.name
        return send_file(
            combined,
            as_attachment=True,
            download_name=f"{label}-combined.pdf",
            mimetype="application/pdf",
        )
    finally:
        for s in streams:
            try:
                s.close()
            except Exception:
                pass


# ----------------------------
# Bulk downloads & CSV export
# ----------------------------
@app.post("/bulk/download")
def bulk_download():
    action = request.form.get("action")  # cutsheet|cert|combined
    ids = request.form.getlist("ids")
    if not ids:
        flash("No products selected", "error")
        return redirect(url_for("index"))

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for sid in ids:
            p = Product.query.get(int(sid))
            if not p:
                continue
            label = (p.model_number or p.sku or p.name).strip().replace("/", "-")
            try:
                if action == "cutsheet":
                    path = p.cutsheet_path()
                    if path and path.exists():
                        zf.write(path, arcname=f"{label}/cutsheet.pdf")
                elif action == "cert":
                    path = p.cert_path()
                    if path and path.exists():
                        zf.write(path, arcname=f"{label}/cert{path.suffix.lower()}")
                elif action == "combined":
                    cspath = p.cutsheet_path()
                    certpath = p.cert_path()
                    if cspath and cspath.exists() and certpath and certpath.exists():
                        streams = [io.BytesIO(cspath.read_bytes()), _cert_as_pdf_stream(p)]
                        combined = _merge_pdfs_stream(streams)
                        zf.writestr(f"{label}/{label}-combined.pdf", combined.getvalue())
            except Exception as e:
                zf.writestr(f"{label}/ERROR.txt", f"Failed to process: {e}")

    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name=f"products-{action}.zip")


@app.get("/export/csv")
def export_csv():
    output = io.StringIO()
    w = csv.writer(output)
    w.writerow(["id", "name", "model_number", "notes", "has_cutsheet", "has_cert", "has_image", "created_at", "updated_at"])
    for p in Product.query.order_by(Product.id.asc()).all():
        w.writerow([
            p.id,
            p.name,
            p.model_number or p.sku or "",
            (p.notes or "").replace("\n", " "),
            bool(p.cutsheet_filename),
            bool(p.cert_filename),
            bool(p.image_filename),
            p.created_at.isoformat(timespec="seconds"),
            p.updated_at.isoformat(timespec="seconds"),
        ])
    mem = io.BytesIO(output.getvalue().encode("utf-8"))
    return send_file(mem, as_attachment=True, download_name="products.csv", mimetype="text/csv")


# ----------------------------
# Preview helpers
# ----------------------------
@app.get("/preview/cert/<int:pid>")
def preview_cert(pid: int):
    p = Product.query.get_or_404(pid)
    path = p.cert_path()
    if not path or not path.exists():
        abort(404)
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        return send_file(path, as_attachment=False)
    return send_file(path, as_attachment=True)


@app.get("/preview/image/<int:pid>")
def preview_image(pid: int):
    p = Product.query.get_or_404(pid)
    path = p.image_path()
    if not path or not path.exists():
        abort(404)
    return send_file(path, as_attachment=False)


# ----------------------------
# Templates
# ----------------------------
TPL_BASE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Product Docs Manager</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    .line-clamp-2 { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .thumb { width: 72px; height: 72px; object-fit: cover; border-radius: 0.5rem; border: 1px solid #e5e7eb; }
  </style>
</head>
<body class="bg-gray-50 text-gray-900">
  <div class="max-w-6xl mx-auto p-6">
    <header class="flex items-center justify-between mb-6">
      <h1 class="text-2xl font-semibold">Product Cut Sheets & Certifications</h1>
      <nav class="flex items-center gap-2">
        <a href="{{ url_for('index') }}" class="px-3 py-2 rounded-xl bg-white shadow hover:shadow-md">Home</a>
        <a href="{{ url_for('product_new') }}" class="px-3 py-2 rounded-xl bg-blue-600 text-white hover:bg-blue-700">New Product</a>
        <a href="{{ url_for('export_csv') }}" class="px-3 py-2 rounded-xl bg-white shadow hover:shadow-md">Export CSV</a>
      </nav>
    </header>

    {% with messages = get_flashed_messages(with_categories=true) %}
      {% if messages %}
        <div class="space-y-2 mb-4">
          {% for category, msg in messages %}
            <div class="px-4 py-3 rounded-xl {% if category == 'error' %}bg-red-100 text-red-700{% elif category == 'success' %}bg-green-100 text-green-700{% else %}bg-gray-100{% endif %}">{{ msg }}</div>
          {% endfor %}
        </div>
      {% endif %}
    {% endwith %}

    {% block content %}{% endblock %}
    <footer class="mt-10 text-sm text-gray-500">&copy; {{ now().year }} Product Docs Manager</footer>
  </div>

  <script>
  function toggleAll(source) {
    document.querySelectorAll('input[name="ids"]').forEach(cb => cb.checked = source.checked);
  }
  function confirmDelete(){ return confirm('Delete this product? This will remove stored files.'); }
  </script>
</body>
</html>
"""

TPL_INDEX = r"""
{% extends "base.html" %}
{% block content %}
  <div class="bg-white rounded-2xl shadow p-4 mb-6">
    <form method="get" class="flex flex-wrap items-end gap-3">
      <div>
        <label class="block text-sm text-gray-600">Search</label>
        <input type="text" name="q" value="{{ q }}" placeholder="Name, Model Number, Notes..." class="px-3 py-2 rounded-xl border w-64">
      </div>
      <div>
        <label class="block text-sm text-gray-600">Show</label>
        <select name="missing" class="px-3 py-2 rounded-xl border">
          <option value="">All</option>
          <option value="cutsheet" {% if missing=='cutsheet' %}selected{% endif %}>Missing cutsheet</option>
          <option value="cert" {% if missing=='cert' %}selected{% endif %}>Missing certification</option>
        </select>
      </div>
      <button class="px-4 py-2 bg-gray-900 text-white rounded-xl">Filter</button>
    </form>
  </div>

  <form method="post" action="{{ url_for('bulk_download') }}" class="bg-white rounded-2xl shadow overflow-hidden">
    <div class="flex items-center justify-between px-4 py-3 border-b">
      <div class="flex items-center gap-2">
        <input type="checkbox" onclick="toggleAll(this)">
        <span class="text-sm text-gray-600">Select all</span>
      </div>
      <div class="flex items-center gap-2">
        <select name="action" class="px-3 py-2 rounded-xl border">
          <option value="cutsheet">Zip: Cutsheets</option>
          <option value="cert">Zip: Certifications</option>
          <option value="combined">Zip: Combined PDFs</option>
        </select>
        <button class="px-4 py-2 rounded-xl bg-blue-600 text-white hover:bg-blue-700">Download Selected</button>
      </div>
    </div>

    <table class="w-full text-sm">
      <thead class="bg-gray-50">
        <tr class="text-left">
          <th class="p-3 w-10"></th>
          <th class="p-3">Image</th>
          <th class="p-3">Name</th>
          <th class="p-3">Model Number</th>
          <th class="p-3">Cutsheet</th>
          <th class="p-3">Certification</th>
          <th class="p-3 text-right">Actions</th>
        </tr>
      </thead>
      <tbody>
        {% for p in products %}
          <tr class="border-t">
            <td class="p-3 align-top"><input type="checkbox" name="ids" value="{{ p.id }}"></td>
            <td class="p-3 align-top">
              {% if p.image_filename %}
                <a href="{{ url_for('preview_image', pid=p.id) }}" target="_blank"><img class="thumb" src="{{ url_for('preview_image', pid=p.id) }}" alt="{{ p.name }}"></a>
              {% else %}
                <div class="text-gray-400">—</div>
              {% endif %}
            </td>
            <td class="p-3 align-top">
              <div class="font-medium">{{ p.name }}</div>
              {% if p.notes %}<div class="text-gray-500 line-clamp-2 max-w-xs">{{ p.notes }}</div>{% endif %}
              <div class="text-xs text-gray-400">Added {{ p.created_at.strftime('%Y-%m-%d') }}</div>
            </td>
            <td class="p-3 align-top">{{ p.model_number or p.sku or '—' }}</td>
            <td class="p-3 align-top">
              {% if p.cutsheet_filename %}
                <span class="inline-flex items-center px-2 py-1 rounded-full bg-green-100 text-green-700">Present</span>
                <div class="mt-2">
                  <a class="text-blue-700 hover:underline" href="{{ url_for('download_cutsheet', pid=p.id) }}">Download</a>
                </div>
              {% else %}
                <span class="inline-flex items-center px-2 py-1 rounded-full bg-red-100 text-red-700">Missing</span>
              {% endif %}
            </td>
            <td class="p-3 align-top">
              {% if p.cert_filename %}
                <span class="inline-flex items-center px-2 py-1 rounded-full bg-green-100 text-green-700">Present</span>
                <div class="mt-2 flex items-center gap-3">
                  <a class="text-blue-700 hover:underline" href="{{ url_for('download_cert', pid=p.id) }}">Download</a>
                  <a class="text-blue-700 hover:underline" href="{{ url_for('preview_cert', pid=p.id) }}" target="_blank">Preview</a>
                </div>
              {% else %}
                <span class="inline-flex items-center px-2 py-1 rounded-full bg-red-100 text-red-700">Missing</span>
              {% endif %}
            </td>
            <td class="p-3 align-top text-right">
              <div class="flex justify-end gap-2 flex-wrap">
                <a href="{{ url_for('product_edit', pid=p.id) }}" class="px-3 py-2 rounded-xl bg-white border hover:bg-gray-50">Edit</a>
                {% if p.cutsheet_filename and p.cert_filename %}
                  <a href="{{ url_for('download_combined', pid=p.id) }}" class="px-3 py-2 rounded-xl bg-gray-900 text-white">Combined PDF</a>
                {% endif %}
                <form method="post" action="{{ url_for('product_delete', pid=p.id) }}" onsubmit="return confirmDelete()">
                  <button class="px-3 py-2 rounded-xl bg-red-600 text-white">Delete</button>
                </form>
              </div>
            </td>
          </tr>
        {% else %}
          <tr><td colspan="7" class="p-6 text-center text-gray-500">No products found. Add one!</td></tr>
        {% endfor %}
      </tbody>
    </table>
  </form>
{% endblock %}
"""

TPL_FORM = r"""
{% extends "base.html" %}
{% block content %}
  <div class="bg-white rounded-2xl shadow p-6">
    <form method="post" enctype="multipart/form-data" action="{{ url_for('product_create') if not product else url_for('product_update', pid=product.id) }}" class="space-y-4">
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div>
          <label class="block text-sm text-gray-600">Name *</label>
          <input required name="name" value="{{ product.name if product else '' }}" class="px-3 py-2 rounded-xl border w-full"/>
        </div>
        <div>
          <label class="block text-sm text-gray-600">Model Number</label>
          <input name="model_number" value="{{ (product.model_number or product.sku) if product else '' }}" class="px-3 py-2 rounded-xl border w-full"/>
        </div>
      </div>

      <div>
        <label class="block text-sm text-gray-600">Notes</label>
        <textarea name="notes" rows="3" class="px-3 py-2 rounded-xl border w-full">{{ product.notes if product else '' }}</textarea>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div>
          <label class="block text-sm text-gray-600">Cutsheet (PDF)</label>
          <input type="file" name="cutsheet" accept="application/pdf" class="block w-full text-sm text-gray-600"/>
          {% if product and product.cutsheet_filename %}
            <div class="mt-2 text-sm">Current: <a class="text-blue-700 hover:underline" href="{{ url_for('download_cutsheet', pid=product.id) }}">Download</a></div>
          {% endif %}
        </div>
        <div>
          <label class="block text-sm text-gray-600">Certification (PNG/JPG/PDF)</label>
          <input type="file" name="cert" accept="image/png,image/jpeg,application/pdf" class="block w-full text-sm text-gray-600"/>
          {% if product and product.cert_filename %}
            <div class="mt-2 text-sm">Current: <a class="text-blue-700 hover:underline" href="{{ url_for('download_cert', pid=product.id) }}">Download</a> · <a class="text-blue-700 hover:underline" href="{{ url_for('preview_cert', pid=product.id) }}" target="_blank">Preview</a></div>
          {% endif %}
        </div>
        <div>
          <label class="block text-sm text-gray-600">Product Image (PNG/JPG/WEBP)</label>
          <input type="file" name="image" accept="image/png,image/jpeg,image/webp" class="block w-full text-sm text-gray-600"/>
          {% if product and product.image_filename %}
            <div class="mt-2 text-sm">Current: <a class="text-blue-700 hover:underline" href="{{ url_for('preview_image', pid=product.id) }}" target="_blank">Preview</a></div>
          {% endif %}
        </div>
      </div>

      <div class="flex items-center gap-2">
        <button class="px-4 py-2 rounded-xl bg-blue-600 text-white hover:bg-blue-700">{{ 'Save changes' if product else 'Create product' }}</button>
        <a href="{{ url_for('index') }}" class="px-4 py-2 rounded-xl bg-white border">Cancel</a>
      </div>
    </form>
  </div>
{% endblock %}
"""

# Register in-memory templates for Jinja loader
_existing_loader = app.jinja_loader
_dict_loader = DictLoader({
    "base.html": TPL_BASE,
    "index.html": TPL_INDEX,
    "form.html": TPL_FORM,
})
if _existing_loader is None:
    app.jinja_loader = _dict_loader
else:
    app.jinja_loader = ChoiceLoader([_existing_loader, _dict_loader])
app.jinja_env.globals.update(now=datetime.utcnow)


# ----------------------------
# Built-in tests (kept; added minimal image test)
# ----------------------------
import unittest

class ProductAppTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        pass

    @staticmethod
    def _make_sample_pdf_bytes(text: str = "Hello") -> bytes:
        img = Image.new("RGB", (300, 100), color=(255, 255, 255))
        bio = io.BytesIO()
        img.save(bio, format="PDF")
        return bio.getvalue()

    @staticmethod
    def _make_sample_png_bytes() -> bytes:
        img = Image.new("RGB", (120, 80), color=(100, 180, 240))
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        return bio.getvalue()

    def test_index(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Product Cut Sheets", resp.data)

    def test_create_minimal_product_and_delete(self):
        resp = self.client.post(
            "/product/create",
            data={"name": "Widget A", "sku": "W-A", "notes": "test"},  # keep sku for back-compat
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Product created", resp.data)
        resp = self.client.get("/?q=Widget%20A")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Widget A", resp.data)
        with self.app.app_context():
            p = Product.query.filter_by(name="Widget A").first()
            self.assertIsNotNone(p)
            del_resp = self.client.post(f"/product/{p.id}/delete", follow_redirects=True)
            self.assertEqual(del_resp.status_code, 200)
            self.assertIn(b"Product deleted", del_resp.data)

    def test_upload_and_combined_download(self):
        pdf_bytes = self._make_sample_pdf_bytes()
        png_bytes = self._make_sample_png_bytes()
        data = {
            "name": "Widget B",
            "sku": "W-B",
            "notes": "with files",
            "cutsheet": (io.BytesIO(pdf_bytes), "cutsheet.pdf"),
            "cert": (io.BytesIO(png_bytes), "cert.png"),
        }
        resp = self.client.post("/product/create", data=data, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            p = Product.query.filter_by(name="Widget B").first()
            self.assertIsNotNone(p)
            r1 = self.client.get(f"/download/cutsheet/{p.id}")
            self.assertEqual(r1.status_code, 200)
            self.assertTrue(r1.data.startswith(b"%PDF"))
            r2 = self.client.get(f"/download/cert/{p.id}")
            self.assertEqual(r2.status_code, 200)
            r3 = self.client.get(f"/download/combined/{p.id}")
            self.assertEqual(r3.status_code, 200)
            self.assertTrue(r3.data.startswith(b"%PDF"))
            self.client.post(f"/product/{p.id}/delete", follow_redirects=True)

    def test_bulk_zip(self):
        for idx in (1, 2):
            pdf_bytes = self._make_sample_pdf_bytes(f"Doc {idx}")
            png_bytes = self._make_sample_png_bytes()
            data = {
                "name": f"Bundle {idx}",
                "sku": f"B-{idx}",
                "notes": "bulk",
                "cutsheet": (io.BytesIO(pdf_bytes), "cs.pdf"),
                "cert": (io.BytesIO(png_bytes), "cert.png"),
            }
            self.client.post("/product/create", data=data, content_type="multipart/form-data", follow_redirects=True)
        with self.app.app_context():
            ids = [str(p.id) for p in Product.query.filter(Product.name.like("Bundle %")).all()]
        form = {"action": "combined", "ids": ids}
        resp = self.client.post("/bulk/download", data=form)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/zip")
        with self.app.app_context():
            for p in Product.query.filter(Product.name.like("Bundle %")).all():
                self.client.post(f"/product/{p.id}/delete", follow_redirects=True)

    def test_combined_requires_both_files(self):
        pdf_bytes = self._make_sample_pdf_bytes()
        data = {
            "name": "Only Cutsheet",
            "sku": "OC-1",
            "cutsheet": (io.BytesIO(pdf_bytes), "cutsheet.pdf"),
        }
        self.client.post("/product/create", data=data, content_type="multipart/form-data", follow_redirects=True)
        with self.app.app_context():
            p = Product.query.filter_by(name="Only Cutsheet").first()
            self.assertIsNotNone(p)
            r = self.client.get(f"/download/combined/{p.id}")
            self.assertEqual(r.status_code, 404)
            self.client.post(f"/product/{p.id}/delete", follow_redirects=True)

    def test_bulk_requires_selection(self):
        resp = self.client.post("/bulk/download", data={"action": "combined"}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"No products selected", resp.data)

    def test_image_upload_and_preview(self):
        img_bytes = self._make_sample_png_bytes()
        data = {
            "name": "With Image",
            "sku": "IMG-1",
            "image": (io.BytesIO(img_bytes), "photo.png"),
        }
        resp = self.client.post("/product/create", data=data, content_type="multipart/form-data", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            p = Product.query.filter_by(name="With Image").first()
            self.assertIsNotNone(p)
            r = self.client.get(f"/preview/image/{p.id}")
            self.assertEqual(r.status_code, 200)
            self.client.post(f"/product/{p.id}/delete", follow_redirects=True)


if __name__ == "__main__":
    mode = os.environ.get("MODE")  # server|test|snapshot

    if mode == "test" or os.environ.get("RUN_TESTS") == "1":
        with tempfile.TemporaryDirectory() as tmp:
            BASE_DIR = Path(tmp)
            UPLOAD_DIR = BASE_DIR / "uploads"
            CUTSHEETS_DIR = UPLOAD_DIR / "cutsheets"
            CERTS_DIR = UPLOAD_DIR / "certifications"
            IMAGES_DIR = UPLOAD_DIR / "images"
            DB_PATH = BASE_DIR / "products.db"
            for d in (UPLOAD_DIR, CUTSHEETS_DIR, CERTS_DIR, IMAGES_DIR):
                d.mkdir(parents=True, exist_ok=True)
            app.config.update(SQLALCHEMY_DATABASE_URI=f"sqlite:///{DB_PATH}")
            with app.app_context():
                db.drop_all()
                db.create_all()
            import unittest as _unittest
            _unittest.main(argv=["-m", "app"], exit=False)

    elif mode == "server":
        host = os.getenv("HOST", "0.0.0.0")
        port = int(os.getenv("PORT", "8000"))
        debug = os.getenv("FLASK_DEBUG", "0") == "1"
        print(f"Starting server on http://{host}:{port}")
        app.run(host=host, port=port, debug=debug)

    else:
        SNAP_DIR = BASE_DIR if os.access(BASE_DIR, os.W_OK) else Path.cwd()
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        with app.app_context():
            if Product.query.count() == 0:
                db.session.add(Product(name="Sample Widget", model_number="SW-001", sku="SW-001", notes="Example item"))
                db.session.add(Product(name="Gadget Pro", model_number="GP-200", sku="GP-200", notes="Missing files demo"))
                db.session.commit()
        with app.test_request_context("/"):
            html = index()
        snap_path = SNAP_DIR / "product_manager_snapshot.html"
        if hasattr(html, "data"):
            content = html.data if isinstance(html.data, (bytes, bytearray)) else str(html).encode("utf-8")
        else:
            content = html.encode("utf-8")
        snap_path.write_bytes(content)
        print("Snapshot written:", snap_path.resolve())
        print("To run the web server locally: set MODE=server and execute this file.")
