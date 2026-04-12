import csv
import io
import os
import sqlite3
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
DB_NAME = "portal.db"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS quizzes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        filename TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        question_text TEXT NOT NULL,
        correct_answer TEXT NOT NULL,
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_id INTEGER NOT NULL,
        option_text TEXT NOT NULL,
        FOREIGN KEY (question_id) REFERENCES questions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        student_name TEXT NOT NULL,
        quiz_id INTEGER NOT NULL,
        score INTEGER NOT NULL,
        total INTEGER NOT NULL,
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        UNIQUE(quiz_id, username),
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        result_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        selected_answer TEXT,
        FOREIGN KEY (result_id) REFERENCES results(id),
        FOREIGN KEY (question_id) REFERENCES questions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS classes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS class_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        class_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        UNIQUE(class_id, username),
        FOREIGN KEY (class_id) REFERENCES classes(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS class_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        quiz_id INTEGER NOT NULL,
        class_id INTEGER NOT NULL,
        UNIQUE(quiz_id, class_id),
        FOREIGN KEY (quiz_id) REFERENCES quizzes(id),
        FOREIGN KEY (class_id) REFERENCES classes(id)
    )
    """)

    # Add submitted_at to existing results tables that predate this column
    try:
        cur.execute("ALTER TABLE results ADD COLUMN submitted_at TEXT DEFAULT (datetime('now'))")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Add role to existing users tables that predate this column
    try:
        cur.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'student'")
    except sqlite3.OperationalError:
        pass  # column already exists

    # Add duration_seconds to track quiz completion time
    try:
        cur.execute("ALTER TABLE results ADD COLUMN duration_seconds INTEGER")
    except sqlite3.OperationalError:
        pass  # column already exists

    conn.commit()
    conn.close()


def clean_option_text(text):
    return text.replace("✓", "").strip()


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
    cur.execute("SELECT score, total FROM results WHERE quiz_id = ?", (quiz_id,))
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
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("INSERT INTO quizzes (title, filename) VALUES (?, ?)", (title, filename))
    quiz_id = cur.lastrowid

    for q in quiz_data:
        cur.execute(
            "INSERT INTO questions (quiz_id, question_text, correct_answer) VALUES (?, ?, ?)",
            (quiz_id, q["question"], q["answer"])
        )
        question_id = cur.lastrowid

        for option in q["options"]:
            cur.execute(
                "INSERT INTO options (question_id, option_text) VALUES (?, ?)",
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
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --primary: #4f46e5;
            --primary-hover: #4338ca;
            --danger: #dc2626;
            --danger-hover: #b91c1c;
            --success: #16a34a;
            --bg: #f1f5f9;
            --card: #ffffff;
            --border: #e2e8f0;
            --text: #1e293b;
            --muted: #64748b;
            --radius: 10px;
            --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.06);
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
        }

        /* ── Navbar ── */
        nav {
            background: var(--card);
            border-bottom: 1px solid var(--border);
            padding: 0 24px;
            display: flex;
            align-items: center;
            height: 56px;
            gap: 32px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        }
        nav .brand {
            font-weight: 700;
            font-size: 17px;
            color: var(--primary);
            text-decoration: none;
            letter-spacing: -0.3px;
        }
        nav a {
            text-decoration: none;
            color: var(--muted);
            font-size: 14px;
            font-weight: 500;
            padding: 6px 2px;
            border-bottom: 2px solid transparent;
            transition: color 0.15s, border-color 0.15s;
        }
        nav a:hover { color: var(--primary); }
        nav a.active { color: var(--primary); border-bottom-color: var(--primary); }
        nav .nav-right { margin-left: auto; }
        .btn-nav {
            padding: 6px 14px;
            font-size: 13px;
            font-weight: 500;
            background: var(--primary);
            color: #fff !important;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            text-decoration: none;
            border-bottom: none !important;
        }
        .btn-nav:hover { background: var(--primary-hover); color: #fff !important; }

        /* ── Layout ── */
        .page { max-width: 780px; margin: 40px auto; padding: 0 20px 60px; }

        .page-header { margin-bottom: 28px; }
        .page-header h1 { font-size: 24px; font-weight: 700; color: var(--text); }
        .page-header p  { color: var(--muted); font-size: 14px; margin-top: 4px; }

        /* ── Flash messages ── */
        .flash {
            padding: 11px 16px;
            border-radius: 8px;
            font-size: 13px;
            font-weight: 500;
            margin-bottom: 20px;
        }
        .flash.success {
            background: #f0fdf4;
            border: 1px solid #86efac;
            color: var(--success);
        }
        .flash.error {
            background: #fef2f2;
            border: 1px solid #fca5a5;
            color: var(--danger);
        }

        /* ── Cards ── */
        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            padding: 24px 28px;
            margin-bottom: 20px;
        }
        .card-title {
            font-size: 14px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--muted);
            margin-bottom: 16px;
        }

        /* ── Buttons ── */
        .btn {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 9px 18px;
            font-size: 14px;
            font-weight: 500;
            border: none;
            border-radius: 7px;
            cursor: pointer;
            text-decoration: none;
            transition: background 0.15s, transform 0.1s;
        }
        .btn:active { transform: scale(0.98); }
        .btn-primary { background: var(--primary); color: #fff; }
        .btn-primary:hover { background: var(--primary-hover); }
        .btn-danger  { background: var(--danger);  color: #fff; padding: 6px 13px; font-size: 13px; }
        .btn-danger:hover  { background: var(--danger-hover); }
        .btn-assign {
            background: #ede9fe;
            color: var(--primary);
            padding: 6px 13px;
            font-size: 13px;
            font-weight: 600;
            border: none;
            border-radius: 7px;
            cursor: pointer;
            transition: background 0.15s;
        }
        .btn-assign:hover { background: #ddd6fe; }

        /* ── File input ── */
        .file-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
        input[type="file"] {
            font-size: 13px;
            color: var(--muted);
            border: 1px solid var(--border);
            border-radius: 7px;
            padding: 7px 12px;
            background: var(--bg);
            cursor: pointer;
        }
        input[type="file"]::-webkit-file-upload-button {
            background: var(--bg);
            border: none;
            color: var(--muted);
            cursor: pointer;
        }

        /* ── Quiz list ── */
        .quiz-list { list-style: none; }
        .quiz-item {
            border: 1px solid var(--border);
            border-radius: 8px;
            margin-bottom: 8px;
            background: var(--bg);
            transition: border-color 0.15s, box-shadow 0.15s;
            overflow: hidden;
        }
        .quiz-item:hover { border-color: #c7d2fe; }
        .quiz-item-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 13px 16px;
        }
        .quiz-item-actions { display: flex; align-items: center; gap: 8px; }
        .quiz-item a.quiz-link {
            text-decoration: none;
            font-weight: 500;
            font-size: 15px;
            color: var(--primary);
        }
        .quiz-item a.quiz-link:hover { text-decoration: underline; }

        /* ── Assign panel (CSS checkbox toggle, no JS) ── */
        .assign-toggle { display: none; }
        .assign-panel {
            display: none;
            padding: 12px 16px;
            border-top: 1px solid var(--border);
            background: var(--card);
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }
        .assign-toggle:checked ~ .assign-panel { display: flex; }
        .assign-panel label {
            font-size: 13px;
            font-weight: 600;
            color: var(--muted);
            white-space: nowrap;
        }
        .assign-panel input[type="text"] {
            padding: 7px 11px;
            font-size: 13px;
            border: 1px solid var(--border);
            border-radius: 7px;
            outline: none;
            transition: border-color 0.15s, box-shadow 0.15s;
            width: 180px;
        }
        .assign-panel input[type="text"]:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79,70,229,0.12);
        }
        .btn-do-assign {
            padding: 7px 14px;
            font-size: 13px;
            font-weight: 600;
            background: var(--primary);
            color: #fff;
            border: none;
            border-radius: 7px;
            cursor: pointer;
            transition: background 0.15s;
        }
        .btn-do-assign:hover { background: var(--primary-hover); }

        .empty { color: var(--muted); font-size: 14px; }

        /* ── Class-assign panel on quiz items (2nd toggle) ── */
        .class-toggle { display: none; }
        .class-panel {
            display: none;
            padding: 12px 16px;
            border-top: 1px solid var(--border);
            background: #fafaf9;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }
        .class-toggle:checked ~ .class-panel { display: flex; }
        .class-panel label {
            font-size: 13px;
            font-weight: 600;
            color: var(--muted);
            white-space: nowrap;
        }
        .class-panel select {
            padding: 7px 11px;
            font-size: 13px;
            border: 1px solid var(--border);
            border-radius: 7px;
            outline: none;
            background: #fff;
            cursor: pointer;
            transition: border-color 0.15s, box-shadow 0.15s;
        }
        .class-panel select:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79,70,229,0.12);
        }

        /* ── Class list ── */
        .cls-toggle { display: none; }
        .cls-panel {
            display: none;
            padding: 10px 14px;
            border-top: 1px solid var(--border);
            background: var(--card);
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }
        .cls-toggle:checked ~ .cls-panel { display: flex; }
        .cls-panel label {
            font-size: 13px;
            font-weight: 600;
            color: var(--muted);
            white-space: nowrap;
        }
        .cls-panel input[type="text"] {
            padding: 7px 11px;
            font-size: 13px;
            border: 1px solid var(--border);
            border-radius: 7px;
            outline: none;
            width: 180px;
            transition: border-color 0.15s, box-shadow 0.15s;
        }
        .cls-panel input[type="text"]:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79,70,229,0.12);
        }
        .class-list { list-style: none; }
        .class-item {
            border: 1px solid var(--border);
            border-radius: 8px;
            margin-bottom: 8px;
            background: var(--bg);
            overflow: hidden;
        }
        .class-item-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 11px 14px;
        }
        .class-item-name { font-weight: 500; font-size: 14px; }
        .member-count {
            display: inline-block;
            font-size: 12px;
            color: var(--muted);
            background: #f1f5f9;
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 1px 9px;
            margin-left: 8px;
        }
        .create-class-row {
            display: flex;
            gap: 10px;
            align-items: center;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }
        .create-class-row input[type="text"] {
            flex: 1;
            min-width: 160px;
            padding: 8px 12px;
            font-size: 13px;
            border: 1px solid var(--border);
            border-radius: 7px;
            outline: none;
            transition: border-color 0.15s, box-shadow 0.15s;
        }
        .create-class-row input[type="text"]:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79,70,229,0.12);
        }
    </style>
</head>
<body>

<nav>
    <a href="/" class="brand">TestPortal</a>
    <a href="/" class="active">Home</a>
    {% if is_teacher %}<a href="/results">Results</a>{% endif %}
    <span class="nav-right">
        {% if current_user %}
            <a href="/logout" class="btn-nav">Logout ({{ current_user }})</a>
        {% else %}
            <a href="/login" class="btn-nav">Login</a>
        {% endif %}
    </span>
</nav>

<div class="page">

    {% if flash_message %}
        <div class="flash {{ flash_type }}">{{ flash_message }}</div>
    {% endif %}

    {% if not is_teacher %}
        {# ── Student view ── #}
        <div class="page-header">
            <h1>Your Quizzes</h1>
            <p>Quizzes assigned to you, {{ current_user }}.</p>
        </div>

        <div class="card">
            {% if quizzes %}
                <ul class="quiz-list">
                    {% for quiz in quizzes %}
                        <li class="quiz-item">
                            <div class="quiz-item-row">
                                <a href="/quiz/{{ quiz[0] }}" class="quiz-link">{{ quiz[1] }}</a>
                            </div>
                        </li>
                    {% endfor %}
                </ul>
            {% else %}
                <p class="empty">No quizzes have been assigned to you yet.</p>
            {% endif %}
        </div>

    {% else %}
        {# ── Teacher view ── #}

        <div class="page-header">
            <h1>Quiz Dashboard</h1>
            <p>Import Word documents, assign quizzes, and manage your portal.</p>
        </div>

        <div class="card">
            <div class="card-title">Import a Quiz</div>
            <form method="POST" enctype="multipart/form-data" action="/import">
                <div class="file-row">
                    <input type="file" name="quiz_file" accept=".docx" required>
                    <button type="submit" class="btn btn-primary">&#8679; Import Quiz</button>
                </div>
            </form>
        </div>

        <div class="card">
            <div class="card-title">Import Students</div>
            <form method="POST" enctype="multipart/form-data" action="/import-students">
                <div class="file-row">
                    <input type="file" name="students_csv" accept=".csv,.txt" required>
                    <button type="submit" class="btn btn-primary">&#8679; Import Students</button>
                </div>
            </form>
            <p style="margin-top:10px;font-size:12px;color:var(--muted);">
                CSV format: <code>username,password</code> — one student per line, no header required (header is skipped automatically).
            </p>
        </div>

        <div class="card">
            <div class="card-title">Classes</div>
            <form method="POST" action="/class/create" class="create-class-row">
                <input type="text" name="name" placeholder="New class name" required>
                <button type="submit" class="btn btn-primary" style="white-space:nowrap;">+ Create Class</button>
            </form>
            {% if classes %}
                <ul class="class-list">
                    {% for cls in classes %}
                        <li class="class-item">
                            <input type="checkbox" id="cls-{{ cls[0] }}" class="cls-toggle">
                            <div class="class-item-row">
                                <span class="class-item-name">
                                    {{ cls[1] }}
                                    <span class="member-count">{{ cls[2] }} member{{ 's' if cls[2] != 1 else '' }}</span>
                                </span>
                                <label for="cls-{{ cls[0] }}" class="btn-assign">Add Member</label>
                            </div>
                            <div class="cls-panel">
                                <form method="POST" action="/class/{{ cls[0] }}/add-member"
                                      style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                                    <label>Username:</label>
                                    <input type="text" name="username" placeholder="e.g. alice" required>
                                    <button type="submit" class="btn-do-assign">Add</button>
                                </form>
                            </div>
                        </li>
                    {% endfor %}
                </ul>
            {% else %}
                <p class="empty">No classes yet. Create one above.</p>
            {% endif %}
        </div>

        <div class="card">
            <div class="card-title">All Quizzes</div>
            {% if quizzes %}
                <ul class="quiz-list">
                    {% for quiz in quizzes %}
                        <li class="quiz-item">
                            <input type="checkbox" id="assign-{{ quiz[0] }}" class="assign-toggle">
                            <input type="checkbox" id="class-assign-{{ quiz[0] }}" class="class-toggle">
                            <div class="quiz-item-row">
                                <span style="font-weight:500;font-size:15px;">{{ quiz[1] }}</span>
                                <div class="quiz-item-actions">
                                    <label for="assign-{{ quiz[0] }}" class="btn-assign">Assign User</label>
                                    {% if classes %}
                                        <label for="class-assign-{{ quiz[0] }}" class="btn-assign">Assign Class</label>
                                    {% endif %}
                                    <form method="POST" action="/quiz/{{ quiz[0] }}/delete"
                                          onsubmit="return confirm('Delete &quot;{{ quiz[1] }}&quot; and all its data? This cannot be undone.');">
                                        <button type="submit" class="btn btn-danger">Delete</button>
                                    </form>
                                </div>
                            </div>
                            <div class="assign-panel">
                                <form method="POST" action="/quiz/{{ quiz[0] }}/assign"
                                      style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                                    <label>Assign to username:</label>
                                    <input type="text" name="username" placeholder="e.g. alice" required>
                                    <button type="submit" class="btn-do-assign">Assign</button>
                                </form>
                            </div>
                            {% if classes %}
                                <div class="class-panel">
                                    <form method="POST" action="/quiz/{{ quiz[0] }}/assign-class"
                                          style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
                                        <label>Assign to class:</label>
                                        <select name="class_id" required>
                                            {% for cls in classes %}
                                                <option value="{{ cls[0] }}">{{ cls[1] }}</option>
                                            {% endfor %}
                                        </select>
                                        <button type="submit" class="btn-do-assign">Assign</button>
                                    </form>
                                </div>
                            {% endif %}
                        </li>
                    {% endfor %}
                </ul>
            {% else %}
                <p class="empty">No quizzes imported yet. Upload a .docx file above to get started.</p>
            {% endif %}
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
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --primary: #4f46e5;
            --primary-hover: #4338ca;
            --bg: #f1f5f9;
            --card: #ffffff;
            --border: #e2e8f0;
            --text: #1e293b;
            --muted: #64748b;
            --correct-bg: #f0fdf4;
            --correct-border: #86efac;
            --correct-text: #16a34a;
            --incorrect-bg: #fef2f2;
            --incorrect-border: #fca5a5;
            --incorrect-text: #dc2626;
            --radius: 10px;
            --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.06);
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
        }

        nav {
            background: var(--card);
            border-bottom: 1px solid var(--border);
            padding: 0 24px;
            display: flex;
            align-items: center;
            height: 56px;
            gap: 32px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        }
        nav .brand {
            font-weight: 700;
            font-size: 17px;
            color: var(--primary);
            text-decoration: none;
            letter-spacing: -0.3px;
        }
        nav a {
            text-decoration: none;
            color: var(--muted);
            font-size: 14px;
            font-weight: 500;
            padding: 6px 2px;
            border-bottom: 2px solid transparent;
            transition: color 0.15s, border-color 0.15s;
        }
        nav a:hover { color: var(--primary); }
        nav .nav-right { margin-left: auto; }
        .btn-nav {
            padding: 6px 14px;
            font-size: 13px;
            font-weight: 500;
            background: var(--primary);
            color: #fff !important;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            text-decoration: none;
            border-bottom: none !important;
        }
        .btn-nav:hover { background: var(--primary-hover); color: #fff !important; }

        .page { max-width: 720px; margin: 40px auto; padding: 0 20px 60px; }

        .page-header { margin-bottom: 24px; }
        .page-header h1 { font-size: 22px; font-weight: 700; }
        .page-header .taking-as {
            display: inline-block;
            margin-top: 6px;
            font-size: 13px;
            color: var(--muted);
            background: #f1f5f9;
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 2px 12px;
        }

        /* Question cards */
        .question-card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            padding: 22px 24px;
            margin-bottom: 16px;
        }
        .question-num {
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            color: var(--muted);
            margin-bottom: 6px;
        }
        .question-text {
            font-size: 16px;
            font-weight: 600;
            margin-bottom: 16px;
        }

        /* Options */
        .option-label {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px 14px;
            border: 1px solid var(--border);
            border-radius: 7px;
            margin-bottom: 8px;
            cursor: pointer;
            font-size: 14px;
            transition: border-color 0.15s, background 0.15s;
        }
        .option-label:hover { border-color: var(--primary); background: #f5f3ff; }
        .option-label input[type="radio"] { accent-color: var(--primary); width: 16px; height: 16px; flex-shrink: 0; }

        /* Submitted option states */
        .option-label.option-correct {
            background: var(--correct-bg);
            border-color: var(--correct-border);
            color: var(--correct-text);
            font-weight: 600;
            cursor: default;
        }
        .option-label.option-correct input[type="radio"] { accent-color: var(--correct-text); }
        .option-label.option-incorrect {
            background: var(--incorrect-bg);
            border-color: var(--incorrect-border);
            color: var(--incorrect-text);
            font-weight: 600;
            cursor: default;
        }
        .option-label.option-incorrect input[type="radio"] { accent-color: var(--incorrect-text); }
        .option-label.option-disabled {
            cursor: default;
            color: var(--muted);
        }
        .option-label.option-disabled:hover { border-color: var(--border); background: transparent; }

        /* Inline answer badge */
        .option-badge {
            margin-left: auto;
            font-size: 12px;
            font-weight: 700;
            padding: 2px 9px;
            border-radius: 999px;
            white-space: nowrap;
        }
        .badge-correct { background: #dcfce7; color: var(--correct-text); }
        .badge-incorrect { background: #fee2e2; color: var(--incorrect-text); }
        .badge-answer { background: #dcfce7; color: var(--correct-text); }

        /* Score banner */
        .score-banner {
            background: var(--primary);
            color: #fff;
            border-radius: var(--radius);
            padding: 20px 28px;
            margin-bottom: 24px;
            display: flex;
            align-items: center;
            gap: 16px;
        }
        .score-banner .score-value { font-size: 28px; font-weight: 800; }
        .score-banner .score-label { font-size: 14px; opacity: 0.85; }

        /* Button */
        .btn {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 10px 22px;
            font-size: 14px;
            font-weight: 600;
            border: none;
            border-radius: 7px;
            cursor: pointer;
            text-decoration: none;
            transition: background 0.15s, transform 0.1s;
        }
        .btn:active { transform: scale(0.98); }
        .btn-primary { background: var(--primary); color: #fff; }
        .btn-primary:hover { background: var(--primary-hover); }

        .back-link {
            display: inline-block;
            margin-top: 24px;
            font-size: 14px;
            color: var(--muted);
            text-decoration: none;
        }
        .back-link:hover { color: var(--primary); }

        .submit-row { margin-top: 24px; }
    </style>
</head>
<body>

<nav>
    <a href="/" class="brand">TestPortal</a>
    <a href="/">Home</a>
    {% if is_teacher %}<a href="/results">Results</a>{% endif %}
    <span class="nav-right">
        {% if current_user %}
            <a href="/logout" class="btn-nav">Logout ({{ current_user }})</a>
        {% else %}
            <a href="/login" class="btn-nav">Login</a>
        {% endif %}
    </span>
</nav>

<div class="page">
    <div class="page-header">
        <h1>{{ quiz_title }}</h1>
        <span class="taking-as">Taking as: {{ current_user }}</span>
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

        <div class="submit-row">
            <button type="submit" class="btn btn-primary">Submit Quiz</button>
        </div>
    </form>

    <a href="/" class="back-link">&#8592; Back to homepage</a>
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
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --primary: #4f46e5;
            --primary-hover: #4338ca;
            --bg: #f1f5f9;
            --card: #ffffff;
            --border: #e2e8f0;
            --text: #1e293b;
            --muted: #64748b;
            --radius: 10px;
            --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.06);
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .login-card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            padding: 40px 40px 36px;
            width: 100%;
            max-width: 380px;
        }

        .login-brand {
            font-size: 20px;
            font-weight: 700;
            color: var(--primary);
            letter-spacing: -0.3px;
            margin-bottom: 6px;
        }
        .login-subtitle {
            font-size: 13px;
            color: var(--muted);
            margin-bottom: 28px;
        }

        .field { margin-bottom: 16px; }
        .field label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: var(--muted);
            margin-bottom: 6px;
        }
        .field input {
            width: 100%;
            padding: 9px 13px;
            font-size: 14px;
            border: 1px solid var(--border);
            border-radius: 7px;
            outline: none;
            transition: border-color 0.15s, box-shadow 0.15s;
        }
        .field input:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 3px rgba(79,70,229,0.12);
        }

        .btn-login {
            width: 100%;
            padding: 10px;
            margin-top: 8px;
            font-size: 14px;
            font-weight: 600;
            background: var(--primary);
            color: #fff;
            border: none;
            border-radius: 7px;
            cursor: pointer;
            transition: background 0.15s, transform 0.1s;
        }
        .btn-login:hover { background: var(--primary-hover); }
        .btn-login:active { transform: scale(0.98); }

        .error {
            background: #fef2f2;
            border: 1px solid #fca5a5;
            color: #dc2626;
            font-size: 13px;
            padding: 10px 13px;
            border-radius: 7px;
            margin-bottom: 16px;
        }
    </style>
</head>
<body>

<div class="login-card">
    <div class="login-brand">TestPortal</div>
    <div class="login-subtitle">Sign in to take quizzes</div>

    {% if error %}
        <div class="error">{{ error }}</div>
    {% endif %}

    <form method="POST">
        <div class="field">
            <label for="username">Username</label>
            <input type="text" id="username" name="username"
                   value="{{ username or '' }}" autocomplete="username" required autofocus>
        </div>
        <div class="field">
            <label for="password">Password</label>
            <input type="password" id="password" name="password"
                   autocomplete="current-password" required>
        </div>
        <button type="submit" class="btn-login">Sign in</button>
    </form>
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

        conn = sqlite3.connect(DB_NAME)
        cur = conn.cursor()
        cur.execute("SELECT id, role FROM users WHERE username = ? AND password = ?", (username, password))
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
    conn = sqlite3.connect(DB_NAME)
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
                SELECT quiz_id FROM assignments WHERE username = ?
                UNION
                SELECT ca.quiz_id FROM class_assignments ca
                JOIN class_members cm ON cm.class_id = ca.class_id
                WHERE cm.username = ?
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

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    for row in reader:
        if len(row) < 2:
            continue
        username, password = row[0].strip(), row[1].strip()
        # Skip blank lines and the header row
        if not username or username.lower() == "username":
            continue
        try:
            cur.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, 'student')",
                (username, password)
            )
            added += 1
        except sqlite3.IntegrityError:
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

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("SELECT title FROM quizzes WHERE id = ?", (quiz_id,))
    quiz_row = cur.fetchone()

    if not quiz_row:
        conn.close()
        return "Quiz not found."

    cur.execute(
        "SELECT id, question_text, correct_answer FROM questions WHERE quiz_id = ?",
        (quiz_id,)
    )
    question_rows = cur.fetchall()

    questions = []
    for question_id, question_text, correct_answer in question_rows:
        cur.execute("SELECT option_text FROM options WHERE question_id = ?", (question_id,))
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
            "INSERT INTO results (student_name, quiz_id, score, total, duration_seconds) VALUES (?, ?, ?, ?, ?)",
            (current_user, quiz_id, score, total, duration_seconds)
        )
        result_id = cur.lastrowid

        for q in questions:
            cur.execute(
                "INSERT INTO answers (result_id, question_id, selected_answer) VALUES (?, ?, ?)",
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

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    # Verify quiz exists
    cur.execute("SELECT title FROM quizzes WHERE id = ?", (quiz_id,))
    quiz_row = cur.fetchone()
    if not quiz_row:
        conn.close()
        return redirect(url_for("home", flash="Quiz not found.", flash_type="error"))

    # INSERT OR IGNORE handles the duplicate constraint silently
    cur.execute(
        "INSERT OR IGNORE INTO assignments (quiz_id, username) VALUES (?, ?)",
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

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO classes (name) VALUES (?)", (name,))
        conn.commit()
        flash = f'Class "{name}" created.'
        flash_type = "success"
    except sqlite3.IntegrityError:
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

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT name FROM classes WHERE id = ?", (class_id,))
    cls = cur.fetchone()
    if not cls:
        conn.close()
        return redirect(url_for("home", flash="Class not found.", flash_type="error"))

    try:
        cur.execute(
            "INSERT INTO class_members (class_id, username) VALUES (?, ?)",
            (class_id, username)
        )
        conn.commit()
        flash = f'Added {username} to "{cls[0]}".'
        flash_type = "success"
    except sqlite3.IntegrityError:
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

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("SELECT title FROM quizzes WHERE id = ?", (quiz_id,))
    quiz_row = cur.fetchone()
    cur.execute("SELECT name FROM classes WHERE id = ?", (class_id,))
    cls = cur.fetchone()
    if not quiz_row or not cls:
        conn.close()
        return redirect(url_for("home", flash="Quiz or class not found.", flash_type="error"))

    try:
        cur.execute(
            "INSERT INTO class_assignments (quiz_id, class_id) VALUES (?, ?)",
            (quiz_id, int(class_id))
        )
        conn.commit()
        flash = f'Assigned "{quiz_row[0]}" to class "{cls[0]}".'
        flash_type = "success"
    except sqlite3.IntegrityError:
        flash = f'Class "{cls[0]}" is already assigned to "{quiz_row[0]}".'
        flash_type = "error"
    conn.close()
    return redirect(url_for("home", flash=flash, flash_type=flash_type))


@app.route("/quiz/<int:quiz_id>/delete", methods=["POST"])
def delete_quiz(quiz_id):
    if not is_teacher():
        return redirect(url_for("home"))

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM options WHERE question_id IN (
            SELECT id FROM questions WHERE quiz_id = ?
        )
    """, (quiz_id,))
    cur.execute("""
        DELETE FROM answers WHERE result_id IN (
            SELECT id FROM results WHERE quiz_id = ?
        )
    """, (quiz_id,))
    cur.execute("DELETE FROM questions WHERE quiz_id = ?", (quiz_id,))
    cur.execute("DELETE FROM results WHERE quiz_id = ?", (quiz_id,))
    cur.execute("DELETE FROM assignments WHERE quiz_id = ?", (quiz_id,))
    cur.execute("DELETE FROM class_assignments WHERE quiz_id = ?", (quiz_id,))
    cur.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))

    conn.commit()
    conn.close()

    return redirect(url_for("home"))


RESULTS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Results Dashboard — TestPortal</title>
    <style>
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        :root {
            --primary: #4f46e5;
            --bg: #f1f5f9;
            --card: #ffffff;
            --border: #e2e8f0;
            --text: #1e293b;
            --muted: #64748b;
            --radius: 10px;
            --shadow: 0 1px 3px rgba(0,0,0,0.08), 0 4px 16px rgba(0,0,0,0.06);
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg);
            color: var(--text);
            line-height: 1.6;
            min-height: 100vh;
        }

        nav {
            background: var(--card);
            border-bottom: 1px solid var(--border);
            padding: 0 24px;
            display: flex;
            align-items: center;
            height: 56px;
            gap: 32px;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: 0 1px 3px rgba(0,0,0,0.06);
        }
        nav .brand {
            font-weight: 700;
            font-size: 17px;
            color: var(--primary);
            text-decoration: none;
            letter-spacing: -0.3px;
        }
        nav a {
            text-decoration: none;
            color: var(--muted);
            font-size: 14px;
            font-weight: 500;
            padding: 6px 2px;
            border-bottom: 2px solid transparent;
            transition: color 0.15s, border-color 0.15s;
        }
        nav a:hover { color: var(--primary); }
        nav a.active { color: var(--primary); border-bottom-color: var(--primary); }
        nav .nav-right { margin-left: auto; }
        .btn-nav {
            padding: 6px 14px;
            font-size: 13px;
            font-weight: 500;
            background: var(--primary);
            color: #fff !important;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            text-decoration: none;
            border-bottom: none !important;
        }
        .btn-nav:hover { background: #4338ca; color: #fff !important; }

        .page { max-width: 860px; margin: 40px auto; padding: 0 20px 60px; }

        .page-header { margin-bottom: 24px; }
        .page-header h1 { font-size: 24px; font-weight: 700; }
        .page-header p  { color: var(--muted); font-size: 14px; margin-top: 4px; }

        .card {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            box-shadow: var(--shadow);
            overflow: hidden;
        }

        table { width: 100%; border-collapse: collapse; }

        thead { background: #f8fafc; }
        th {
            padding: 11px 18px;
            text-align: left;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.07em;
            color: var(--muted);
            border-bottom: 1px solid var(--border);
        }
        td {
            padding: 13px 18px;
            font-size: 14px;
            border-bottom: 1px solid var(--border);
            color: var(--text);
        }
        tbody tr:last-child td { border-bottom: none; }
        tbody tr:hover { background: #f8fafc; }

        .score-pill {
            display: inline-block;
            background: #ede9fe;
            color: var(--primary);
            font-weight: 700;
            font-size: 13px;
            padding: 2px 10px;
            border-radius: 999px;
        }
        .id-badge {
            font-size: 12px;
            color: var(--muted);
            font-family: monospace;
        }

        .pct-pill {
            display: inline-block;
            background: #f0fdf4;
            color: #16a34a;
            font-weight: 700;
            font-size: 13px;
            padding: 2px 10px;
            border-radius: 999px;
        }
        .pct-pill.fail {
            background: #fef2f2;
            color: #dc2626;
        }
        .percentile-badge {
            font-size: 12px;
            color: var(--muted);
            white-space: nowrap;
        }
        .date-cell {
            font-size: 12px;
            color: var(--muted);
            white-space: nowrap;
        }
        .duration-cell {
            font-size: 12px;
            color: var(--muted);
            white-space: nowrap;
        }

        .empty {
            padding: 40px 24px;
            text-align: center;
            color: var(--muted);
            font-size: 14px;
        }

        .page-header-row {
            display: flex;
            align-items: flex-end;
            justify-content: space-between;
            gap: 16px;
            flex-wrap: wrap;
            margin-bottom: 24px;
        }
        .page-header-row h1 { font-size: 24px; font-weight: 700; }
        .page-header-row p  { color: var(--muted); font-size: 14px; margin-top: 4px; }

        .btn-export {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 9px 16px;
            font-size: 13px;
            font-weight: 600;
            background: #f0fdf4;
            color: #16a34a;
            border: 1px solid #86efac;
            border-radius: 7px;
            text-decoration: none;
            white-space: nowrap;
            transition: background 0.15s;
        }
        .btn-export:hover { background: #dcfce7; }

        .btn-pdf {
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 4px 10px;
            font-size: 12px;
            font-weight: 600;
            background: #eff6ff;
            color: #2563eb;
            border: 1px solid #bfdbfe;
            border-radius: 6px;
            text-decoration: none;
            white-space: nowrap;
            transition: background 0.15s;
        }
        .btn-pdf:hover { background: #dbeafe; }

        .back-link {
            display: inline-block;
            margin-top: 24px;
            font-size: 14px;
            color: var(--muted);
            text-decoration: none;
        }
        .back-link:hover { color: var(--primary); }
    </style>
</head>
<body>

<nav>
    <a href="/" class="brand">TestPortal</a>
    <a href="/">Home</a>
    <a href="/results" class="active">Results</a>
    <span class="nav-right">
        <a href="/logout" class="btn-nav">Logout ({{ current_user }})</a>
    </span>
</nav>

<div class="page">
    <div class="page-header-row">
        <div>
            <h1>Results Dashboard</h1>
            <p>All student submissions, newest first.</p>
        </div>
        <a href="/export" class="btn-export">&#8595; Export CSV</a>
    </div>

    <div class="card">
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
                            <td>{{ row.student }}</td>
                            <td>{{ row.title }}</td>
                            <td><span class="score-pill">{{ row.score }} / {{ row.total }}</span></td>
                            <td><span class="pct-pill {% if row.pct < 50 %}fail{% endif %}">{{ row.pct }}%</span></td>
                            <td><span class="percentile-badge">{{ row.percentile }}th pct</span></td>
                            <td><span class="duration-cell">{{ row.duration }}</span></td>
                            <td><span class="date-cell">{{ row.submitted_at }}</span></td>
                            <td><a href="/result/{{ row.id }}/pdf" class="btn-pdf">&#8595; PDF</a></td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        {% else %}
            <div class="empty">No results yet. Students must submit a quiz first.</div>
        {% endif %}
    </div>

    <a href="/" class="back-link">&#8592; Back to homepage</a>
</div>

</body>
</html>
"""


@app.route("/results")
def results_dashboard():
    if not is_teacher():
        return redirect(url_for("home"))

    conn = sqlite3.connect(DB_NAME)
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
            "submitted_at": submitted_at or "—",
            "duration":     format_duration(duration_seconds),
        })

    conn.close()

    return render_template_string(RESULTS_HTML, results=rows, current_user=session.get("username"), is_teacher=True)


@app.route("/export")
def export_results():
    if not is_teacher():
        return redirect(url_for("home"))

    conn = sqlite3.connect(DB_NAME)
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
            submitted_at or "",
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

    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.execute("""
        SELECT r.id, r.student_name, q.title, r.score, r.total,
               r.submitted_at, r.duration_seconds, r.quiz_id
        FROM results r
        JOIN quizzes q ON r.quiz_id = q.id
        WHERE r.id = ?
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
        LEFT JOIN answers a ON a.question_id = q.id AND a.result_id = ?
        WHERE r.id = ?
        ORDER BY q.id ASC
    """, (result_id, result_id))
    answer_rows = cur.fetchall()

    print(f"[PDF debug] result_id={result_id}  questions found={len(answer_rows)}", flush=True)

    # Fetch all options for each question
    options_by_question = {}
    for (qid, _, _, _) in answer_rows:
        cur.execute("SELECT option_text FROM options WHERE question_id = ? ORDER BY id ASC", (qid,))
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
    if submitted_at:
        meta_lines += f"<br/>Submitted: {submitted_at}"
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


if __name__ == "__main__":
    init_db()
    app.run(debug=True)