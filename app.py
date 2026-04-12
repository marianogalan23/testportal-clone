import csv
import io
import os
import psycopg2
import psycopg2.extras
import docx
from datetime import datetime, timezone
from flask import Flask, render_template_string, request, redirect, url_for, session, Response
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")

_DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Railway (and some other hosts) use postgres:// but psycopg2 needs postgresql://
if _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)


def get_conn():
    return psycopg2.connect(_DATABASE_URL)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS quizzes (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        filename TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id SERIAL PRIMARY KEY,
        quiz_id INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        correct_answer TEXT NOT NULL,
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS options (
        id SERIAL PRIMARY KEY,
        question_id INTEGER NOT NULL,
        option_text TEXT NOT NULL,
        FOREIGN KEY (question_id) REFERENCES questions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS results (
        id SERIAL PRIMARY KEY,
        student_name TEXT NOT NULL,
        quiz_id INTEGER NOT NULL,
        score INTEGER NOT NULL,
        total INTEGER NOT NULL,
        submitted_at TIMESTAMP DEFAULT NOW(),
        duration_seconds INTEGER,
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'student'
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS assignments (
        id SERIAL PRIMARY KEY,
        quiz_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        UNIQUE(quiz_id, username),
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS answers (
        id SERIAL PRIMARY KEY,
        result_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        selected_answer TEXT,
        FOREIGN KEY (result_id) REFERENCES results(id),
        FOREIGN KEY (question_id) REFERENCES questions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS classes (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS class_members (
        id SERIAL PRIMARY KEY,
        class_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        UNIQUE(class_id, username),
        FOREIGN KEY (class_id) REFERENCES classes(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS class_assignments (
        id SERIAL PRIMARY KEY,
        quiz_id INTEGER NOT NULL,
        class_id INTEGER NOT NULL,
        UNIQUE(quiz_id, class_id),
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id),
        FOREIGN KEY (class_id) REFERENCES classes(id)
    )
    """)

    # Safe migrations for databases that predate the full schema
    cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP DEFAULT NOW()")
    cur.execute("ALTER TABLE results ADD COLUMN IF NOT EXISTS duration_seconds INTEGER")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'student'")

    conn.commit()
    conn.close()


def clean_option_text(text):
    return text.replace("✓", "").strip()


def fmt_dt(val):
    """Format a datetime value (or string) for display. Returns '—' for None."""
    if val is None:
        return "—"
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    return str(val)


def format_duration(seconds):
    """Return a human-readable duration string, e.g. '4m 32s' or '45s'."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


def compute_percentile(score, total, quiz_id, conn):
    """
    Return the percentile rank (0–100) of this result among all results
    for the same quiz. Uses percentage score so submissions with different
    totals are comparable.
    """
    if total == 0:
        return 0
    pct = score / total
    cur = conn.cursor()
    cur.execute("SELECT score, total FROM results WHERE quiz_id = %s", (quiz_id,))
    all_scores = cur.fetchall()
    if not all_scores:
        return 0
    at_or_below = sum(1 for s, t in all_scores if t and s / t <= pct)
    return round(at_or_below / len(all_scores) * 100)


def parse_quiz_from_docx(file_path):
    doc = docx.Document(file_path)
    quiz = []
    current_question = None

    for para in doc.paragraphs:
        text = para.text.strip()

        if text:
            if text.startswith("Q:"):
                if current_question:
                    quiz.append(current_question)

                current_question = {
                    "question": text[2:].strip(),
                    "options": [],
                    "answer": None
                }

            elif text.startswith("A:"):
                if current_question is not None:
                    option_text = clean_option_text(text[2:].strip())
                    is_correct = any(run.bold for run in para.runs)

                    current_question["options"].append(option_text)

                    if is_correct:
                        current_question["answer"] = option_text

    if current_question:
        quiz.append(current_question)

    return quiz


def save_quiz_to_db(title, filename, quiz_data):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        "INSERT INTO quizzes (title, filename) VALUES (%s, %s) RETURNING id",
        (title, filename)
    )
    quiz_id = cur.fetchone()[0]

    for q in quiz_data:
        cur.execute(
            "INSERT INTO questions (quiz_id, question_text, correct_answer) VALUES (%s, %s, %s) RETURNING id",
            (quiz_id, q["question"], q["answer"])
        )
        question_id = cur.fetchone()[0]

        for option in q["options"]:
            cur.execute(
                "INSERT INTO options (question_id, option_text) VALUES (%s, %s)",
                (question_id, option)
            )

    conn.commit()
    conn.close()


HOME_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TestPortal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --primary: #4f46e5; --primary-hover: #4338ca; --primary-soft: #eef2ff;
            --danger: #dc2626; --danger-soft: #fef2f2; --danger-border: #fecaca;
            --success: #16a34a; --success-soft: #f0fdf4; --success-border: #bbf7d0;
            --gray-50: #f9fafb; --gray-100: #f3f4f6; --gray-200: #e5e7eb;
            --gray-300: #d1d5db; --gray-400: #9ca3af; --gray-500: #6b7280;
            --gray-600: #4b5563; --gray-700: #374151; --gray-900: #111827;
            --bg: #f9fafb; --card: #fff; --border: #e5e7eb;
            --text: #111827; --text-sec: #374151; --muted: #6b7280; --dim: #9ca3af;
            --radius: 8px; --radius-lg: 10px;
            --shadow: 0 1px 2px rgba(0,0,0,0.04);
            --ring: 0 0 0 2px rgba(79,70,229,0.15);
        }
        body { font-family: 'Inter', system-ui, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; -webkit-font-smoothing: antialiased; }

        /* ── Nav ── */
        nav { background: var(--card); border-bottom: 1px solid var(--border); padding: 0 24px; display: flex; align-items: center; height: 52px; gap: 4px; position: sticky; top: 0; z-index: 50; }
        .brand { font-weight: 700; font-size: 15px; color: var(--text); text-decoration: none; margin-right: 20px; }
        .brand b { color: var(--primary); }
        .nav-link { text-decoration: none; color: var(--muted); font-size: 13px; font-weight: 500; padding: 6px 10px; border-radius: var(--radius); transition: color .15s, background .15s; }
        .nav-link:hover { color: var(--text); background: var(--gray-50); }
        .nav-link.active { color: var(--primary); background: var(--primary-soft); }
        .nav-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }
        .nav-user { font-size: 12px; color: var(--muted); }
        .btn-logout { padding: 4px 10px; font-size: 12px; font-weight: 500; background: none; color: var(--muted); border: 1px solid var(--border); border-radius: var(--radius); cursor: pointer; text-decoration: none; transition: color .15s, border-color .15s; }
        .btn-logout:hover { color: var(--text); border-color: var(--gray-300); }

        /* ── Layout ── */
        .page { max-width: 800px; margin: 0 auto; padding: 32px 24px 80px; }
        .page-header { margin-bottom: 32px; }
        .page-header h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.3px; }
        .page-header p { color: var(--muted); font-size: 14px; margin-top: 4px; }

        /* ── Flash ── */
        .flash { padding: 10px 14px; border-radius: var(--radius); font-size: 13px; font-weight: 500; margin-bottom: 24px; }
        .flash.success { background: var(--success-soft); border: 1px solid var(--success-border); color: var(--success); }
        .flash.error { background: var(--danger-soft); border: 1px solid var(--danger-border); color: var(--danger); }

        /* ── Section ── */
        .section { margin-bottom: 32px; }
        .section-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--dim); margin-bottom: 10px; }

        /* ── Card ── */
        .card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: 20px; }
        .card + .card { margin-top: 10px; }
        .card-title { font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 14px; }
        .card-help { margin-top: 10px; font-size: 12px; color: var(--dim); }
        .card-help code { font-size: 11px; background: var(--gray-100); padding: 1px 5px; border-radius: 3px; font-family: 'SF Mono', monospace; }

        /* ── Buttons ── */
        .btn { display: inline-flex; align-items: center; justify-content: center; gap: 5px; padding: 8px 16px; font-size: 13px; font-weight: 600; border: none; border-radius: var(--radius); cursor: pointer; text-decoration: none; font-family: inherit; transition: background .15s, box-shadow .15s; }
        .btn:active { transform: scale(0.98); }
        .btn-primary { background: var(--primary); color: #fff; }
        .btn-primary:hover { background: var(--primary-hover); }
        .btn-danger { background: none; color: var(--danger); border: 1px solid var(--danger-border); padding: 5px 10px; font-size: 12px; }
        .btn-danger:hover { background: var(--danger-soft); }
        .btn-ghost { background: none; color: var(--muted); padding: 5px 10px; font-size: 12px; font-weight: 500; border: 1px solid var(--border); border-radius: var(--radius); cursor: pointer; font-family: inherit; transition: color .15s, border-color .15s; }
        .btn-ghost:hover { color: var(--primary); border-color: var(--primary); }

        /* ── File input ── */
        .file-zone { border: 1px dashed var(--gray-300); border-radius: var(--radius); padding: 14px; background: var(--gray-50); text-align: center; }
        .file-zone input[type="file"] { font-size: 13px; color: var(--muted); cursor: pointer; background: none; border: none; width: 100%; }
        .file-zone input[type="file"]::-webkit-file-upload-button { background: var(--card); border: 1px solid var(--border); border-radius: 5px; padding: 4px 10px; font-size: 12px; font-weight: 500; color: var(--text-sec); cursor: pointer; margin-right: 8px; font-family: inherit; }

        /* ── Quiz list ── */
        .quiz-list { list-style: none; }
        .quiz-item { border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 6px; background: var(--card); overflow: hidden; transition: border-color .15s; }
        .quiz-item:hover { border-color: var(--gray-300); }
        .quiz-item-row { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; gap: 12px; }
        .quiz-item-name { font-weight: 500; font-size: 14px; color: var(--text); }
        .quiz-item-actions { display: flex; align-items: center; gap: 5px; flex-shrink: 0; }

        /* ── Toggle panels ── */
        .assign-toggle, .class-toggle, .cls-toggle { display: none; }
        .assign-panel, .class-panel, .cls-panel { display: none; padding: 10px 16px; border-top: 1px solid var(--gray-100); background: var(--gray-50); align-items: center; gap: 8px; flex-wrap: wrap; }
        .assign-toggle:checked ~ .assign-panel, .class-toggle:checked ~ .class-panel, .cls-toggle:checked ~ .cls-panel { display: flex; }
        .assign-panel label, .class-panel label, .cls-panel label { font-size: 12px; font-weight: 500; color: var(--muted); white-space: nowrap; }
        .panel-input { padding: 7px 10px; font-size: 13px; border: 1px solid var(--border); border-radius: var(--radius); outline: none; background: var(--card); font-family: inherit; transition: border-color .15s; }
        .panel-input:focus { border-color: var(--primary); box-shadow: var(--ring); }
        input[type="text"].panel-input { width: 180px; }
        select.panel-input { cursor: pointer; }
        .btn-panel { padding: 7px 14px; font-size: 12px; font-weight: 600; background: var(--primary); color: #fff; border: none; border-radius: var(--radius); cursor: pointer; font-family: inherit; transition: background .15s; }
        .btn-panel:hover { background: var(--primary-hover); }

        /* ── Class list ── */
        .class-list { list-style: none; }
        .class-item { border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: 6px; background: var(--card); overflow: hidden; }
        .class-item-row { display: flex; align-items: center; justify-content: space-between; padding: 10px 16px; }
        .class-item-name { font-weight: 500; font-size: 14px; color: var(--text); display: flex; align-items: center; gap: 8px; }
        .member-badge { font-size: 11px; color: var(--dim); background: var(--gray-100); border-radius: 99px; padding: 1px 8px; font-weight: 500; }
        .create-class-row { display: flex; gap: 8px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; }
        .create-class-row input[type="text"] { flex: 1; min-width: 160px; padding: 8px 12px; font-size: 13px; border: 1px solid var(--border); border-radius: var(--radius); outline: none; background: var(--card); font-family: inherit; transition: border-color .15s; }
        .create-class-row input[type="text"]:focus { border-color: var(--primary); box-shadow: var(--ring); }

        .empty { text-align: center; color: var(--dim); font-size: 13px; padding: 24px 0; }

        .import-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        @media (max-width: 640px) { .import-grid { grid-template-columns: 1fr; } }

        /* ── Student quiz cards ── */
        .student-quiz-grid { display: flex; flex-direction: column; gap: 6px; }
        .student-quiz-card { display: flex; align-items: center; justify-content: space-between; padding: 16px 18px; background: var(--card); border: 1px solid var(--border); border-radius: var(--radius-lg); text-decoration: none; transition: border-color .15s, box-shadow .15s; }
        .student-quiz-card:hover { border-color: var(--gray-300); box-shadow: var(--shadow); }
        .sq-name { font-weight: 500; font-size: 14px; color: var(--text); }
        .sq-arrow { color: var(--dim); font-size: 18px; transition: color .15s; }
        .student-quiz-card:hover .sq-arrow { color: var(--primary); }

        @media (max-width: 600px) { nav { padding: 0 16px; } .page { padding: 24px 16px 60px; } }
    </style>
</head>
<body>

<nav>
    <a href="/" class="brand">Test<b>Portal</b></a>
    <a href="/" class="nav-link active">Home</a>
    {% if is_teacher %}<a href="/results" class="nav-link">Results</a>{% endif %}
    <span class="nav-right">
        {% if current_user %}
            <span class="nav-user">{{ current_user }}</span>
            <a href="/logout" class="btn-logout">Log out</a>
        {% else %}
            <a href="/login" class="btn-logout">Login</a>
        {% endif %}
    </span>
</nav>

<div class="page">

    {% if flash_message %}
        <div class="flash {{ flash_type }}">{{ flash_message }}</div>
    {% endif %}

    {% if not is_teacher %}
        <div class="page-header">
            <h1>Your Quizzes</h1>
            <p>Select a quiz to begin.</p>
        </div>

        {% if quizzes %}
            <div class="student-quiz-grid">
                {% for quiz in quizzes %}
                    <a href="/quiz/{{ quiz[0] }}" class="student-quiz-card">
                        <span class="sq-name">{{ quiz[1] }}</span>
                        <span class="sq-arrow">&#8250;</span>
                    </a>
                {% endfor %}
            </div>
        {% else %}
            <div class="card">
                <p class="empty">No quizzes assigned to you yet.</p>
            </div>
        {% endif %}

    {% else %}

        <div class="page-header">
            <h1>Dashboard</h1>
            <p>Manage quizzes, students, and classes.</p>
        </div>

        <div class="section">
            <div class="section-label">Import</div>
            <div class="import-grid">
                <div class="card">
                    <div class="card-title">Quiz (.docx)</div>
                    <form method="POST" enctype="multipart/form-data" action="/import">
                        <div class="file-zone">
                            <input type="file" name="quiz_file" accept=".docx" required>
                        </div>
                        <button type="submit" class="btn btn-primary" style="margin-top:12px;width:100%;">Upload &amp; Import</button>
                    </form>
                </div>
                <div class="card">
                    <div class="card-title">Students (.csv)</div>
                    <form method="POST" enctype="multipart/form-data" action="/import-students">
                        <div class="file-zone">
                            <input type="file" name="students_csv" accept=".csv,.txt" required>
                        </div>
                        <button type="submit" class="btn btn-primary" style="margin-top:12px;width:100%;">Upload &amp; Import</button>
                    </form>
                    <p class="card-help">Format: <code>username,password</code> per line.</p>
                </div>
            </div>
        </div>

        <div class="section">
            <div class="section-label">Classes</div>
            <div class="card">
                <form method="POST" action="/class/create" class="create-class-row">
                    <input type="text" name="name" placeholder="New class name" required>
                    <button type="submit" class="btn btn-primary">Create</button>
                </form>
                {% if classes %}
                    <ul class="class-list">
                        {% for cls in classes %}
                            <li class="class-item">
                                <input type="checkbox" id="cls-{{ cls[0] }}" class="cls-toggle">
                                <div class="class-item-row">
                                    <span class="class-item-name">
                                        {{ cls[1] }}
                                        <span class="member-badge">{{ cls[2] }}</span>
                                    </span>
                                    <label for="cls-{{ cls[0] }}" class="btn-ghost">+ Add member</label>
                                </div>
                                <div class="cls-panel">
                                    <form method="POST" action="/class/{{ cls[0] }}/add-member" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                                        <label>Username:</label>
                                        <input type="text" name="username" class="panel-input" placeholder="e.g. alice" required>
                                        <button type="submit" class="btn-panel">Add</button>
                                    </form>
                                </div>
                            </li>
                        {% endfor %}
                    </ul>
                {% else %}
                    <p class="empty">No classes yet.</p>
                {% endif %}
            </div>
        </div>

        <div class="section">
            <div class="section-label">Quizzes</div>
            <div class="card" style="padding:10px;">
                {% if quizzes %}
                    <ul class="quiz-list">
                        {% for quiz in quizzes %}
                            <li class="quiz-item">
                                <input type="checkbox" id="assign-{{ quiz[0] }}" class="assign-toggle">
                                <input type="checkbox" id="class-assign-{{ quiz[0] }}" class="class-toggle">
                                <div class="quiz-item-row">
                                    <span class="quiz-item-name">{{ quiz[1] }}</span>
                                    <div class="quiz-item-actions">
                                        <label for="assign-{{ quiz[0] }}" class="btn-ghost">Assign user</label>
                                        {% if classes %}
                                            <label for="class-assign-{{ quiz[0] }}" class="btn-ghost">Assign class</label>
                                        {% endif %}
                                        <form method="POST" action="/quiz/{{ quiz[0] }}/delete" onsubmit="return confirm('Delete &quot;{{ quiz[1] }}&quot; and all its data?');">
                                            <button type="submit" class="btn btn-danger">Delete</button>
                                        </form>
                                    </div>
                                </div>
                                <div class="assign-panel">
                                    <form method="POST" action="/quiz/{{ quiz[0] }}/assign" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                                        <label>Assign to:</label>
                                        <input type="text" name="username" class="panel-input" placeholder="username" required>
                                        <button type="submit" class="btn-panel">Assign</button>
                                    </form>
                                </div>
                                {% if classes %}
                                    <div class="class-panel">
                                        <form method="POST" action="/quiz/{{ quiz[0] }}/assign-class" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
                                            <label>Class:</label>
                                            <select name="class_id" class="panel-input" required>
                                                {% for cls in classes %}
                                                    <option value="{{ cls[0] }}">{{ cls[1] }}</option>
                                                {% endfor %}
                                            </select>
                                            <button type="submit" class="btn-panel">Assign</button>
                                        </form>
                                    </div>
                                {% endif %}
                            </li>
                        {% endfor %}
                    </ul>
                {% else %}
                    <p class="empty">No quizzes imported yet.</p>
                {% endif %}
            </div>
        </div>
    {% endif %}
</div>
</body>
</html>
"""


QUIZ_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ quiz_title }} — TestPortal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --primary: #4f46e5; --primary-hover: #4338ca; --primary-soft: #eef2ff;
            --danger: #dc2626; --danger-soft: #fef2f2; --danger-border: #fecaca;
            --success: #16a34a; --success-soft: #f0fdf4; --success-border: #bbf7d0;
            --gray-50: #f9fafb; --gray-100: #f3f4f6; --gray-200: #e5e7eb;
            --gray-300: #d1d5db; --gray-400: #9ca3af; --gray-500: #6b7280;
            --gray-600: #4b5563; --gray-700: #374151; --gray-900: #111827;
            --bg: #f9fafb; --card: #fff; --border: #e5e7eb;
            --text: #111827; --text-sec: #374151; --muted: #6b7280; --dim: #9ca3af;
            --radius: 8px; --radius-lg: 10px;
            --shadow: 0 1px 2px rgba(0,0,0,0.04);
            --ring: 0 0 0 2px rgba(79,70,229,0.15);
        }
        body { font-family: 'Inter', system-ui, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; -webkit-font-smoothing: antialiased; }

        /* ── Nav ── */
        nav { background: var(--card); border-bottom: 1px solid var(--border); padding: 0 24px; display: flex; align-items: center; height: 52px; gap: 4px; position: sticky; top: 0; z-index: 50; }
        .brand { font-weight: 700; font-size: 15px; color: var(--text); text-decoration: none; margin-right: 20px; }
        .brand b { color: var(--primary); }
        .nav-link { text-decoration: none; color: var(--muted); font-size: 13px; font-weight: 500; padding: 6px 10px; border-radius: var(--radius); transition: color .15s, background .15s; }
        .nav-link:hover { color: var(--text); background: var(--gray-50); }
        .nav-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }
        .nav-user { font-size: 12px; color: var(--muted); }
        .btn-logout { padding: 4px 10px; font-size: 12px; font-weight: 500; background: none; color: var(--muted); border: 1px solid var(--border); border-radius: var(--radius); cursor: pointer; text-decoration: none; transition: color .15s, border-color .15s; }
        .btn-logout:hover { color: var(--text); border-color: var(--gray-300); }

        /* ── Layout ── */
        .page { max-width: 700px; margin: 0 auto; padding: 32px 24px 80px; }
        .page-header { margin-bottom: 28px; }
        .page-header h1 { font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }
        .taking-as {
            display: inline-block; margin-top: 8px; font-size: 12px; color: var(--muted); font-weight: 500;
        }

        /* ── Question cards ── */
        .question-card {
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius-lg); padding: 20px 22px; margin-bottom: 12px;
        }
        .question-num {
            font-size: 11px; font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.04em; color: var(--dim); margin-bottom: 6px;
        }
        .question-text {
            font-size: 15px; font-weight: 600; line-height: 1.5;
            margin-bottom: 16px; color: var(--text);
        }

        /* ── Options ── */
        .option-label {
            display: flex; align-items: center; gap: 10px;
            padding: 10px 14px; border: 1px solid var(--border);
            border-radius: var(--radius); margin-bottom: 6px;
            cursor: pointer; font-size: 14px; font-weight: 500; color: var(--text-sec);
            transition: border-color .15s, background .15s;
        }
        .option-label:hover { border-color: var(--gray-300); background: var(--gray-50); }
        .option-label input[type="radio"] {
            accent-color: var(--primary); width: 16px; height: 16px; flex-shrink: 0;
        }

        /* States */
        .option-label.option-correct {
            background: var(--success-soft); border-color: var(--success-border);
            color: var(--success); font-weight: 600; cursor: default;
        }
        .option-label.option-correct:hover { background: var(--success-soft); border-color: var(--success-border); }
        .option-label.option-incorrect {
            background: var(--danger-soft); border-color: var(--danger-border);
            color: var(--danger); font-weight: 600; cursor: default;
        }
        .option-label.option-incorrect:hover { background: var(--danger-soft); border-color: var(--danger-border); }
        .option-label.option-disabled {
            cursor: default; color: var(--dim); opacity: 0.65;
        }
        .option-label.option-disabled:hover { border-color: var(--border); background: transparent; }

        .option-badge {
            margin-left: auto; font-size: 11px; font-weight: 600;
            padding: 2px 8px; border-radius: 4px; white-space: nowrap;
        }
        .badge-correct { background: var(--success-soft); color: var(--success); }
        .badge-incorrect { background: var(--danger-soft); color: var(--danger); }
        .badge-answer { background: var(--success-soft); color: var(--success); }

        /* ── Score banner ── */
        .score-banner {
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius-lg); padding: 24px;
            margin-bottom: 24px; display: flex; align-items: center; gap: 16px;
        }
        .score-value { font-size: 28px; font-weight: 700; letter-spacing: -0.5px; color: var(--primary); }
        .score-label { font-size: 13px; color: var(--muted); font-weight: 500; margin-top: 2px; }

        /* ── Submit ── */
        .submit-row { margin-top: 24px; }
        .btn-submit {
            padding: 10px 24px; font-size: 14px; font-weight: 600;
            background: var(--primary); color: #fff; border: none;
            border-radius: var(--radius); cursor: pointer; font-family: inherit;
            transition: background .15s;
        }
        .btn-submit:hover { background: var(--primary-hover); }
        .btn-submit:active { transform: scale(0.98); }

        .back-link {
            display: inline-block; margin-top: 24px; font-size: 13px; font-weight: 500;
            color: var(--muted); text-decoration: none; transition: color .15s;
        }
        .back-link:hover { color: var(--primary); }

        @media (max-width: 600px) { .page { padding: 24px 16px 60px; } }
    </style>
</head>
<body>

<nav>
    <a href="/" class="brand">Test<b>Portal</b></a>
    <a href="/" class="nav-link">Home</a>
    {% if is_teacher %}<a href="/results" class="nav-link">Results</a>{% endif %}
    <span class="nav-right">
        {% if current_user %}
            <span class="nav-user">{{ current_user }}</span>
            <a href="/logout" class="btn-logout">Log out</a>
        {% else %}
            <a href="/login" class="btn-logout">Login</a>
        {% endif %}
    </span>
</nav>

<div class="page">
    <div class="page-header">
        <h1>{{ quiz_title }}</h1>
        <span class="taking-as">{{ current_user }}</span>
    </div>

    {% if submitted %}
        <div class="score-banner">
            <div>
                <div class="score-value">{{ score }} / {{ total }}</div>
                <div class="score-label">Your final score</div>
            </div>
        </div>
    {% endif %}

    <form method="POST">
        {% for q in questions %}
            <div class="question-card">
                <div class="question-num">Question {{ loop.index }} of {{ total }}</div>
                <div class="question-text">{{ q.text }}</div>

                {% set selected = user_answers.get("q" ~ q.id|string) if submitted else None %}
                {% for option in q.options %}
                    {% if submitted %}
                        {% if option == q.correct_answer and option == selected %}
                            <label class="option-label option-correct">
                                <input type="radio" name="q{{ q.id }}" value="{{ option }}" checked disabled>
                                {{ option }}
                                <span class="option-badge badge-correct">&#10003; Correct</span>
                            </label>
                        {% elif option == selected and option != q.correct_answer %}
                            <label class="option-label option-incorrect">
                                <input type="radio" name="q{{ q.id }}" value="{{ option }}" checked disabled>
                                {{ option }}
                                <span class="option-badge badge-incorrect">&#10007; Incorrect</span>
                            </label>
                        {% elif option == q.correct_answer %}
                            <label class="option-label option-correct">
                                <input type="radio" name="q{{ q.id }}" value="{{ option }}" disabled>
                                {{ option }}
                                <span class="option-badge badge-answer">&#10003; Correct answer</span>
                            </label>
                        {% else %}
                            <label class="option-label option-disabled">
                                <input type="radio" name="q{{ q.id }}" value="{{ option }}" disabled>
                                {{ option }}
                            </label>
                        {% endif %}
                    {% else %}
                        <label class="option-label">
                            <input type="radio" name="q{{ q.id }}" value="{{ option }}">
                            {{ option }}
                        </label>
                    {% endif %}
                {% endfor %}
            </div>
        {% endfor %}

        {% if not submitted %}
            <div class="submit-row">
                <button type="submit" class="btn-submit">Submit Quiz</button>
            </div>
        {% endif %}
    </form>

    <a href="/" class="back-link">Back to home</a>
</div>

</body>
</html>
"""


LOGIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login — TestPortal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --primary: #4f46e5; --primary-hover: #4338ca;
            --danger: #dc2626; --danger-soft: #fef2f2; --danger-border: #fecaca;
            --gray-100: #f3f4f6; --gray-200: #e5e7eb; --gray-300: #d1d5db;
            --gray-400: #9ca3af; --gray-500: #6b7280; --gray-900: #111827;
            --bg: #f9fafb; --card: #fff; --border: #e5e7eb;
            --text: #111827; --muted: #6b7280;
            --radius: 8px;
            --ring: 0 0 0 2px rgba(79,70,229,0.15);
        }

        body {
            font-family: 'Inter', system-ui, sans-serif;
            background: var(--bg); color: var(--text);
            min-height: 100vh; display: flex; align-items: center; justify-content: center;
            -webkit-font-smoothing: antialiased;
        }

        .login-wrapper { width: 100%; max-width: 380px; padding: 0 24px; }

        .login-brand { text-align: center; margin-bottom: 28px; }
        .login-brand-name {
            font-size: 20px; font-weight: 700; color: var(--text); letter-spacing: -0.3px;
        }
        .login-brand-name b { color: var(--primary); }
        .login-brand-sub {
            font-size: 14px; color: var(--muted); margin-top: 4px;
        }

        .login-card {
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: 0 1px 2px rgba(0,0,0,0.04);
            padding: 28px 24px 24px;
        }

        .field { margin-bottom: 18px; }
        .field label {
            display: block; font-size: 13px; font-weight: 600;
            color: var(--text); margin-bottom: 5px;
        }
        .field input {
            width: 100%; padding: 9px 12px; font-size: 14px;
            border: 1px solid var(--border); border-radius: var(--radius);
            outline: none; background: var(--card); font-family: inherit;
            transition: border-color .15s, box-shadow .15s;
        }
        .field input:focus { border-color: var(--primary); box-shadow: var(--ring); }
        .field input::placeholder { color: var(--gray-400); }

        .btn-login {
            width: 100%; padding: 10px; margin-top: 4px;
            font-size: 14px; font-weight: 600;
            background: var(--primary); color: #fff;
            border: none; border-radius: var(--radius); cursor: pointer; font-family: inherit;
            transition: background .15s;
        }
        .btn-login:hover { background: var(--primary-hover); }
        .btn-login:active { transform: scale(0.98); }

        .error {
            background: var(--danger-soft); border: 1px solid var(--danger-border);
            color: var(--danger); font-size: 13px; font-weight: 500;
            padding: 10px 12px; border-radius: var(--radius); margin-bottom: 18px;
        }
    </style>
</head>
<body>

<div class="login-wrapper">
    <div class="login-brand">
        <div class="login-brand-name">Test<b>Portal</b></div>
        <div class="login-brand-sub">Sign in to your account</div>
    </div>

    <div class="login-card">
        {% if error %}
            <div class="error">{{ error }}</div>
        {% endif %}

        <form method="POST">
            <div class="field">
                <label for="username">Username</label>
                <input type="text" id="username" name="username"
                       value="{{ username or '' }}" autocomplete="username" required autofocus
                       placeholder="Enter your username">
            </div>
            <div class="field">
                <label for="password">Password</label>
                <input type="password" id="password" name="password"
                       autocomplete="current-password" required
                       placeholder="Enter your password">
            </div>
            <button type="submit" class="btn-login">Sign in</button>
        </form>
    </div>
</div>

</body>
</html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("username"):
        return redirect(url_for("home"))

    error = None
    username = ""

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, role FROM users WHERE username = %s AND password = %s", (username, password))
        user = cur.fetchone()
        conn.close()

        if user:
            session["username"] = username
            session["role"] = user[1] or "student"
            return redirect(url_for("home"))
        else:
            error = "Invalid username or password."

    return render_template_string(LOGIN_HTML, error=error, username=username)


@app.route("/logout")
def logout():
    session.pop("username", None)
    session.pop("role", None)
    return redirect(url_for("login"))


def is_teacher():
    return session.get("role") == "teacher"


@app.route("/")
def home():
    if not session.get("username"):
        return redirect(url_for("login"))

    current_user = session["username"]
    conn = get_conn()
    cur = conn.cursor()

    if is_teacher():
        cur.execute("SELECT id, title FROM quizzes ORDER BY id DESC")
        quizzes = cur.fetchall()
        cur.execute("""
            SELECT c.id, c.name, COUNT(cm.id) AS member_count
            FROM classes c
            LEFT JOIN class_members cm ON cm.class_id = c.id
            GROUP BY c.id
            ORDER BY c.name
        """)
        classes = cur.fetchall()
    else:
        cur.execute("""
            SELECT DISTINCT q.id, q.title FROM quizzes q
            WHERE q.id IN (
                SELECT quiz_id FROM assignments WHERE username = %s
                UNION
                SELECT ca.quiz_id FROM class_assignments ca
                JOIN class_members cm ON cm.class_id = ca.class_id
                WHERE cm.username = %s
            )
            ORDER BY q.id DESC
        """, (current_user, current_user))
        quizzes = cur.fetchall()
        classes = []

    conn.close()

    flash_message = request.args.get("flash")
    flash_type = request.args.get("flash_type", "success")

    return render_template_string(
        HOME_HTML,
        quizzes=quizzes,
        classes=classes,
        current_user=current_user,
        is_teacher=is_teacher(),
        flash_message=flash_message,
        flash_type=flash_type,
    )


@app.route("/import", methods=["POST"])
def import_quiz():
    if not is_teacher():
        return redirect(url_for("home"))

    file = request.files["quiz_file"]

    if not file or file.filename == "":
        return "No file selected."

    os.makedirs("uploads", exist_ok=True)
    file_path = os.path.join("uploads", file.filename)
    file.save(file_path)

    quiz_data = parse_quiz_from_docx(file_path)
    title = os.path.splitext(file.filename)[0]
    save_quiz_to_db(title, file.filename, quiz_data)

    return redirect(url_for("home"))


@app.route("/import-students", methods=["POST"])
def import_students():
    if not is_teacher():
        return redirect(url_for("home"))

    file = request.files.get("students_csv")
    if not file or file.filename == "":
        return redirect(url_for("home", flash="No file selected.", flash_type="error"))

    stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
    reader = csv.reader(stream)

    added = 0
    skipped = 0

    conn = get_conn()
    cur = conn.cursor()

    for row in reader:
        if len(row) < 2:
            continue
        username, password = row[0].strip(), row[1].strip()
        # Skip blank lines and the header row
        if not username or username.lower() == "username":
            continue
        cur.execute(
            "INSERT INTO users (username, password, role) VALUES (%s, %s, 'student') ON CONFLICT (username) DO NOTHING",
            (username, password)
        )
        if cur.rowcount == 1:
            added += 1
        else:
            skipped += 1

    conn.commit()
    conn.close()

    parts = [f"{added} student{'s' if added != 1 else ''} added"]
    if skipped:
        parts.append(f"{skipped} skipped (already exist)")
    flash = ", ".join(parts) + "."
    return redirect(url_for("home", flash=flash, flash_type="success"))


@app.route("/quiz/<int:quiz_id>", methods=["GET", "POST"])
def take_quiz(quiz_id):
    if not session.get("username"):
        return redirect(url_for("login"))

    current_user = session["username"]

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT title FROM quizzes WHERE id = %s", (quiz_id,))
    quiz_row = cur.fetchone()

    if not quiz_row:
        conn.close()
        return "Quiz not found."

    cur.execute(
        "SELECT id, question_text, correct_answer FROM questions WHERE quiz_id = %s",
        (quiz_id,)
    )
    question_rows = cur.fetchall()

    questions = []
    for question_id, question_text, correct_answer in question_rows:
        cur.execute("SELECT option_text FROM options WHERE question_id = %s", (question_id,))
        options = [row[0] for row in cur.fetchall()]
        questions.append({
            "id": question_id,
            "text": question_text,
            "options": options,
            "correct_answer": correct_answer
        })

    submitted = False
    score = 0
    total = len(questions)
    user_answers = {}

    # Record start time in session on first GET; clear on a fresh visit
    start_key = f"quiz_start_{quiz_id}"
    if request.method == "GET":
        session[start_key] = datetime.now(timezone.utc).isoformat()

    if request.method == "POST":
        submitted = True

        # Compute duration from session-stored start time
        started_iso = session.pop(start_key, None)
        duration_seconds = None
        if started_iso:
            try:
                started_dt = datetime.fromisoformat(started_iso)
                delta = datetime.now(timezone.utc) - started_dt
                duration_seconds = max(0, int(delta.total_seconds()))
            except (ValueError, TypeError):
                pass

        for q in questions:
            field_name = f"q{q['id']}"
            selected = request.form.get(field_name)
            user_answers[field_name] = selected

            if selected == q["correct_answer"]:
                score += 1

        cur.execute(
            "INSERT INTO results (student_name, quiz_id, score, total, duration_seconds) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (current_user, quiz_id, score, total, duration_seconds)
        )
        result_id = cur.fetchone()[0]

        for q in questions:
            cur.execute(
                "INSERT INTO answers (result_id, question_id, selected_answer) VALUES (%s, %s, %s)",
                (result_id, q["id"], user_answers.get(f"q{q['id']}"))
            )

        conn.commit()

    conn.close()

    return render_template_string(
        QUIZ_HTML,
        quiz_title=quiz_row[0],
        questions=questions,
        submitted=submitted,
        score=score,
        total=total,
        user_answers=user_answers,
        current_user=current_user,
        is_teacher=is_teacher(),
    )


@app.route("/quiz/<int:quiz_id>/assign", methods=["POST"])
def assign_quiz(quiz_id):
    if not is_teacher():
        return redirect(url_for("home"))

    username = request.form.get("username", "").strip()

    if not username:
        return redirect(url_for("home", flash="Username cannot be empty.", flash_type="error"))

    conn = get_conn()
    cur = conn.cursor()

    # Verify quiz exists
    cur.execute("SELECT title FROM quizzes WHERE id = %s", (quiz_id,))
    quiz_row = cur.fetchone()
    if not quiz_row:
        conn.close()
        return redirect(url_for("home", flash="Quiz not found.", flash_type="error"))

    cur.execute(
        "INSERT INTO assignments (quiz_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (quiz_id, username)
    )
    affected = cur.rowcount
    conn.commit()
    conn.close()

    if affected:
        flash = f"Assigned \"{quiz_row[0]}\" to {username}."
        flash_type = "success"
    else:
        flash = f"{username} is already assigned to \"{quiz_row[0]}\"."
        flash_type = "error"

    return redirect(url_for("home", flash=flash, flash_type=flash_type))


@app.route("/class/create", methods=["POST"])
def create_class():
    if not is_teacher():
        return redirect(url_for("home"))

    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("home", flash="Class name cannot be empty.", flash_type="error"))

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO classes (name) VALUES (%s)", (name,))
        conn.commit()
        flash = f'Class "{name}" created.'
        flash_type = "success"
    except psycopg2.IntegrityError:
        conn.rollback()
        flash = f'Class "{name}" already exists.'
        flash_type = "error"
    conn.close()
    return redirect(url_for("home", flash=flash, flash_type=flash_type))


@app.route("/class/<int:class_id>/add-member", methods=["POST"])
def add_class_member(class_id):
    if not is_teacher():
        return redirect(url_for("home"))

    username = request.form.get("username", "").strip()
    if not username:
        return redirect(url_for("home", flash="Username cannot be empty.", flash_type="error"))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM classes WHERE id = %s", (class_id,))
    cls = cur.fetchone()
    if not cls:
        conn.close()
        return redirect(url_for("home", flash="Class not found.", flash_type="error"))

    try:
        cur.execute(
            "INSERT INTO class_members (class_id, username) VALUES (%s, %s)",
            (class_id, username)
        )
        conn.commit()
        flash = f'Added {username} to "{cls[0]}".'
        flash_type = "success"
    except psycopg2.IntegrityError:
        conn.rollback()
        flash = f'{username} is already in "{cls[0]}".'
        flash_type = "error"
    conn.close()
    return redirect(url_for("home", flash=flash, flash_type=flash_type))


@app.route("/quiz/<int:quiz_id>/assign-class", methods=["POST"])
def assign_quiz_to_class(quiz_id):
    if not is_teacher():
        return redirect(url_for("home"))

    class_id = request.form.get("class_id", "").strip()
    if not class_id:
        return redirect(url_for("home", flash="Select a class.", flash_type="error"))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT title FROM quizzes WHERE id = %s", (quiz_id,))
    quiz_row = cur.fetchone()
    cur.execute("SELECT name FROM classes WHERE id = %s", (class_id,))
    cls = cur.fetchone()
    if not quiz_row or not cls:
        conn.close()
        return redirect(url_for("home", flash="Quiz or class not found.", flash_type="error"))

    try:
        cur.execute(
            "INSERT INTO class_assignments (quiz_id, class_id) VALUES (%s, %s)",
            (quiz_id, int(class_id))
        )
        conn.commit()
        flash = f'Assigned "{quiz_row[0]}" to class "{cls[0]}".'
        flash_type = "success"
    except psycopg2.IntegrityError:
        conn.rollback()
        flash = f'Class "{cls[0]}" is already assigned to "{quiz_row[0]}".'
        flash_type = "error"
    conn.close()
    return redirect(url_for("home", flash=flash, flash_type=flash_type))


@app.route("/quiz/<int:quiz_id>/delete", methods=["POST"])
def delete_quiz(quiz_id):
    if not is_teacher():
        return redirect(url_for("home"))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM options WHERE question_id IN (
            SELECT id FROM questions WHERE quiz_id = %s
        )
    """, (quiz_id,))
    cur.execute("""
        DELETE FROM answers WHERE result_id IN (
            SELECT id FROM results WHERE quiz_id = %s
        )
    """, (quiz_id,))
    cur.execute("DELETE FROM questions WHERE quiz_id = %s", (quiz_id,))
    cur.execute("DELETE FROM results WHERE quiz_id = %s", (quiz_id,))
    cur.execute("DELETE FROM assignments WHERE quiz_id = %s", (quiz_id,))
    cur.execute("DELETE FROM class_assignments WHERE quiz_id = %s", (quiz_id,))
    cur.execute("DELETE FROM quizzes WHERE id = %s", (quiz_id,))

    conn.commit()
    conn.close()

    return redirect(url_for("home"))


RESULTS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Results — TestPortal</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        :root {
            --primary: #4f46e5; --primary-hover: #4338ca; --primary-soft: #eef2ff;
            --danger: #dc2626; --danger-soft: #fef2f2;
            --success: #16a34a; --success-soft: #f0fdf4; --success-border: #bbf7d0;
            --amber: #b45309; --amber-soft: #fffbeb; --amber-border: #fef3c7;
            --gray-50: #f9fafb; --gray-100: #f3f4f6; --gray-200: #e5e7eb;
            --gray-300: #d1d5db; --gray-400: #9ca3af; --gray-500: #6b7280;
            --gray-600: #4b5563; --gray-700: #374151; --gray-900: #111827;
            --bg: #f9fafb; --card: #fff; --border: #e5e7eb;
            --text: #111827; --text-sec: #374151; --muted: #6b7280; --dim: #9ca3af;
            --radius: 8px; --radius-lg: 10px;
            --shadow: 0 1px 2px rgba(0,0,0,0.04);
        }
        body { font-family: 'Inter', system-ui, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; -webkit-font-smoothing: antialiased; }

        /* ── Nav ── */
        nav { background: var(--card); border-bottom: 1px solid var(--border); padding: 0 24px; display: flex; align-items: center; height: 52px; gap: 4px; position: sticky; top: 0; z-index: 50; }
        .brand { font-weight: 700; font-size: 15px; color: var(--text); text-decoration: none; margin-right: 20px; }
        .brand b { color: var(--primary); }
        .nav-link { text-decoration: none; color: var(--muted); font-size: 13px; font-weight: 500; padding: 6px 10px; border-radius: var(--radius); transition: color .15s, background .15s; }
        .nav-link:hover { color: var(--text); background: var(--gray-50); }
        .nav-link.active { color: var(--primary); background: var(--primary-soft); }
        .nav-right { margin-left: auto; display: flex; align-items: center; gap: 8px; }
        .nav-user { font-size: 12px; color: var(--muted); }
        .btn-logout { padding: 4px 10px; font-size: 12px; font-weight: 500; background: none; color: var(--muted); border: 1px solid var(--border); border-radius: var(--radius); cursor: pointer; text-decoration: none; transition: color .15s, border-color .15s; }
        .btn-logout:hover { color: var(--text); border-color: var(--gray-300); }

        /* ── Layout ── */
        .page { max-width: 1100px; margin: 0 auto; padding: 32px 24px 80px; }

        .page-header-row {
            display: flex; align-items: flex-end; justify-content: space-between;
            gap: 16px; flex-wrap: wrap; margin-bottom: 20px;
        }
        .page-header-row h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.3px; }
        .page-header-row p { color: var(--muted); font-size: 14px; margin-top: 2px; }

        .btn-export {
            display: inline-flex; align-items: center; gap: 6px;
            padding: 8px 14px; font-size: 13px; font-weight: 600;
            background: none; color: var(--muted);
            border: 1px solid var(--border); border-radius: var(--radius);
            text-decoration: none; white-space: nowrap;
            transition: color .15s, border-color .15s;
        }
        .btn-export:hover { color: var(--text); border-color: var(--gray-300); }

        /* ── Table card ── */
        .table-card {
            background: var(--card); border: 1px solid var(--border);
            border-radius: var(--radius-lg); overflow: hidden;
        }

        table { width: 100%; border-collapse: collapse; }

        thead { background: var(--gray-50); }
        th {
            padding: 10px 16px; text-align: left;
            font-size: 11px; font-weight: 600; text-transform: uppercase;
            letter-spacing: 0.04em; color: var(--dim);
            border-bottom: 1px solid var(--border);
        }
        td {
            padding: 10px 16px; font-size: 13px;
            border-bottom: 1px solid var(--gray-100); color: var(--text);
            vertical-align: middle;
        }
        tbody tr:last-child td { border-bottom: none; }
        tbody tr { transition: background .15s; }
        tbody tr:hover { background: var(--gray-50); }

        /* ── Badges ── */
        .score-cell { font-weight: 600; font-size: 13px; color: var(--text); }
        .id-badge { font-size: 11px; color: var(--dim); font-family: 'SF Mono', monospace; font-weight: 500; }
        .pct-cell { font-weight: 600; font-size: 12px; }
        .pct-cell.pass { color: var(--success); }
        .pct-cell.warn { color: var(--amber); }
        .pct-cell.fail { color: var(--danger); }
        .percentile-cell { font-size: 12px; color: var(--muted); font-weight: 500; }
        .meta-cell { font-size: 12px; color: var(--muted); white-space: nowrap; }
        .student-name { font-weight: 600; font-size: 13px; color: var(--text); }
        .quiz-title { color: var(--gray-600); font-size: 13px; }

        .btn-pdf {
            padding: 4px 10px; font-size: 11px; font-weight: 600;
            background: none; color: var(--primary);
            border: 1px solid var(--border); border-radius: var(--radius);
            text-decoration: none; white-space: nowrap;
            transition: border-color .15s;
        }
        .btn-pdf:hover { border-color: var(--primary); }

        .empty-state {
            padding: 48px 24px; text-align: center; color: var(--dim); font-size: 14px;
        }

        .back-link {
            display: inline-block; margin-top: 24px; font-size: 13px; font-weight: 500;
            color: var(--muted); text-decoration: none; transition: color .15s;
        }
        .back-link:hover { color: var(--primary); }

        @media (max-width: 900px) {
            .page { padding: 20px 16px 60px; }
            td, th { padding: 8px 10px; }
        }
    </style>
</head>
<body>

<nav>
    <a href="/" class="brand">Test<b>Portal</b></a>
    <a href="/" class="nav-link">Home</a>
    <a href="/results" class="nav-link active">Results</a>
    <span class="nav-right">
        <span class="nav-user">{{ current_user }}</span>
        <a href="/logout" class="btn-logout">Log out</a>
    </span>
</nav>

<div class="page">
    <div class="page-header-row">
        <div>
            <h1>Results</h1>
            <p>All student submissions, newest first.</p>
        </div>
        <a href="/export" class="btn-export">Export CSV</a>
    </div>

    <div class="table-card">
        {% if results %}
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Student</th>
                        <th>Quiz</th>
                        <th>Score</th>
                        <th>%</th>
                        <th>Percentile</th>
                        <th>Duration</th>
                        <th>Submitted</th>
                        <th></th>
                    </tr>
                </thead>
                <tbody>
                    {% for row in results %}
                        <tr>
                            <td><span class="id-badge">#{{ row.id }}</span></td>
                            <td><span class="student-name">{{ row.student }}</span></td>
                            <td><span class="quiz-title">{{ row.title }}</span></td>
                            <td><span class="score-cell">{{ row.score }}/{{ row.total }}</span></td>
                            <td><span class="pct-cell {% if row.pct >= 70 %}pass{% elif row.pct >= 50 %}warn{% else %}fail{% endif %}">{{ row.pct }}%</span></td>
                            <td><span class="percentile-cell">{{ row.percentile }}th</span></td>
                            <td><span class="meta-cell">{{ row.duration }}</span></td>
                            <td><span class="meta-cell">{{ row.submitted_at }}</span></td>
                            <td><a href="/result/{{ row.id }}/pdf" class="btn-pdf">PDF</a></td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        {% else %}
            <div class="empty-state">
                No results yet. Submissions will appear here.
            </div>
        {% endif %}
    </div>

    <a href="/" class="back-link">Back to home</a>
</div>

</body>
</html>
"""


@app.route("/results")
def results_dashboard():
    if not is_teacher():
        return redirect(url_for("home"))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id, r.student_name, q.title, r.score, r.total,
               r.submitted_at, r.duration_seconds, r.quiz_id
        FROM results r
        JOIN quizzes q ON r.quiz_id = q.id
        ORDER BY r.id DESC
    """)
    raw = cur.fetchall()

    # Build enriched rows: append percentage, percentile, formatted duration
    rows = []
    for rid, student, title, score, total, submitted_at, duration_seconds, quiz_id in raw:
        pct = round(score / total * 100) if total else 0
        percentile = compute_percentile(score, total, quiz_id, conn)
        rows.append({
            "id":           rid,
            "student":      student,
            "title":        title,
            "score":        score,
            "total":        total,
            "pct":          pct,
            "percentile":   percentile,
            "submitted_at": fmt_dt(submitted_at),
            "duration":     format_duration(duration_seconds),
        })

    conn.close()

    return render_template_string(RESULTS_HTML, results=rows, current_user=session.get("username"), is_teacher=True)


@app.route("/export")
def export_results():
    if not is_teacher():
        return redirect(url_for("home"))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT r.id, r.student_name, q.title, r.score, r.total,
               r.submitted_at, r.duration_seconds, r.quiz_id
        FROM results r
        JOIN quizzes q ON r.quiz_id = q.id
        ORDER BY r.id DESC
    """)
    raw = cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Result ID", "Student Name", "Quiz Title",
        "Score", "Total", "Percentage", "Percentile",
        "Duration", "Submitted At",
    ])
    for rid, student, title, score, total, submitted_at, duration_seconds, quiz_id in raw:
        pct = round(score / total * 100) if total else 0
        percentile = compute_percentile(score, total, quiz_id, conn)
        writer.writerow([
            rid, student, title, score, total,
            f"{pct}%", f"{percentile}th",
            format_duration(duration_seconds),
            fmt_dt(submitted_at),
        ])

    conn.close()

    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=results.csv"},
    )


@app.route("/result/<int:result_id>/pdf")
def export_result_pdf(result_id):
    if not is_teacher():
        return redirect(url_for("home"))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT r.id, r.student_name, q.title, r.score, r.total,
               r.submitted_at, r.duration_seconds, r.quiz_id
        FROM results r
        JOIN quizzes q ON r.quiz_id = q.id
        WHERE r.id = %s
    """, (result_id,))
    result = cur.fetchone()

    if not result:
        conn.close()
        return "Result not found.", 404

    # Drive from questions (via quiz_id) so rows always exist even if
    # the answers table has no entry for this result (old submissions).
    cur.execute("""
        SELECT q.id, q.question_text, q.correct_answer, a.selected_answer
        FROM results r
        JOIN questions q ON q.quiz_id = r.quiz_id
        LEFT JOIN answers a ON a.question_id = q.id AND a.result_id = %s
        WHERE r.id = %s
        ORDER BY q.id ASC
    """, (result_id, result_id))
    answer_rows = cur.fetchall()

    # Fetch all options for each question
    options_by_question = {}
    for (qid, _, _, _) in answer_rows:
        cur.execute("SELECT option_text FROM options WHERE question_id = %s ORDER BY id ASC", (qid,))
        options_by_question[qid] = [r[0] for r in cur.fetchall()]

    res_id, student_name, quiz_title, score, total, submitted_at, duration_seconds, quiz_id = result
    percentile = compute_percentile(score, total, quiz_id, conn)
    conn.close()

    percentage = round(score / total * 100) if total else 0

    # ── Colour palette (minimal – print-friendly) ────────────
    BLACK      = colors.HexColor("#111111")
    GRAY_DARK  = colors.HexColor("#374151")
    GRAY_MID   = colors.HexColor("#6b7280")
    GRAY_LIGHT = colors.HexColor("#d1d5db")
    GRAY_BG    = colors.HexColor("#f9fafb")
    GREEN      = colors.HexColor("#16a34a")
    GREEN_BG   = colors.HexColor("#f0fdf4")
    RED        = colors.HexColor("#dc2626")
    RED_BG     = colors.HexColor("#fef2f2")
    WHITE      = colors.white

    # ── Page geometry ────────────────────────────────────────
    W = 17 * cm   # usable width  (A4 = 21 cm – 2 × 2 cm margins)
    INDENT = 0.7 * cm

    base = getSampleStyleSheet()
    def sty(name, **kw):
        return ParagraphStyle(name, parent=base["Normal"], **kw)

    # Shared text styles
    quiz_title_sty = sty("qt", fontSize=16, fontName="Helvetica-Bold",
                          textColor=BLACK, leading=20, spaceAfter=2)
    meta_sty       = sty("mt", fontSize=9, textColor=GRAY_MID, leading=13)
    score_sty      = sty("sc", fontSize=11, fontName="Helvetica-Bold",
                          textColor=BLACK, alignment=TA_CENTER)
    score_sub_sty  = sty("scs", fontSize=8, textColor=GRAY_MID, alignment=TA_CENTER)

    q_num_sty      = sty("qn", fontSize=10, fontName="Helvetica-Bold",
                          textColor=GRAY_MID, leading=14)
    q_text_sty     = sty("qx", fontSize=11, fontName="Helvetica-Bold",
                          textColor=BLACK, leading=15)
    verdict_ok_sty = sty("vok",  fontSize=9, fontName="Helvetica-Bold",
                          textColor=GREEN, alignment=TA_CENTER)
    verdict_bad_sty= sty("vbad", fontSize=9, fontName="Helvetica-Bold",
                          textColor=RED, alignment=TA_CENTER)

    opt_plain_sty  = sty("op",  fontSize=10, textColor=GRAY_DARK, leading=14)
    opt_right_sty  = sty("orc", fontSize=10, fontName="Helvetica-Bold",
                          textColor=GREEN, leading=14)
    opt_wrong_sty  = sty("owg", fontSize=10, fontName="Helvetica-Bold",
                          textColor=RED,   leading=14)
    annot_sty      = sty("an",  fontSize=8,  textColor=GRAY_MID,
                          alignment=TA_CENTER, leading=11)
    marker_sty     = sty("mk",  fontSize=13, leading=14)

    # ── Document ─────────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=2*cm,  bottomMargin=2*cm,
    )
    story = []

    # ════════════════════════════════════════════════════════
    # HEADER  – looks like the top of an exam paper
    # ════════════════════════════════════════════════════════
    pct_color = "#16a34a" if percentage >= 50 else "#dc2626"

    # Left column: quiz title + meta info
    # Right column: score box
    meta_lines = quiz_title
    submitted_str = fmt_dt(submitted_at)
    if submitted_str != "—":
        meta_lines += f"<br/>Submitted: {submitted_str}"
    duration_str = format_duration(duration_seconds)
    meta_lines += f"<br/>Duration: {duration_str}  ·  Percentile: {percentile}th"

    header_left = [
        [Paragraph(student_name, quiz_title_sty)],
        [Paragraph(meta_lines,   meta_sty)],
        [Paragraph(f"Result ID: #{res_id}", meta_sty)],
    ]
    left_tbl = Table(header_left, colWidths=[12*cm])
    left_tbl.setStyle(TableStyle([
        ("LEFTPADDING",  (0,0),(-1,-1), 0),
        ("RIGHTPADDING", (0,0),(-1,-1), 0),
        ("TOPPADDING",   (0,0),(-1,-1), 1),
        ("BOTTOMPADDING",(0,0),(-1,-1), 1),
        ("VALIGN",       (0,0),(-1,-1), "TOP"),
    ]))

    score_tbl = Table(
        [
            [Paragraph(f"{score}/{total}", score_sty)],
            [Paragraph(f"<font color='{pct_color}'><b>{percentage}%</b></font>", score_sty)],
            [Paragraph("score", score_sub_sty)],
        ],
        colWidths=[4*cm],
    )
    score_tbl.setStyle(TableStyle([
        ("BOX",          (0,0),(-1,-1), 1.5, GRAY_LIGHT),
        ("BACKGROUND",   (0,0),(-1,-1), GRAY_BG),
        ("TOPPADDING",   (0,0),(-1,-1), 8),
        ("BOTTOMPADDING",(0,0),(-1,-1), 8),
        ("LEFTPADDING",  (0,0),(-1,-1), 6),
        ("RIGHTPADDING", (0,0),(-1,-1), 6),
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
    ]))

    header_row = Table([[left_tbl, score_tbl]], colWidths=[12.5*cm, 4.5*cm])
    header_row.setStyle(TableStyle([
        ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING",  (0,0),(-1,-1), 0),
        ("RIGHTPADDING", (0,0),(-1,-1), 0),
        ("TOPPADDING",   (0,0),(-1,-1), 0),
        ("BOTTOMPADDING",(0,0),(-1,-1), 0),
    ]))

    story.append(header_row)
    story.append(Spacer(1, 0.3*cm))
    story.append(HRFlowable(width="100%", thickness=1.5, color=BLACK, spaceAfter=0.5*cm))

    # ════════════════════════════════════════════════════════
    # QUESTIONS  – one block per question, exam-paper style
    # ════════════════════════════════════════════════════════
    for i, (qid, question_text, correct_answer, selected_answer) in enumerate(answer_rows, start=1):
        is_correct = (selected_answer == correct_answer)
        verdict_text = "✓  Correct"   if is_correct else "✗  Incorrect"
        verdict_sty  = verdict_ok_sty if is_correct else verdict_bad_sty

        # ── Question stem row ────────────────────────────────
        # Columns: [num | text | verdict]
        q_stem = Table(
            [[
                Paragraph(f"{i}.", q_num_sty),
                Paragraph(question_text, q_text_sty),
                Paragraph(verdict_text, verdict_sty),
            ]],
            colWidths=[0.7*cm, 13.5*cm, 2.8*cm],
        )
        q_stem.setStyle(TableStyle([
            ("VALIGN",       (0,0),(-1,-1), "TOP"),
            ("LEFTPADDING",  (0,0),(-1,-1), 0),
            ("RIGHTPADDING", (0,0),(-1,-1), 0),
            ("TOPPADDING",   (0,0),(-1,-1), 0),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ]))
        story.append(q_stem)

        # ── Option rows ──────────────────────────────────────
        for opt_text in options_by_question.get(qid, []):
            is_selected = (opt_text == selected_answer)
            is_answer   = (opt_text == correct_answer)

            if is_selected and is_correct:
                marker   = Paragraph("<font color='#16a34a'><b>●</b></font>", marker_sty)
                opt_para = Paragraph(opt_text, opt_right_sty)
                annot    = Paragraph("your answer  ✓", annot_sty)
                row_bg   = GREEN_BG

            elif is_selected and not is_correct:
                marker   = Paragraph("<font color='#dc2626'><b>●</b></font>", marker_sty)
                opt_para = Paragraph(f"<strike>{opt_text}</strike>", opt_wrong_sty)
                annot    = Paragraph("your answer  ✗", annot_sty)
                row_bg   = RED_BG

            elif is_answer and not is_correct:
                marker   = Paragraph("<font color='#16a34a'><b>✓</b></font>", marker_sty)
                opt_para = Paragraph(opt_text, opt_right_sty)
                annot    = Paragraph("correct answer", annot_sty)
                row_bg   = GREEN_BG

            else:
                marker   = Paragraph("<font color='#9ca3af'>○</font>", marker_sty)
                opt_para = Paragraph(opt_text, opt_plain_sty)
                annot    = Paragraph("", annot_sty)
                row_bg   = WHITE

            opt_row = Table(
                [[marker, opt_para, annot]],
                colWidths=[INDENT, W - INDENT - 2.5*cm, 2.5*cm],
            )
            opt_row.setStyle(TableStyle([
                ("BACKGROUND",   (0,0),(-1,-1), row_bg),
                ("VALIGN",       (0,0),(-1,-1), "MIDDLE"),
                ("LEFTPADDING",  (0,0),(0,0),   4),
                ("LEFTPADDING",  (1,0),(1,0),   4),
                ("RIGHTPADDING", (0,0),(-1,-1), 6),
                ("TOPPADDING",   (0,0),(-1,-1), 4),
                ("BOTTOMPADDING",(0,0),(-1,-1), 4),
            ]))
            story.append(opt_row)

        # ── Divider between questions ────────────────────────
        story.append(Spacer(1, 0.15*cm))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=GRAY_LIGHT, spaceAfter=0.25*cm))

    doc.build(story)
    buf.seek(0)

    return Response(
        buf.read(),
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=result_{result_id}.pdf"},
    )


init_db()

if __name__ == "__main__":
    app.run(debug=True)