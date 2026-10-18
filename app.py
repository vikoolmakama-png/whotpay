from flask import Flask, render_template, request, redirect, session, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3
from datetime import datetime
import os
import secrets
import re

app = Flask(__name__)
app.secret_key = os.environ.get("WHOTPAY_SECRET", secrets.token_hex(32))

DB = "whotpay.db"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

PLANS = [
    {"entry": 50, "prize": 85},
    {"entry": 100, "prize": 180},
    {"entry": 200, "prize": 350},
    {"entry": 300, "prize": 500},
    {"entry": 500, "prize": 800},
    {"entry": 1000, "prize": 1800},
]


class HybridRow:
    """Supports both SQLite-style row['column'] and row[0] access."""
    def __init__(self, values, names):
        self._values = tuple(values)
        self._names = tuple(names)
        self._map = dict(zip(self._names, self._values))

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._map[key]
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def keys(self):
        return self._names


class CompatCursor:
    def __init__(self, cursor, db, lastrowid=None):
        self.cursor = cursor
        self.db = db
        self._lastrowid = lastrowid

    @property
    def lastrowid(self):
        return self._lastrowid

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def _row(self, row):
        if row is None:
            return None
        names = [col.name if hasattr(col, "name") else col[0]
                 for col in (self.cursor.description or [])]
        return HybridRow(row, names)

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        for row in self.cursor:
            yield self._row(row)


class CompatConnection:
    def __init__(self, raw, postgres):
        self.raw = raw
        self.postgres = postgres
        self.last_changes = 0

    def execute(self, sql, params=()):
        sql = sql.strip()
        if self.postgres:
            if sql.upper() == "PRAGMA TABLE_INFO(USERS)":
                cur = self.raw.execute("""
                    SELECT column_name AS name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'users'
                    ORDER BY ordinal_position
                """)
                return CompatCursor(cur, self)
            if sql.upper() == "SELECT CHANGES()":
                cur = self.raw.execute("SELECT %s AS changes", (self.last_changes,))
                return CompatCursor(cur, self)

            if sql.upper() == "BEGIN IMMEDIATE":
                if self.raw.info.transaction_status == 0:
                    self.raw.execute("BEGIN")
                return CompatCursor(self.raw.cursor(), self)

            sql = sql.replace("BEGIN IMMEDIATE", "BEGIN")
            sql = sql.replace("datetime('now', 'localtime')", "CURRENT_TIMESTAMP")
            sql = sql.replace("datetime('now','localtime')", "CURRENT_TIMESTAMP")
            sql = sql.replace("date('now', 'start of month')",
                              "date_trunc('month', CURRENT_DATE)::date")
            sql = sql.replace("date(created_at)", "CAST(created_at AS date)")
            sql = sql.replace("instr(commission.details, '#')",
                              "POSITION('#' IN commission.details)")
            sql = sql.replace("?", "%s")

            is_insert = sql.lstrip().upper().startswith("INSERT INTO")
            if is_insert and "RETURNING" not in sql.upper():
                sql = sql.rstrip().rstrip(";") + " RETURNING id"
            cur = self.raw.execute(sql, params)
            self.last_changes = cur.rowcount if cur.rowcount >= 0 else 0
            last_id = None
            if is_insert:
                row = cur.fetchone()
                if row is not None:
                    last_id = row[0]
            return CompatCursor(cur, self, last_id)

        cur = self.raw.execute(sql, params)
        self.last_changes = cur.rowcount if cur.rowcount >= 0 else 0
        return CompatCursor(cur, self, getattr(cur, "lastrowid", None))

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


def connect():
    if DATABASE_URL:
        import psycopg
        raw = psycopg.connect(DATABASE_URL, autocommit=False)
        return CompatConnection(raw, True)
    raw = sqlite3.connect(DB)
    raw.row_factory = sqlite3.Row
    return CompatConnection(raw, False)


def initialize():
    with connect() as db:
        id_def = ("INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
                  if db.postgres else "INTEGER PRIMARY KEY AUTOINCREMENT")
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS users (
                id {id_def},
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0,
                fullname TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                referral_code TEXT,
                referred_by TEXT
            )
        """)
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS transactions (
                id {id_def},
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                details TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        db.execute(f"""
            CREATE TABLE IF NOT EXISTS game_sessions (
                id {id_def},
                user_id INTEGER NOT NULL,
                entry_fee INTEGER NOT NULL,
                prize INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'started',
                result_verified INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                settled_at TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)
        # SQLite needs its schema checked differently; PostgreSQL uses the
        # information_schema compatibility query above.
        columns = {row["name"] for row in db.execute(
            "PRAGMA table_info(users)").fetchall()}
        additions = {
            "fullname": "TEXT NOT NULL DEFAULT ''",
            "email": "TEXT NOT NULL DEFAULT ''",
            "referral_code": "TEXT",
            "referred_by": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS users_referral_unique
            ON users(referral_code) WHERE referral_code IS NOT NULL
        """)
        db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique
            ON users(email) WHERE email != ''
        """)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

@app.route("/")
def welcome():
    return render_template("welcome.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    error = ""

    if request.method == "POST":
        fullname = request.form.get("fullname", "").strip()
        email = request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        referral = request.form.get("referral", "").strip().upper()

        if not fullname or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            error = "Enter your full name and a valid email address."
        elif not re.fullmatch(r"[A-Za-z0-9_]{3,30}", username):
            error = "Username must be 3–30 letters, numbers, or underscores."
        elif len(password) < 8:
            error = "Password must contain at least 8 characters."
        else:
            try:
                with connect() as db:
                    if referral:
                        referrer = db.execute(
                            "SELECT id FROM users WHERE referral_code = ?",
                            (referral,)
                        ).fetchone()
                        if not referrer:
                            error = "That referral code does not exist."

                    if not error:
                        code = secrets.token_hex(4).upper()
                        cursor = db.execute("""
                            INSERT INTO users
                            (fullname, email, username, password,
                             balance, referral_code, referred_by)
                            VALUES (?, ?, ?, ?, 80, ?, ?)
                        """, (
                            fullname, email, username,
                            generate_password_hash(password),
                            code, referral if referral else None
                        ))

                        new_user_id = cursor.lastrowid

                        db.execute("""
                            INSERT INTO transactions
                            (user_id, kind, amount, status, details)
                            VALUES (?, 'welcome_bonus', 80, 'Approved',
                                    '₦80 welcome bonus')
                        """, (new_user_id,))

                        return redirect("/login?registered=1")

            except Exception as exc:
                if not isinstance(exc, sqlite3.IntegrityError) and exc.__class__.__name__ not in ("UniqueViolation", "IntegrityError"):
                    raise
                error = "Username or email is already registered."

    return render_template("register.html", error=error, referral=request.args.get("ref", ""))

@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    success = request.args.get("registered") == "1"

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        with connect() as db:
            user = db.execute(
                "SELECT * FROM users WHERE username = ?",
                (username,)
            ).fetchone()

        if user and check_password_hash(user["password"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect("/dashboard")

        error = "Incorrect username or password."

    return render_template("login.html", error=error, success=success)

@app.route("/dashboard")
@login_required
def dashboard():
    with connect() as db:
        user = db.execute(
            "SELECT * FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()

        if user is None:
            session.clear()
            return redirect("/login")

        pending = db.execute("""
            SELECT COUNT(*) AS total FROM transactions
            WHERE user_id = ? AND status = 'Pending'
        """, (user["id"],)).fetchone()["total"]

        referral_count = db.execute("""
            SELECT COUNT(*) AS total FROM users WHERE referred_by = ?
        """, (user["referral_code"],)).fetchone()["total"]

    return render_template(
        "dashboard.html",
        user=user,
        plans=PLANS,
        pending=pending,
        referral_count=referral_count,
        referral_link=url_for(
            "register", _external=True
        ) + "?ref=" + (user["referral_code"] or "")
    )

@app.route("/deposit", methods=["GET", "POST"])
@login_required
def deposit():
    error = ""

    if request.method == "POST":
        sender = request.form.get("sender", "").strip()
        amount_text = request.form.get("amount", "").strip()

        try:
            amount = int(amount_text)
            if amount < 100 or amount > 10000000:
                raise ValueError
            if not sender or len(sender) > 100:
                raise ValueError
        except ValueError:
            error = "Enter a valid sender name and amount of at least ₦100."
        else:
            with connect() as db:
                db.execute("""
                    INSERT INTO transactions
                    (user_id, kind, amount, status, details)
                    VALUES (?, 'Deposit', ?, 'Pending', ?)
                """, (session["user_id"], amount,
                      "Sender: " + sender))
            flash("Deposit submitted for admin verification.", "success")
            return redirect("/history")

    return render_template("deposit.html", error=error)

@app.route("/withdraw", methods=["GET", "POST"])
@login_required
def withdraw():
    error = ""

    if request.method == "POST":
        bank = request.form.get("bank", "").strip()
        account_name = request.form.get("account_name", "").strip()
        account_number = request.form.get("account_number", "").strip()
        amount_text = request.form.get("amount", "").strip()

        try:
            amount = int(amount_text)
            if amount < 300 or amount > 10000000:
                raise ValueError
            if not bank or len(bank) > 80:
                raise ValueError
            if not account_name or len(account_name) > 100:
                raise ValueError
            if not re.fullmatch(r"\d{10}", account_number):
                raise ValueError
        except ValueError:
            error = "Check your details. Withdrawal minimum is ₦300 and account number must be 10 digits."
        else:
            with connect() as db:
                user = db.execute(
                    "SELECT balance FROM users WHERE id = ?",
                    (session["user_id"],)
                ).fetchone()

                reserved = db.execute("""
                    SELECT COALESCE(SUM(amount), 0) AS total
                    FROM transactions
                    WHERE user_id = ? AND kind = 'Withdrawal'
                    AND status = 'Pending'
                """, (session["user_id"],)).fetchone()["total"]

                if amount > user["balance"] - reserved:
                    error = "Insufficient available balance."
                else:
                    details = (
                        f"Bank: {bank}; Account name: {account_name}; "
                        f"Account number: {account_number}"
                    )
                    db.execute("""
                        INSERT INTO transactions
                        (user_id, kind, amount, status, details)
                        VALUES (?, 'Withdrawal', ?, 'Pending', ?)
                    """, (session["user_id"], amount, details))

                    flash("Withdrawal request submitted for review.", "success")
                    return redirect("/history")

    return render_template("withdraw.html", error=error)

@app.route("/history")
@login_required
def history():
    with connect() as db:
        transactions = db.execute("""
            SELECT * FROM transactions
            WHERE user_id = ?
            ORDER BY id DESC
        """, (session["user_id"],)).fetchall()

    return render_template("history.html", transactions=transactions)

@app.route("/referrals")
@login_required
def referrals():
    from urllib.parse import quote

    with connect() as db:
        user = db.execute(
            "SELECT referral_code FROM users WHERE id = ?",
            (session["user_id"],)
        ).fetchone()

        if not user:
            return redirect(url_for("login"))

        code = user["referral_code"]

        count = db.execute("""
            SELECT COUNT(*) AS total
            FROM users
            WHERE referred_by = ?
        """, (code,)).fetchone()["total"]

        total_earned = db.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE user_id = ?
              AND kind = 'referral_commission'
              AND status = 'Approved'
        """, (session["user_id"],)).fetchone()[0]

        month_earned = db.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE user_id = ?
              AND kind = 'referral_commission'
              AND status = 'Approved'
              AND date(created_at) >= date('now', 'start of month')
        """, (session["user_id"],)).fetchone()[0]

        earnings = db.execute("""
            SELECT
                commission.id,
                commission.amount AS commission,
                commission.created_at,
                deposit.amount AS deposit_amount,
                invited.username AS referred_username,
                commission.status
            FROM transactions AS commission
            LEFT JOIN transactions AS deposit
              ON deposit.id = CAST(
                   SUBSTRING(
                     commission.details
                     FROM POSITION('#' IN commission.details) + 1
                   ) AS INTEGER
                 )
             AND lower(deposit.kind) LIKE '%deposit%'
            LEFT JOIN users AS invited
              ON invited.id = deposit.user_id
            WHERE commission.user_id = ?
              AND commission.kind = 'referral_commission'
            ORDER BY commission.id DESC
            LIMIT 100
        """, (session["user_id"],)).fetchall()

    link = url_for("register", _external=True) + "?ref=" + quote(code)

    return render_template(
        "referrals.html",
        code=code,
        link=link,
        count=count,
        total_earned=total_earned,
        month_earned=month_earned,
        earnings=earnings
    )

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

initialize()




def start_paid_game(user_id, entry):
    """Atomically deduct an entry fee and create one game session."""
    plan = next((p for p in PLANS if p["entry"] == entry), None)
    if plan is None:
        return None, "Choose a valid entry plan."

    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        user = db.execute(
            "SELECT id, balance FROM users WHERE id = ? FOR UPDATE",
            (user_id,)
        ).fetchone()

        if user is None:
            db.rollback()
            return None, "Account not found."

        if user["balance"] < plan["entry"]:
            db.rollback()
            return None, "Insufficient balance for this entry fee."

        db.execute(
            "UPDATE users SET balance = balance - ? "
            "WHERE id = ? AND balance >= ?",
            (plan["entry"], user_id, plan["entry"])
        )
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            db.rollback()
            return None, "Entry payment failed. Please try again."

        cur = db.execute("""
            INSERT INTO game_sessions
                (user_id, entry_fee, prize, status, result_verified)
            VALUES (?, ?, ?, 'started', 0)
        """, (user_id, plan["entry"], plan["prize"]))
        game_id = cur.lastrowid

        db.execute("""
            INSERT INTO transactions
                (user_id, kind, amount, status, details)
            VALUES (?, 'game_entry', ?, 'Approved', ?)
        """, (user_id, plan["entry"], f"Whot game entry #{game_id}"))

        db.commit()
        return {"id": game_id, "entry": plan["entry"],
                "prize": plan["prize"]}, None
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def settle_verified_game(game_id, user_id, won):
    """Settle only a result already verified by trusted server-side game logic."""
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        game = db.execute("""
            SELECT id, entry_fee, prize, status, result_verified
            FROM game_sessions
            WHERE id = ? AND user_id = ? FOR UPDATE
        """, (game_id, user_id)).fetchone()

        if game is None or game["status"] != "started":
            db.rollback()
            return False, "Game not found or already settled."

        # This function intentionally refuses browser-supplied win claims.
        if not won or game["result_verified"] != 1:
            db.rollback()
            return False, "The server has not verified a win."

        db.execute("""
            UPDATE users SET balance = balance + ?
            WHERE id = ?
        """, (game["prize"], user_id))

        db.execute("""
            INSERT INTO transactions
                (user_id, kind, amount, status, details)
            VALUES (?, 'game_prize', ?, 'Approved', ?)
        """, (user_id, game["prize"], f"Verified Whot win #{game_id}"))

        db.execute("""
            UPDATE game_sessions
            SET status = 'won', settled_at = datetime('now', 'localtime')
            WHERE id = ? AND user_id = ? AND status = 'started'
        """, (game_id, user_id))
        if db.execute("SELECT changes()").fetchone()[0] != 1:
            db.rollback()
            return False, "Game settlement failed."

        db.commit()
        return True, "Verified prize credited."
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()



@app.route("/play", methods=["GET", "POST"])
@login_required
def play_whot():
    selected = request.values.get("entry", type=int)
    plan = next((p for p in PLANS if p["entry"] == selected), None)
    game_id = None

    if selected is not None and plan is None:
        flash("Choose a valid entry plan.", "error")
        return redirect("/dashboard")

    if request.method == "POST":
        game, error = start_paid_game(session["user_id"], selected)
        if error:
            flash(error, "error")
            return redirect("/dashboard")
        game_id = game["id"]

    return render_template(
        "game.html",
        selected_plan=plan,
        plans=PLANS,
        game_id=game_id
    )


@app.route("/api/game/settle", methods=["POST"])
@login_required
def settle_game_api():
    # The browser controls the game UI and cannot be trusted to declare a win.
    # Do not credit paid-game prizes until server-side game state is authoritative.
    return {
        "ok": False,
        "error": "Paid-game result verification is not enabled yet."
    }, 409


# ADMIN_DASHBOARD_INSTALLED
import hmac

ADMIN_USERNAME = os.environ.get("WHOTPAY_ADMIN_USERNAME", "Vikool")
ADMIN_PASSWORD = os.environ.get("WHOTPAY_ADMIN_PASSWORD", "Whot@5050")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("whotpay_admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("whotpay_admin"):
        return redirect(url_for("admin_dashboard"))

    error = ""
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        valid_user = hmac.compare_digest(username, ADMIN_USERNAME)
        valid_pass = hmac.compare_digest(password, ADMIN_PASSWORD)

        if valid_user and valid_pass:
            session.clear()
            session["whotpay_admin"] = True
            return redirect(url_for("admin_dashboard"))

        error = "Incorrect admin username or password."

    return render_template("admin_login.html", error=error)


@app.route("/admin")
@admin_required
def admin_dashboard():
    with connect() as db:
        total_users = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        users = db.execute("""
            SELECT id, username, fullname, email, balance
            FROM users
            ORDER BY id DESC
        """).fetchall()

        total_balance = db.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM users"
        ).fetchone()[0]

        transactions = db.execute("""
            SELECT t.id, t.user_id, t.kind, t.amount, t.status,
                   t.details, t.created_at, u.username,
                   u.fullname, u.balance
            FROM transactions AS t
            LEFT JOIN users AS u ON u.id = t.user_id
            ORDER BY t.id DESC
        """).fetchall()

    deposits = [
        t for t in transactions
        if "deposit" in str(t["kind"]).lower()
    ]
    withdrawals = [
        t for t in transactions
        if "withdraw" in str(t["kind"]).lower()
    ]

    return render_template(
        "admin_dashboard.html",
        users=users,
        transactions=transactions,
        deposits=deposits,
        withdrawals=withdrawals,
        total_users=total_users,
        total_balance=total_balance
    )


@app.route("/admin/logout")
@admin_required
def admin_logout():
    session.pop("whotpay_admin", None)
    return redirect(url_for("admin_login"))




@app.route("/admin/transaction/<int:transaction_id>/<action>", methods=["POST"])
@admin_required
def admin_transaction_action(transaction_id, action):
    from flask import abort

    if action not in ("approve", "reject"):
        abort(400)

    with connect() as db:
        # Lock the decision into one database transaction. A request already
        # processed cannot be approved or credited a second time.
        db.execute("BEGIN IMMEDIATE")

        tx = db.execute("""
            SELECT id, user_id, kind, amount, status, details
            FROM transactions
            WHERE id = ?
        """, (transaction_id,)).fetchone()

        if tx is None:
            db.rollback()
            flash("Transaction not found.", "error")
            return redirect(url_for("admin_dashboard"))

        if str(tx["status"]).strip().lower() not in ("pending",):
            db.rollback()
            flash("This request has already been processed.", "error")
            return redirect(url_for("admin_dashboard"))

        kind = str(tx["kind"]).strip().lower()
        amount = int(tx["amount"])

        if amount <= 0:
            db.rollback()
            flash("Invalid transaction amount.", "error")
            return redirect(url_for("admin_dashboard"))

        is_deposit = "deposit" in kind
        is_withdrawal = "withdraw" in kind

        if not is_deposit and not is_withdrawal:
            db.rollback()
            flash("This transaction type cannot be approved here.", "error")
            return redirect(url_for("admin_dashboard"))

        user = db.execute(
            "SELECT id, balance FROM users WHERE id = ? FOR UPDATE",
            (tx["user_id"],)
        ).fetchone()

        if user is None:
            db.rollback()
            flash("The associated user account was not found.", "error")
            return redirect(url_for("admin_dashboard"))

        if action == "reject":
            db.execute(
                "UPDATE transactions SET status = 'Rejected' "
                "WHERE id = ? AND status = 'Pending'",
                (transaction_id,)
            )
            db.commit()
            flash("Request rejected. User balance was not changed.", "success")
            return redirect(url_for("admin_dashboard"))

        if is_withdrawal and int(user["balance"]) < amount:
            db.rollback()
            flash("Withdrawal cannot be approved: insufficient user balance.", "error")
            return redirect(url_for("admin_dashboard"))

        # Change status first, guarded by Pending, to prevent duplicate credits.
        changed = db.execute("""
            UPDATE transactions
            SET status = 'Approved'
            WHERE id = ? AND status = 'Pending'
        """, (transaction_id,)).rowcount

        if changed != 1:
            db.rollback()
            flash("This request was already processed.", "error")
            return redirect(url_for("admin_dashboard"))

        if is_deposit:
            # Credit the depositor after approval.
            db.execute(
                "UPDATE users SET balance = balance + ? WHERE id = ?",
                (amount, tx["user_id"])
            )

            # Pay 30% commission to the direct referrer, if any.
            depositor = db.execute(
                "SELECT referred_by FROM users WHERE id = ?",
                (tx["user_id"],)
            ).fetchone()

            if depositor and depositor["referred_by"]:
                referrer = db.execute(
                    "SELECT id FROM users WHERE referral_code = ?",
                    (depositor["referred_by"],)
                ).fetchone()

                commission = amount * 30 // 100

                if referrer and referrer["id"] != tx["user_id"] and commission > 0:
                    # A unique transaction detail makes each commission
                    # attributable to one original deposit.
                    commission_detail = f"Referral commission for deposit #{transaction_id}"

                    existing = db.execute("""
                        SELECT 1 FROM transactions
                        WHERE kind = 'referral_commission' AND details = ?
                        LIMIT 1
                    """, (commission_detail,)).fetchone()

                    if not existing:
                        db.execute(
                            "UPDATE users SET balance = balance + ? WHERE id = ?",
                            (commission, referrer["id"])
                        )
                        db.execute("""
                            INSERT INTO transactions
                                (user_id, kind, amount, status, details)
                            VALUES (?, 'referral_commission', ?, 'Approved', ?)
                        """, (
                            referrer["id"],
                            commission,
                            commission_detail
                        ))
        else:
            db.execute(
                "UPDATE users SET balance = balance - ? "
                "WHERE id = ? AND balance >= ?",
                (amount, tx["user_id"], amount)
            )
            updated = db.execute(
                "SELECT balance FROM users WHERE id = ?",
                (tx["user_id"],)
            ).fetchone()
            if updated is None or int(updated["balance"]) < 0:
                db.rollback()
                flash("Withdrawal could not be completed.", "error")
                return redirect(url_for("admin_dashboard"))

        db.commit()

    flash(
        "Deposit approved and user balance updated."
        if is_deposit else
        "Withdrawal approved and user balance deducted.",
        "success"
    )
    return redirect(url_for("admin_dashboard"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
