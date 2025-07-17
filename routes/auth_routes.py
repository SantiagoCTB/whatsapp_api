from flask import Blueprint, render_template, request, redirect, session, url_for
import hashlib
import sqlite3
from config import Config

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        hashed = hashlib.sha256(password.encode()).hexdigest()

        conn = sqlite3.connect(Config.DB_PATH)
        c = conn.cursor()
        c.execute("SELECT * FROM usuarios WHERE username = ? AND password = ?", (username, hashed))
        user = c.fetchone()
        conn.close()

        if user:
            session["user"] = user[1]
            session["rol"] = user[3]
            return redirect("/")
        else:
            error = "Usuario o contraseña incorrectos"

    return render_template("login.html", error=error)

@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
