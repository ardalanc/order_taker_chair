import telebot
import mysql.connector
import threading
import json
import jdatetime
import traceback
import logging
import logging.handlers
import os
import time
from datetime import datetime, timedelta
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup,
    Message, CallbackQuery
)
from config import BOT_TOKEN, DATABASE_CONFIG, DB_NAME, ADMIN_IDS
from Texts import texts

telebot.apihelper.API_URL = "http://tapi.bale.ai/bot{0}/{1}"

# ════════════════════════════════════════════════════════════════
#  LOGGING SETUP
# ════════════════════════════════════════════════════════════════

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

_formatter = logging.Formatter(
    fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

def _make_file_handler(filename: str, level=logging.DEBUG) -> logging.handlers.RotatingFileHandler:
    h = logging.handlers.RotatingFileHandler(
        os.path.join(LOG_DIR, filename),
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8"
    )
    h.setLevel(level)
    h.setFormatter(_formatter)
    return h

_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_formatter)

# Main bot logger
logger = logging.getLogger("bot")
logger.setLevel(logging.DEBUG)
logger.addHandler(_console_handler)
logger.addHandler(_make_file_handler("bot.log"))

# Database logger
db_logger = logging.getLogger("bot.db")
db_logger.setLevel(logging.DEBUG)
db_logger.addHandler(_make_file_handler("db.log"))
db_logger.propagate = True

# Error logger
err_logger = logging.getLogger("bot.errors")
err_logger.setLevel(logging.ERROR)
err_logger.addHandler(_make_file_handler("errors.log", level=logging.ERROR))
err_logger.propagate = True

# Scheduler logger
sched_logger = logging.getLogger("bot.scheduler")
sched_logger.setLevel(logging.DEBUG)
sched_logger.addHandler(_make_file_handler("scheduler.log"))
sched_logger.propagate = True

logger.info("Logging system initialized. Directory: %s", os.path.abspath(LOG_DIR))


class GlobalExceptionHandler(telebot.ExceptionHandler):
    """
    تمام exception هایی که توی handler ها (message/callback) رخ بدن از اینجا رد می‌شن.
    هدف: ربات هیچ‌وقت کرش نکنه و کاربر/ادمین پیام مناسب ببینن.
    """
    def handle(self, exception):
        print(f"[UNHANDLED EXCEPTION] {type(exception).__name__}: {exception}")
        err_logger.exception("UNHANDLED EXCEPTION in polling loop: %s", exception)
        traceback.print_exc()

        # سعی می‌کنیم chat_id رو از stack trace پیدا کنیم تا به کاربر اطلاع بدیم
        # (telebot جزئیات update رو در دسترس handler نمی‌ذاره، پس فقط لاگ و گزارش به ادمین)
        try:
            err_text = f"⚠️ خطای داخلی ربات\n{type(exception).__name__}: {str(exception)[:300]}"
            for admin_cid in ADMIN_IDS:
                try:
                    bot.send_message(admin_cid, err_text)
                except Exception:
                    pass
        except Exception:
            pass
        return True

bot = telebot.TeleBot(BOT_TOKEN, exception_handler=GlobalExceptionHandler())

PAGE_SIZE = 20

# ════════════════════════════════════════════════════════════════
#  تبدیل تاریخ میلادی ↔ شمسی
# ════════════════════════════════════════════════════════════════

def to_jalali(dt, fmt: str = "%Y/%m/%d") -> str:
    """تبدیل datetime یا date میلادی (از دیتابیس) به رشته‌ی شمسی."""
    if dt is None:
        return "—"
    try:
        if hasattr(dt, 'hour'):
            return jdatetime.datetime.fromgregorian(datetime=dt).strftime(fmt)
        else:
            return jdatetime.date.fromgregorian(date=dt).strftime(fmt)
    except Exception:
        return str(dt)

def to_jalali_full(dt) -> str:
    """تاریخ و ساعت شمسی: ۱۴۰۳/۰۶/۱۵ ۱۴:۳۰"""
    return to_jalali(dt, "%Y/%m/%d %H:%M")

def _parse_date_str(s) -> object:
    """تبدیل رشته‌ی میلادی (YYYY-MM-DD) از دیتابیس به date برای ورودی to_jalali."""
    if s is None:
        return None
    if hasattr(s, 'year'):
        return s
    try:
        import datetime as _dt
        return _dt.date.fromisoformat(str(s).strip())
    except Exception:
        return None

def jalali_to_gregorian(jalali_str: str) -> str:
    """تبدیل رشته‌ی شمسی (۱۴۰۳-۰۶-۱۵ یا ۱۴۰۳/۰۶/۱۵) به میلادی برای دیتابیس."""
    s = jalali_str.strip().replace("/", "-")
    parts = s.split("-")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    g = jdatetime.date(y, m, d).togregorian()
    return g.strftime("%Y-%m-%d")

def validate_jalali(s: str) -> bool:
    """چک می‌کنه تاریخ شمسی معتبره یا نه."""
    try:
        s = s.strip().replace("/", "-")
        parts = s.split("-")
        if len(parts) != 3:
            return False
        jdatetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
        return True
    except Exception:
        return False



def safe_handler(func):
    """
    دکوراتور برای message/callback handler ها.
    DBError رو می‌گیره و به همون کاربر (نه فقط لاگ) پیام مناسب می‌ده،
    بدون اینکه کل polling loop رو بترکونه.
    کار با هر دو نوع آرگومان (Message یا CallbackQuery) می‌کنه.
    """
    def wrapper(update, *args, **kwargs):
        try:
            return func(update, *args, **kwargs)
        except DBError as e:
            cid = update.message.chat.id if isinstance(update, CallbackQuery) else update.chat.id
            print(f"[DBError in {func.__name__}] {e}")
            db_logger.error("DBError in handler=%s | cid=%s | %s", func.__name__, cid, e)
            try:
                bot.send_message(cid, texts["db_error_msg"])
                if isinstance(update, CallbackQuery):
                    bot.answer_callback_query(update.id)
            except Exception:
                pass
        except Exception as e:
            cid = update.message.chat.id if isinstance(update, CallbackQuery) else update.chat.id
            print(f"[UNEXPECTED ERROR in {func.__name__}] {type(e).__name__}: {e}")
            err_logger.exception("Unexpected error in handler=%s | cid=%s | %s: %s",
                                 func.__name__, cid, type(e).__name__, e)
            traceback.print_exc()
            try:
                bot.send_message(cid, texts["unexpected_error_msg"])
                if isinstance(update, CallbackQuery):
                    bot.answer_callback_query(update.id)
            except Exception:
                pass
            for admin_cid in ADMIN_IDS:
                try:
                    bot.send_message(admin_cid,
                        f"⚠️ خطا در {func.__name__}\n{type(e).__name__}: {str(e)[:300]}")
                except Exception:
                    pass
    wrapper.__name__ = func.__name__
    return wrapper


STATUS_FA = {
    "pending":   "⏳ در انتظار تایید",
    "approved":  "✅ تایید شده",
    "producing": "🔧 در حال تولید",
    "ready":     "📦 آماده تحویل",
    "delivered": "🚚 تحویل داده شد",
    "rejected":  "❌ رد شده",
    "cancelled": "🚫 لغو شده",
}
STATUS_EMOJI = {k: v.split()[0] for k, v in STATUS_FA.items()}
TYPE_FA = {
    "order": "🛒 سفارش", "payment": "💵 پرداخت",
    "debt_added": "➕ بدهی", "installment": "📋 قسط",
    "month_transfer": "🔄 انتقال",
}

# ════════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════════

class DBError(Exception):
    """خطای دیتابیس - برای نمایش پیام مناسب به کاربر/ادمین استفاده می‌شه."""
    pass


def get_connection():
    try:
        return mysql.connector.connect(database=DB_NAME, **DATABASE_CONFIG)
    except mysql.connector.Error as e:
        print(f"[DB ERROR] Failed to connect to database: {e}")
        db_logger.critical("Failed to connect to database: %s", e)
        raise DBError("اتصال به دیتابیس برقرار نشد") from e


def db_safe(func):
    """
    دکوراتور برای همه‌ی توابع db_*.
    خطاهای mysql.connector رو می‌گیره، لاگ می‌کنه، و به DBError یکپارچه تبدیل می‌کنه
    تا توی هندلرهای بالادست بشه یکجا مدیریتش کرد.
    """
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except DBError:
            raise
        except mysql.connector.Error as e:
            print(f"[DB ERROR] {func.__name__}: {e}")
            db_logger.error("MySQL error in %s: %s", func.__name__, e)
            raise DBError(f"خطا در عملیات دیتابیس ({func.__name__})") from e
    wrapper.__name__ = func.__name__
    return wrapper

# ── کاربر ────────────────────────────────────────────────────────

@db_safe
def db_get_user(cid: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE cid=%s", (cid,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

@db_safe
def db_get_user_by_db_id(uid: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

@db_safe
def db_get_all_users_summary():
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT u.id, u.cid, u.name, u.username, u.is_banned,
               COALESCE(uf.current_month_debt,0) AS current_month_debt,
               COALESCE(uf.previous_debt,0)      AS previous_debt
        FROM users u LEFT JOIN user_finance uf ON uf.user_id=u.id
        ORDER BY u.name
    """)
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_add_user(cid, name, username, added_by):
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (cid,name,username,added_by) VALUES(%s,%s,%s,%s)",
                    (cid, name, username, added_by))
        uid = cur.lastrowid
        cur.execute("INSERT INTO user_finance (user_id) VALUES(%s)", (uid,))
        conn.commit(); return True
    except mysql.connector.IntegrityError:
        return False
    finally:
        cur.close(); conn.close()

@db_safe
def db_delete_user(uid: int):
    """حذف واقعی کاربر و همه داده‌هاش."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM installments   WHERE user_id=%s", (uid,))
    cur.execute("DELETE FROM transactions   WHERE user_id=%s", (uid,))
    cur.execute("DELETE FROM user_finance   WHERE user_id=%s", (uid,))
    # سفارشات (order_items با CASCADE حذف میشن)
    cur.execute("SELECT id FROM orders WHERE user_id=%s", (uid,))
    oids = [r[0] for r in cur.fetchall()]
    for oid in oids:
        cur.execute("DELETE FROM order_items WHERE order_id=%s", (oid,))
    cur.execute("DELETE FROM orders WHERE user_id=%s", (uid,))
    cur.execute("DELETE FROM users  WHERE id=%s",      (uid,))
    conn.commit(); cur.close(); conn.close()

@db_safe
def db_deactivate_user(uid: int):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned=TRUE, ban_reason=%s WHERE id=%s",
                (texts['deactivated_by_admin'], uid))
    conn.commit(); cur.close(); conn.close()

@db_safe
def db_ban_user(uid: int, reason: str):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned=TRUE,  ban_reason=%s WHERE id=%s", (reason, uid))
    conn.commit(); cur.close(); conn.close()

@db_safe
def db_unban_user(uid: int):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE users SET is_banned=FALSE, ban_reason=NULL WHERE id=%s", (uid,))
    conn.commit(); cur.close(); conn.close()

@db_safe
def db_get_user_finance(uid: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM user_finance WHERE user_id=%s", (uid,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

@db_safe
def db_get_user_full_report(uid: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE id=%s", (uid,)); user = cur.fetchone()
    if not user: cur.close(); conn.close(); return None
    cur.execute("SELECT * FROM user_finance WHERE user_id=%s", (uid,)); finance = cur.fetchone()
    cur.execute("""
        SELECT o.id,o.total_price,o.status,o.created_at,COUNT(oi.id) AS item_count
        FROM orders o LEFT JOIN order_items oi ON oi.order_id=o.id
        WHERE o.user_id=%s GROUP BY o.id ORDER BY o.created_at DESC LIMIT 10
    """, (uid,)); orders = cur.fetchall()
    cur.execute("""
        SELECT * FROM transactions WHERE user_id=%s ORDER BY created_at DESC LIMIT 15
    """, (uid,)); transactions = cur.fetchall()
    cur.execute("SELECT * FROM installments WHERE user_id=%s ORDER BY created_at DESC", (uid,))
    installments = cur.fetchall()
    cur.close(); conn.close()
    return {"user": user, "finance": finance, "orders": orders,
            "transactions": transactions, "installments": installments}

@db_safe
def db_get_user_orders_all(uid: int, limit: int = 50):
    """همه‌ی سفارشات یک کاربر (برای دکمه‌ی «📦 سفارشات» در جزئیات کاربر)."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT o.id,o.total_price,o.status,o.created_at
        FROM orders o WHERE o.user_id=%s ORDER BY o.created_at DESC LIMIT %s
    """, (uid, limit))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_get_user_transactions_all(uid: int, limit: int = 50):
    """همه‌ی تراکنش‌های یک کاربر (پرداخت‌ها، بدهی‌ها و ...) برای دکمه‌ی «💳 تراکنش‌ها»."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM transactions WHERE user_id=%s ORDER BY created_at DESC LIMIT %s
    """, (uid, limit))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_get_installment_payments(installment_id: int):
    """تراکنش‌های پرداخت ثبت‌شده برای یک قسط خاص (برای جزئیات قسط)."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM transactions WHERE installment_id=%s ORDER BY created_at DESC
    """, (installment_id,))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_get_installment_by_id(installment_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT i.*, a.name AS admin_name FROM installments i
        LEFT JOIN admins a ON a.id=i.created_by WHERE i.id=%s
    """, (installment_id,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

# ── ادمین ────────────────────────────────────────────────────────

@db_safe
def db_get_admin(cid: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM admins WHERE cid=%s", (cid,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

@db_safe
def db_get_admin_by_db_id(aid: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM admins WHERE id=%s", (aid,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

@db_safe
def db_add_admin(cid: int, name: str, role: str) -> bool:
    conn = get_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO admins (cid,name,role) VALUES(%s,%s,%s)", (cid, name, role))
        conn.commit(); return True
    except mysql.connector.IntegrityError:
        return False
    finally:
        cur.close(); conn.close()

# ── مدل ─────────────────────────────────────────────────────────

@db_safe
def db_get_active_models():
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.id, m.name, mp.price FROM models m
        JOIN model_prices mp ON mp.id=(
            SELECT id FROM model_prices WHERE model_id=m.id ORDER BY set_at DESC LIMIT 1)
        WHERE m.is_active=TRUE ORDER BY m.name
    """)
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_get_model_by_id(mid: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.id, m.name, m.is_active, mp.price FROM models m
        JOIN model_prices mp ON mp.id=(
            SELECT id FROM model_prices WHERE model_id=m.id ORDER BY set_at DESC LIMIT 1)
        WHERE m.id=%s
    """, (mid,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

@db_safe
def db_get_all_models():
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.id, m.name, m.is_active,
               COALESCE((SELECT price FROM model_prices WHERE model_id=m.id
                         ORDER BY set_at DESC LIMIT 1), 0) AS price
        FROM models m ORDER BY m.name
    """)
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_add_model(name: str, price: int, admin_id: int) -> int:
    conn = get_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO models (name) VALUES(%s)", (name,))
    mid = cur.lastrowid
    cur.execute("INSERT INTO model_prices (model_id,price,set_by) VALUES(%s,%s,%s)",
                (mid, price, admin_id))
    conn.commit(); cur.close(); conn.close(); return mid

@db_safe
def db_toggle_model(mid: int) -> bool:
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT is_active FROM models WHERE id=%s", (mid,)); row = cur.fetchone()
    new = not row["is_active"]
    cur.execute("UPDATE models SET is_active=%s WHERE id=%s", (new, mid))
    conn.commit(); cur.close(); conn.close(); return new

@db_safe
def db_delete_model(mid: int):
    """غیرفعال یا حذف واقعی."""
    conn = get_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM model_prices WHERE model_id=%s", (mid,))
    cur.execute("DELETE FROM models       WHERE id=%s",       (mid,))
    conn.commit(); cur.close(); conn.close()

@db_safe
def db_deactivate_model(mid: int):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE models SET is_active=FALSE WHERE id=%s", (mid,))
    conn.commit(); cur.close(); conn.close()

@db_safe
def db_set_model_price(mid: int, price: int, admin_id: int):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("INSERT INTO model_prices (model_id,price,set_by) VALUES(%s,%s,%s)",
                (mid, price, admin_id))
    conn.commit(); cur.close(); conn.close()

# ── سفارش ────────────────────────────────────────────────────────

@db_safe
def db_get_orders_paged(status_filter: str, offset: int, user_db_id: int = None,
                         show_price: bool = True) -> tuple[list, int]:
    """
    برمی‌گردونه (rows, total_count).
    status_filter: 'active' | 'all' | یا یکی از کلیدهای STATUS_FA (وضعیت خاص)
    """
    conn = get_connection(); cur = conn.cursor(dictionary=True)

    if status_filter == "active":
        where = "o.status NOT IN ('delivered','rejected','cancelled')"
    elif status_filter == "all":
        where = "1=1"
    elif status_filter in STATUS_FA:
        where = "o.status=%s"
    else:
        where = "1=1"

    user_clause = f"AND o.user_id={user_db_id}" if user_db_id else ""

    price_col = "o.total_price," if show_price else ""

    status_param = (status_filter,) if status_filter in STATUS_FA else ()

    cur.execute(f"SELECT COUNT(*) AS cnt FROM orders o WHERE {where} {user_clause}",
                status_param)
    
    total = cur.fetchone()["cnt"]

    cur.execute(f"""
        SELECT o.id, {price_col} o.fabric, o.delivery_date,
            o.status, o.created_at, u.name AS user_name, o.user_id
        FROM orders o JOIN users u ON u.id=o.user_id
        WHERE {where} {user_clause}
        ORDER BY o.created_at DESC
        LIMIT %s OFFSET %s
    """, status_param + (PAGE_SIZE, offset))
    rows = cur.fetchall(); cur.close(); conn.close()
    return rows, total

@db_safe
def db_get_order_full(order_id: int):
    """جزئیات کامل سفارش + آیتم‌ها + کامل تاریخچه‌ی تغییر وضعیت."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,)); order = cur.fetchone()
    if order:
        cur.execute("""
            SELECT oi.*, m.name AS model_name
            FROM order_items oi JOIN models m ON m.id=oi.model_id
            WHERE oi.order_id=%s
        """, (order_id,)); order["items"] = cur.fetchall()
        cur.execute("""
            SELECT h.*, a.name AS admin_name
            FROM order_status_history h
            LEFT JOIN admins a ON a.id=h.changed_by
            WHERE h.order_id=%s ORDER BY h.changed_at ASC
        """, (order_id,)); order["history"] = cur.fetchall()
    cur.close(); conn.close(); return order

@db_safe
def db_get_order_items_no_price(order_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.name AS model_name, oi.quantity
        FROM order_items oi JOIN models m ON m.id=oi.model_id
        WHERE oi.order_id=%s
    """, (order_id,))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_create_order(user_db_id, total_price, fabric, delivery_date, note, items) -> int:
    """
    سفارش ثبت می‌شه ولی به بدهی کاربر اضافه نمی‌شه.
    بدهی فقط وقتی اضافه می‌شه که سفارش به وضعیت 'ready' (آماده تحویل) برسه
    - چون قبل از اون مشخص نیست سفارش قطعاً انجام می‌شه یا نه (ممکنه رد/لغو بشه).
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders (user_id,total_price,fabric,delivery_date,note)
        VALUES(%s,%s,%s,%s,%s)
    """, (user_db_id, total_price, fabric, delivery_date, note))
    oid = cur.lastrowid
    for item in items:
        cur.execute("""
            INSERT INTO order_items (order_id,model_id,quantity,unit_price,line_total)
            VALUES(%s,%s,%s,%s,%s)
        """, (oid, item["model_id"], item["quantity"], item["unit_price"], item["line_total"]))
    # رکورد در user_finance ساخته بشه اگه نبود (مقدار صفر) تا بعداً قابل آپدیت باشه
    cur.execute("""
        INSERT INTO user_finance (user_id) VALUES(%s)
        ON DUPLICATE KEY UPDATE user_id=user_id
    """, (user_db_id,))
    conn.commit(); cur.close(); conn.close(); return oid

@db_safe
def db_update_order_status(order_id: int, status: str, reason: str = None, admin_db_id: int = None):
    """
    تغییر وضعیت سفارش + ثبت در تاریخچه (order_status_history).
    اگه وضعیت جدید 'ready' باشه و قبلاً بدهی اضافه نشده بود (debt_applied=FALSE)،
    مبلغ سفارش به current_month_debt اضافه می‌شه.
    """
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close(); conn.close(); return False

    old_status = order["status"]

    if reason:
        cur.execute("UPDATE orders SET status=%s,rejection_reason=%s WHERE id=%s",
                    (status, reason, order_id))
    else:
        cur.execute("UPDATE orders SET status=%s WHERE id=%s", (status, order_id))

    # ثبت تاریخچه
    cur.execute("""
        INSERT INTO order_status_history (order_id, old_status, new_status, changed_by, reason)
        VALUES(%s,%s,%s,%s,%s)
    """, (order_id, old_status, status, admin_db_id, reason))

    # اضافه کردن بدهی فقط یک‌بار، وقتی به ready می‌رسه
    if status == "ready" and not order.get("debt_applied"):
        amount = int(order["total_price"])
        cur.execute("""
            INSERT INTO user_finance (user_id,current_month_debt) VALUES(%s,%s)
            ON DUPLICATE KEY UPDATE current_month_debt=current_month_debt+%s
        """, (order["user_id"], amount, amount))
        cur.execute("UPDATE orders SET debt_applied=TRUE WHERE id=%s", (order_id,))
        cur.execute("""
            INSERT INTO transactions (user_id,type,amount,description)
            VALUES(%s,'order',%s,%s)
        """, (order["user_id"], amount, f"سفارش #{order_id} آماده تحویل - بدهی ثبت شد"))

    conn.commit(); cur.close(); conn.close(); return True

@db_safe
def db_cancel_order(order_id: int, user_db_id: int = None, admin_db_id: int = None):
    """
    لغو سفارش. اگه کاربر لغو می‌کنه user_db_id رو بده (فقط سفارش خودش، فقط pending).
    اگه ادمین لغو می‌کنه admin_db_id رو بده (هر وضعیتی، چون سوپرادمین مجازه).
    اگه بدهی این سفارش قبلاً اعمال شده بود (debt_applied=TRUE، یعنی به ready رسیده بود)
    برگشت داده می‌شه، وگرنه نیازی به برگشت نیست چون اصلاً اضافه نشده بود.
    """
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    if user_db_id:
        cur.execute("SELECT * FROM orders WHERE id=%s AND user_id=%s FOR UPDATE",
                    (order_id, user_db_id))
        order = cur.fetchone()
        if not order or order["status"] != "pending":
            cur.close(); conn.close(); return False
    else:
        cur.execute("SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,))
        order = cur.fetchone()
        if not order:
            cur.close(); conn.close(); return False

    target_user_id = order["user_id"]
    old_status = order["status"]

    cur.execute("UPDATE orders SET status='cancelled' WHERE id=%s", (order_id,))
    cur.execute("""
        INSERT INTO order_status_history (order_id, old_status, new_status, changed_by, reason)
        VALUES(%s,%s,'cancelled',%s,%s)
    """, (order_id, old_status, admin_db_id, texts['cancel_order_history']))

    if order.get("debt_applied"):
        amount = int(order["total_price"])
        cur.execute("""
            UPDATE user_finance SET current_month_debt=GREATEST(0,current_month_debt-%s)
            WHERE user_id=%s
        """, (amount, target_user_id))
        cur.execute("""
            INSERT INTO transactions (user_id,type,amount,description)
            VALUES(%s,'payment',%s,%s)
        """, (target_user_id, amount, f"لغو سفارش #{order_id} - برگشت وجه"))

    conn.commit(); cur.close(); conn.close(); return True

@db_safe
def db_update_order_items(order_id: int, fabric: str, delivery_date: str,
                           items: list, new_total: int, user_db_id: int):
    """
    ویرایش سفارش pending - آیتم‌ها + پارچه + تاریخ.
    چون ویرایش فقط روی سفارش‌های pending مجازه (و بدهی فقط در وضعیت ready اعمال می‌شه)
    اینجا نیازی به دستکاری بدهی نیست - فقط total_price آپدیت می‌شه.
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("DELETE FROM order_items WHERE order_id=%s", (order_id,))
    for item in items:
        cur.execute("""
            INSERT INTO order_items (order_id,model_id,quantity,unit_price,line_total)
            VALUES(%s,%s,%s,%s,%s)
        """, (order_id, item["model_id"], item["quantity"], item["unit_price"], item["line_total"]))
    cur.execute("""
        UPDATE orders SET fabric=%s,delivery_date=%s,total_price=%s,status='pending'
        WHERE id=%s
    """, (fabric, delivery_date, new_total, order_id))
    conn.commit(); cur.close(); conn.close()

# ── مالی ────────────────────────────────────────────────────────

@db_safe
def db_get_transactions_paged(user_db_id: int, offset: int) -> tuple[list, int]:
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE user_id=%s", (user_db_id,))
    total = cur.fetchone()["cnt"]
    cur.execute("""
        SELECT t.*, a.name AS admin_name FROM transactions t
        LEFT JOIN admins a ON a.id=t.created_by
        WHERE t.user_id=%s ORDER BY t.created_at DESC LIMIT %s OFFSET %s
    """, (user_db_id, PAGE_SIZE, offset))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows, total

@db_safe
def db_get_installments(user_db_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT i.*, a.name AS admin_name FROM installments i
        LEFT JOIN admins a ON a.id=i.created_by
        WHERE i.user_id=%s AND i.paid_amount<i.total_amount ORDER BY i.created_at DESC
    """, (user_db_id,))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_get_pending_payments(user_db_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM transactions WHERE user_id=%s AND type='payment' AND status='pending'
        ORDER BY created_at DESC
    """, (user_db_id,))
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_get_transaction_by_id(tx_id: int):
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM transactions WHERE id=%s", (tx_id,))
    row = cur.fetchone(); cur.close(); conn.close(); return row

@db_safe
def db_create_payment_request(user_db_id: int, amount: int, file_id: str,
                               installment_id: int = None, user_note: str = None) -> int:
    """
    درخواست پرداخت. اگه installment_id داده بشه یعنی این پرداخت برای یک قسط خاصه
    (پرداخت قسط با پرداخت عادی بدهی فرق می‌کنه: فقط previous_debt و paid_amount آپدیت می‌شه).
    user_note: یادداشت/توضیحی که خود کاربر هنگام ارسال پرداخت نوشته (اختیاری).
    """
    conn = get_connection(); cur = conn.cursor()
    desc = "درخواست پرداخت قسط - در انتظار تایید" if installment_id else "درخواست پرداخت در انتظار تایید"
    cur.execute("""
        INSERT INTO transactions (user_id,type,amount,description,status,receipt_file_id,installment_id,user_note)
        VALUES(%s,'payment',%s,%s,'pending',%s,%s,%s)
    """, (user_db_id, amount, desc, file_id, installment_id, user_note))
    tx_id = cur.lastrowid; conn.commit(); cur.close(); conn.close(); return tx_id

@db_safe
def db_approve_payment(tx_id: int, admin_db_id: int) -> dict:
    """
    تایید پرداخت.
    - اگه installment_id داره: فقط previous_debt و installments.paid_amount آپدیت می‌شه
      (current_month_debt دست نمی‌خوره).
    - اگه نداره: پرداخت عادیه - اول از current_month_debt، مازاد از previous_debt.
    """
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM transactions WHERE id=%s", (tx_id,)); tx = cur.fetchone()
    if not tx or tx["status"] != "pending":
        cur.close(); conn.close(); return None
    user_db_id = tx["user_id"]; amount = int(tx["amount"])

    if tx.get("installment_id"):
        # ── پرداخت قسط ──
        inst_id = tx["installment_id"]
        cur.execute("SELECT * FROM installments WHERE id=%s FOR UPDATE", (inst_id,))
        inst = cur.fetchone()
        cur.execute("SELECT * FROM user_finance WHERE user_id=%s FOR UPDATE", (user_db_id,))
        fin = cur.fetchone()
        prev = int(fin["previous_debt"]) if fin else 0
        new_paid = int(inst["paid_amount"]) + amount
        new_prev = max(0, prev - amount)
        cur.execute("UPDATE installments SET paid_amount=%s WHERE id=%s", (new_paid, inst_id))
        cur.execute("UPDATE user_finance SET previous_debt=%s WHERE user_id=%s", (new_prev, user_db_id))
        cur.execute("""
            UPDATE transactions SET status='approved',created_by=%s,description=%s WHERE id=%s
        """, (admin_db_id, f"پرداخت قسط تایید شد | مبلغ:{amount:,}", tx_id))
        conn.commit(); cur.close(); conn.close()
        return {"user_db_id": user_db_id, "amount": amount, "is_installment": True,
                "paid_current": 0, "paid_previous": amount,
                "new_cmd": int(fin["current_month_debt"]) if fin else 0,
                "new_prev": new_prev, "installment_id": inst_id,
                "installment_remaining": int(inst["total_amount"]) - new_paid}

    # ── پرداخت عادی ──
    cur.execute("SELECT * FROM user_finance WHERE user_id=%s FOR UPDATE", (user_db_id,))
    fin = cur.fetchone()
    cmd  = int(fin["current_month_debt"]) if fin else 0
    prev = int(fin["previous_debt"])      if fin else 0
    paid_current  = min(amount, cmd)
    paid_previous = min(amount - paid_current, prev)
    cur.execute("""
        UPDATE user_finance SET current_month_debt=%s,previous_debt=%s WHERE user_id=%s
    """, (cmd - paid_current, prev - paid_previous, user_db_id))
    cur.execute("""
        UPDATE transactions SET status='approved',created_by=%s,
        description=%s WHERE id=%s
    """, (admin_db_id, f"تایید شد | ماه جاری:{paid_current:,} | قبلی:{paid_previous:,}", tx_id))
    conn.commit(); cur.close(); conn.close()
    return {"user_db_id": user_db_id, "amount": amount, "is_installment": False,
            "paid_current": paid_current, "paid_previous": paid_previous,
            "new_cmd": cmd-paid_current, "new_prev": prev-paid_previous}

@db_safe
def db_reject_payment(tx_id: int, admin_db_id: int, reason: str):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        UPDATE transactions SET status='rejected',created_by=%s,description=%s
        WHERE id=%s AND status='pending'
    """, (admin_db_id, f"رد شد: {reason}", tx_id))
    conn.commit(); cur.close(); conn.close()

@db_safe
def db_add_manual_debt(user_db_id: int, amount: int, desc: str, admin_db_id: int):
    conn = get_connection(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_finance (user_id,current_month_debt) VALUES(%s,%s)
        ON DUPLICATE KEY UPDATE current_month_debt=current_month_debt+%s
    """, (user_db_id, amount, amount))
    cur.execute("""
        INSERT INTO transactions (user_id,type,amount,description,created_by)
        VALUES(%s,'debt_added',%s,%s,%s)
    """, (user_db_id, amount, desc, admin_db_id))
    conn.commit(); cur.close(); conn.close()

@db_safe
def db_transfer_debt_to_previous(user_db_id: int, admin_db_id: int) -> int:
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM user_finance WHERE user_id=%s FOR UPDATE", (user_db_id,))
    fin = cur.fetchone()
    if not fin: cur.close(); conn.close(); return 0
    amount = int(fin["current_month_debt"])
    if amount <= 0: cur.close(); conn.close(); return 0
    cur.execute("""
        UPDATE user_finance SET previous_debt=previous_debt+%s,current_month_debt=0
        WHERE user_id=%s
    """, (amount, user_db_id))
    cur.execute("UPDATE users SET is_banned=FALSE,ban_reason=NULL WHERE id=%s", (user_db_id,))
    cur.execute("""
        INSERT INTO transactions (user_id,type,amount,description,created_by)
        VALUES(%s,'month_transfer',%s,%s,%s)
    """, (user_db_id, amount, texts["debt_transfer_desc"], admin_db_id))
    conn.commit(); cur.close(); conn.close(); return amount

@db_safe
def db_get_active_installments_sum(user_db_id: int) -> int:
    """مجموع مونده‌ی اقساط فعال (بخشی از previous_debt که قسط‌بندی شده و هنوز کامل پرداخت نشده)."""
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT COALESCE(SUM(total_amount - paid_amount),0) AS s
        FROM installments WHERE user_id=%s AND paid_amount < total_amount
    """, (user_db_id,))
    row = cur.fetchone(); cur.close(); conn.close()
    return int(row["s"])

@db_safe
def db_add_installment(user_db_id: int, total: int, count: int,
                        due_dates: list, admin_db_id: int):
    """
    قسط‌بندی جدید. مبلغ قسط‌بندی نمی‌تونه از (previous_debt - مجموع اقساط فعال قبلی) بیشتر باشه
    چون نباید بیشتر از بدهی قبلیِ "آزاد" (قسط‌بندی نشده) رو قسط بست.
    برمی‌گردونه: (installment_id, None) موفق یا (None, error_message) ناموفق.
    """
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT previous_debt FROM user_finance WHERE user_id=%s FOR UPDATE", (user_db_id,))
    fin = cur.fetchone()
    prev_debt = int(fin["previous_debt"]) if fin else 0

    cur.execute("""
        SELECT COALESCE(SUM(total_amount - paid_amount),0) AS s
        FROM installments WHERE user_id=%s AND paid_amount < total_amount
    """, (user_db_id,))
    already_installed = int(cur.fetchone()["s"])

    available = prev_debt - already_installed
    if total > available:
        cur.close(); conn.close()
        return None, available

    per = total // count
    first = per + (total - per * count)
    cur.execute("""
        INSERT INTO installments (user_id,total_amount,num_installments,per_installment,first_installment,due_dates,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s)
    """, (user_db_id, total, count, per, first, json.dumps(due_dates), admin_db_id))
    iid = cur.lastrowid; conn.commit(); cur.close(); conn.close()
    return iid, None

# ── گزارش scheduler ──────────────────────────────────────────────

@db_safe
def db_get_users_with_current_debt():
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT u.id,u.cid,u.name,uf.current_month_debt
        FROM users u JOIN user_finance uf ON uf.user_id=u.id
        WHERE uf.current_month_debt>0 AND u.is_banned=FALSE
    """)
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

# ════════════════════════════════════════════════════════════════
#  STATE MANAGEMENT
# ════════════════════════════════════════════════════════════════

user_states: dict = {}
_state_lock = threading.Lock()

# سفارش
S_SEL_MODEL    = "sel_model"
S_ENTER_QTY    = "enter_qty"
S_ADD_MORE     = "add_more"
S_ENTER_FABRIC = "enter_fabric"
S_ENTER_DATE   = "enter_date"
S_ENTER_NOTE   = "enter_note"
S_CONFIRM      = "confirm"
# ویرایش سفارش
S_EDIT_SEL_MODEL  = "edit_sel_model"
S_EDIT_ENTER_QTY  = "edit_enter_qty"
S_EDIT_ADD_MORE   = "edit_add_more"
S_EDIT_FABRIC     = "edit_fabric"
S_EDIT_DATE       = "edit_date"
S_EDIT_CONFIRM    = "edit_confirm"
# پرداخت
S_PAY_AMOUNT  = "pay_amount"
S_PAY_RECEIPT = "pay_receipt"
S_PAY_NOTE    = "pay_note"
S_PAY_CONFIRM = "pay_confirm"
# سوپرادمین
S_SA_ADD_USER_CID  = "sa_u_cid"
S_SA_ADD_USER_NAME = "sa_u_name"
S_SA_ADD_USER_UN   = "sa_u_un"
S_SA_ADD_ADM_CID   = "sa_a_cid"
S_SA_ADD_ADM_NAME  = "sa_a_name"
S_SA_ADD_ADM_ROLE  = "sa_a_role"
S_SA_MDL_NAME      = "sa_m_name"
S_SA_MDL_PRICE     = "sa_m_price"
S_SA_SET_PRICE     = "sa_set_price"
S_SA_DEBT_AMOUNT   = "sa_debt_amt"
S_SA_DEBT_DESC     = "sa_debt_desc"
S_SA_INST_AMOUNT   = "sa_inst_amt"
S_SA_INST_COUNT    = "sa_inst_cnt"
S_SA_INST_DATES    = "sa_inst_dates"
S_SA_BROADCAST     = "sa_broadcast"


def get_state(cid: int) -> dict:
    with _state_lock:
        return dict(user_states.get(cid, {}))

def set_state(cid: int, step: str, **kw):
    with _state_lock:
        if cid not in user_states:
            user_states[cid] = {"step": step, "data": {}}
        user_states[cid]["step"] = step
        user_states[cid]["data"].update(kw)

def clear_state(cid: int):
    with _state_lock:
        user_states.pop(cid, None)

# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════

def _is_admin(cid): return db_get_admin(cid) is not None
def _is_superadmin(cid):
    a = db_get_admin(cid); return a is not None and a["role"] == "superadmin"
def _admin_db_id(cid):
    a = db_get_admin(cid); return a["id"] if a else None

def _format_cart(items: list) -> str:
    lines = [texts["cart_header"]]; total = 0
    for i, it in enumerate(items, 1):
        total += it["line_total"]
        lines.append(f"{i}. {it['model_name']}  ×{it['quantity']}  =  {it['line_total']:,}")
    lines.append(f"\n💵 جمع: {total:,} تومان")
    return "\n".join(lines)

def _format_order_summary(items, fabric, delivery_date, note, show_price=True) -> str:
    lines = [texts["order_summary_header"]]; total = 0
    for i, it in enumerate(items, 1):
        lt = it["quantity"] * it["unit_price"]
        total += lt
        if show_price:
            lines.append(f"{i}. {it['model_name']}  ×{it['quantity']}"
                         f"  |  {it['unit_price']:,}/عدد  =  {lt:,}")
        else:
            lines.append(f"{i}. {it['model_name']}  ×{it['quantity']}")
    if show_price:
        lines.append(f"\n💵 جمع کل: {total:,} تومان")
    lines.append(f"🪡 پارچه: {fabric}")
    lines.append(f"📅 تاریخ تحویل: {to_jalali(delivery_date) if hasattr(delivery_date, 'year') else to_jalali(_parse_date_str(delivery_date)) if delivery_date else '—'}")
    if note: lines.append(f"📝 یادداشت: {note}")
    return "\n".join(lines)

def _notify_admins(text: str, kb=None):
    for ac in ADMIN_IDS:
        try: bot.send_message(ac, text, reply_markup=kb)
        except Exception as e: print(f"[WARN] Failed to notify admin {ac}: {e}")

def _paginate_kb(offset: int, total: int, cb_prev: str, cb_next: str) -> list:
    """دکمه‌های صفحه‌بندی - فقط اگه لازم باشه."""
    btns = []
    if offset > 0:
        btns.append(InlineKeyboardButton("◀️ قبلی", callback_data=cb_prev))
    if offset + PAGE_SIZE < total:
        remaining = total - offset - PAGE_SIZE
        show = min(remaining, PAGE_SIZE)
        btns.append(InlineKeyboardButton(f"بعدی {show}تا ▶️", callback_data=cb_next))
    return btns

# ════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ════════════════════════════════════════════════════════════════

def main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📦 ثبت سفارش"), KeyboardButton("💰 بخش مالی"))
    kb.add(KeyboardButton("📋 سفارش‌های من"), KeyboardButton("👤 پروفایل من"))
    return kb

def admin_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton("📋 سفارشات"))
    return kb

def superadmin_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("👥 کاربران"),    KeyboardButton("📦 مدل‌ها"))
    kb.add(KeyboardButton("📋 سفارشات"),   KeyboardButton("📊 گزارش مالی"))
    kb.add(KeyboardButton("👮 ادمین‌ها"),   KeyboardButton("📣 ارسال پیام همگانی"))
    return kb

def models_kb(models, selected_ids):
    kb = InlineKeyboardMarkup(row_width=2); btns = []
    for m in models:
        lbl = f"{'✅ ' if m['id'] in selected_ids else ''}{m['name']} ({m['price']:,})"
        btns.append(InlineKeyboardButton(lbl, callback_data=f"mdl_{m['id']}"))
    kb.add(*btns)
    kb.add(InlineKeyboardButton("✅ انتخاب مدل‌ها تمومه", callback_data="mdl_done"))
    return kb

def qty_kb():
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(InlineKeyboardButton("2",  callback_data="qty_2"),
           InlineKeyboardButton("4",  callback_data="qty_4"),
           InlineKeyboardButton("6", callback_data="qty_6"),
           InlineKeyboardButton("8", callback_data="qty_8"),
           InlineKeyboardButton("10", callback_data="qty_10"),
           InlineKeyboardButton("✏️ دستی", callback_data="qty_manual"))
    return kb

def date_kb():
    """دکمه‌های پیشنهاد تاریخ تحویل - نمایش شمسی، ذخیره میلادی در callback."""
    kb = InlineKeyboardMarkup(row_width=1)
    today_g = datetime.now()
    for d in [2, 3, 4, 5, 6, 7]:
        dt_g = today_g + timedelta(days=d)
        dt_j = jdatetime.date.fromgregorian(date=dt_g.date())
        jalali_display  = dt_j.strftime("%Y/%m/%d")
        gregorian_value = dt_g.strftime("%Y-%m-%d")
        kb.add(InlineKeyboardButton(f"{jalali_display}  ({d} روز دیگه)",
                                    callback_data=f"date_{gregorian_value}"))
    kb.add(InlineKeyboardButton("✏️ تاریخ دلخواه (شمسی)", callback_data="date_manual"))
    return kb

def order_confirm_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("✅ ارسال سفارش", callback_data="ord_confirm"),
           InlineKeyboardButton("❌ انصراف",      callback_data="ord_cancel"))
    return kb

def order_filter_kb(prefix: str = "of"):
    """فیلتر وضعیت سفارشات."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🔄 فعال",          callback_data=f"{prefix}_active_0"),
        InlineKeyboardButton("📜 نمایش همه",     callback_data=f"{prefix}_all_0"),
        InlineKeyboardButton("⏳ در انتظار",     callback_data=f"{prefix}_pending_0"),
        InlineKeyboardButton("✅ تایید شده",     callback_data=f"{prefix}_approved_0"),
        InlineKeyboardButton("🔧 در تولید",      callback_data=f"{prefix}_producing_0"),
        InlineKeyboardButton("📦 آماده تحویل",  callback_data=f"{prefix}_ready_0"),
        InlineKeyboardButton("🚚 تحویل شده",    callback_data=f"{prefix}_delivered_0"),
        InlineKeyboardButton("❌ رد شده",        callback_data=f"{prefix}_rejected_0"),
        InlineKeyboardButton("🚫 لغو شده",      callback_data=f"{prefix}_cancelled_0"),
    )
    return kb

# ترتیب منطقی مراحل سفارش - برای دکمه‌ی "مرحله بعد"
ORDER_STAGE_FLOW = ["pending", "approved", "producing", "ready", "delivered"]
ORDER_STAGE_NEXT = {
    ORDER_STAGE_FLOW[i]: ORDER_STAGE_FLOW[i+1] for i in range(len(ORDER_STAGE_FLOW)-1)
}

def order_status_change_kb(order_id: int, current_status: str, is_super: bool = False):
    """
    فقط دکمه‌ی مرحله‌ی بعدی منطقی (طبق ORDER_STAGE_FLOW) رو نشون می‌ده،
    به‌علاوه‌ی دکمه‌ی رد/لغو در مراحل مناسب.
    - مرحله‌ی بعد: فقط اگه وضعیت فعلی در جریان عادی باشه (نه delivered/rejected/cancelled)
    - رد سفارش: فقط در pending
    - لغو سفارش: در pending و approved (طبق درخواست) + هر وضعیتی برای سوپرادمین
    """
    kb = InlineKeyboardMarkup(row_width=2)

    next_status = ORDER_STAGE_NEXT.get(current_status)
    if next_status:
        kb.add(InlineKeyboardButton(f"➡️ {STATUS_FA[next_status]}",
                                    callback_data=f"adm_st_{order_id}_{next_status}"))

    if current_status == "pending":
        kb.add(InlineKeyboardButton("❌ رد سفارش", callback_data=f"adm_st_{order_id}_rejected"))

    if current_status in ("pending", "approved"):
        kb.add(InlineKeyboardButton("🚫 لغو سفارش", callback_data=f"adm_st_{order_id}_cancelled"))
    elif is_super and current_status not in ("delivered", "cancelled"):
        # سوپرادمین می‌تونه در هر مرحله‌ی دیگه‌ای هم لغو کنه
        kb.add(InlineKeyboardButton("🚫 لغو سفارش", callback_data=f"adm_st_{order_id}_cancelled"))

    return kb

def admin_order_kb(order_id: int):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("✅ تایید", callback_data=f"admin_approve_{order_id}"),
           InlineKeyboardButton("❌ رد",    callback_data=f"admin_reject_{order_id}"))
    return kb

def admin_payment_kb(tx_id: int):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("✅ تایید پرداخت", callback_data=f"pay_ok_{tx_id}"),
           InlineKeyboardButton("❌ رد پرداخت",    callback_data=f"pay_no_{tx_id}"))
    return kb

def user_order_actions_kb(order_id: int, status: str):
    """دکمه‌های زیر جزئیات سفارش کاربر - فقط اگه pending باشه ویرایش/لغو داره."""
    if status != "pending":
        return None
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("✏️ ویرایش", callback_data=f"uord_edit_{order_id}"),
           InlineKeyboardButton("🚫 لغو",     callback_data=f"uord_cancel_{order_id}"))
    return kb

def sa_users_kb(users):
    kb = InlineKeyboardMarkup(row_width=1)
    for u in users:
        debt = int(u["current_month_debt"]) + int(u["previous_debt"])
        flag = "🚫" if u["is_banned"] else "✅"
        kb.add(InlineKeyboardButton(f"{flag} {u['name']}  |  {debt:,} ت",
                                    callback_data=f"sa_u_{u['id']}"))
    kb.add(InlineKeyboardButton("➕ افزودن کاربر", callback_data="sa_add_user"))
    return kb

def sa_user_actions_kb(uid: int, is_banned: bool):
    kb = InlineKeyboardMarkup(row_width=2)
    ban_lbl = "✅ رفع مسدودی" if is_banned else texts["status_banned"]
    ban_cb  = f"sa_unban_{uid}" if is_banned else f"sa_ban_{uid}"
    kb.add(InlineKeyboardButton("📦 سفارشات",            callback_data=f"sa_uord_{uid}"),
           InlineKeyboardButton("💳 تراکنش‌ها/پرداخت‌ها",  callback_data=f"sa_utx_{uid}"))
    kb.add(InlineKeyboardButton("📋 لیست اقساط",          callback_data=f"sa_uinst_{uid}"))
    kb.add(InlineKeyboardButton(ban_lbl,                   callback_data=ban_cb),
           InlineKeyboardButton("🔄 انتقال بدهی",          callback_data=f"sa_transfer_{uid}"))
    kb.add(InlineKeyboardButton("➕ بدهی دستی",            callback_data=f"sa_adddebt_{uid}"),
           InlineKeyboardButton("📋 قسط‌بندی جدید",        callback_data=f"sa_inst_{uid}"))
    kb.add(InlineKeyboardButton("🗑 غیرفعال کردن",         callback_data=f"sa_deact_user_{uid}"),
           InlineKeyboardButton("🗑🔴 حذف کامل",           callback_data=f"sa_del_user_{uid}"))
    kb.add(InlineKeyboardButton("🔙 لیست کاربران",         callback_data="sa_users_list"))
    return kb

def sa_models_kb(models):
    kb = InlineKeyboardMarkup(row_width=1)
    for m in models:
        st  = "✅" if m["is_active"] else "❌"
        kb.add(InlineKeyboardButton(f"{st} {m['name']}  |  {int(m['price']):,}",
                                    callback_data=f"sa_mdl_{m['id']}"))
    kb.add(InlineKeyboardButton("➕ افزودن مدل", callback_data="sa_add_model"))
    return kb

def sa_model_actions_kb(mid: int, is_active: bool):
    kb = InlineKeyboardMarkup(row_width=2)
    tog = "❌ غیرفعال" if is_active else texts["status_active"]
    kb.add(InlineKeyboardButton(tog,                     callback_data=f"sa_mdl_tog_{mid}"),
           InlineKeyboardButton("💲 تغییر قیمت",         callback_data=f"sa_mdl_price_{mid}"))
    kb.add(InlineKeyboardButton("🗑 غیرفعال کردن",       callback_data=f"sa_mdl_deact_{mid}"),
           InlineKeyboardButton("🗑🔴 حذف کامل",         callback_data=f"sa_mdl_del_{mid}"))
    kb.add(InlineKeyboardButton("🔙 لیست مدل‌ها",        callback_data="sa_models_list"))
    return kb

def finance_main_kb():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("💳 ثبت پرداخت جدید",     callback_data="fin_new_pay"),
           InlineKeyboardButton("📜 تاریخچه تراکنش‌ها",   callback_data="fin_hist_0"),
           InlineKeyboardButton("📋 اقساط من",             callback_data="fin_inst"),
           InlineKeyboardButton("⏳ پرداخت‌های در انتظار", callback_data="fin_pending"))
    return kb

def pay_confirm_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("✅ ارسال",  callback_data="pay_submit"),
           InlineKeyboardButton("❌ انصراف", callback_data="pay_cancel"))
    return kb

# ════════════════════════════════════════════════════════════════
#  /start - نقش‌بندی
# ════════════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
@safe_handler
def cmd_start(message: Message):
    cid = message.chat.id; clear_state(cid)
    logger.info("/start | cid=%s | user=%s", cid, message.from_user.username or message.from_user.first_name)
    admin = db_get_admin(cid)
    if admin:
        if admin["role"] == "superadmin":
            bot.send_message(cid, f"سلام {admin['name']} 👋\n" + texts['superadmin_welcome'],
                             reply_markup=superadmin_menu_kb())
        else:
            bot.send_message(cid, f"سلام {admin['name']} 👋\n" + texts['admin_welcome'],
                             reply_markup=admin_menu_kb())
        return
    user = db_get_user(cid)
    if not user:
        bot.send_message(cid, texts["no_access"]); return
    bot.send_message(cid, f"سلام {message.from_user.first_name} 👋",
                     reply_markup=main_menu_kb())

# ════════════════════════════════════════════════════════════════
#  پروفایل کاربر
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "👤 پروفایل من")
@safe_handler
def btn_profile(message: Message):
    cid = message.chat.id; user = db_get_user(cid)
    if not user: bot.send_message(cid, texts["no_access_short"]); return
    fin = db_get_user_finance(user["id"])
    cmd = int(fin["current_month_debt"]) if fin else 0
    prv = int(fin["previous_debt"])      if fin else 0
    bot.send_message(cid,
        f"👤 {user['name']}\n"
        f"وضعیت: {'🚫 مسدود' if user['is_banned'] else '✅ فعال'}\n\n"
        f"📅 بدهی ماه جاری: {cmd:,} تومان\n"
        f"💳 بدهی قبلی:     {prv:,} تومان",
        reply_markup=main_menu_kb())

# ════════════════════════════════════════════════════════════════
#  سفارشات کاربر
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📋 سفارش‌های من")
@safe_handler
def btn_my_orders(message: Message):
    cid = message.chat.id; user = db_get_user(cid)
    if not user: bot.send_message(cid, texts["no_access_short"]); return
    bot.send_message(cid, "📋 سفارش‌های من\nفیلتر انتخاب کن:",
                     reply_markup=order_filter_kb("uo"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("uo_") and c.data != "uo_back_filters")
@safe_handler
def cb_user_orders(call: CallbackQuery):
    cid = call.message.chat.id
    user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id, "⛔️"); return
    parts  = call.data.split("_")
    status = parts[1]; offset = int(parts[2])
    orders, total = db_get_orders_paged(status, offset, user_db_id=user["id"], show_price=True)
    bot.answer_callback_query(call.id)

    if not orders:
        bot.send_message(cid, f"هیچ سفارشی در وضعیت «{STATUS_FA.get(status,status)}» ندارید.")
        return

    kb = InlineKeyboardMarkup(row_width=1)
    for o in orders:
        st_e = STATUS_EMOJI.get(o["status"], "•")
        lbl  = f"{st_e} #{o['id']}  {to_jalali(o['created_at'], '%m/%d')}"
        kb.add(InlineKeyboardButton(lbl, callback_data=f"uord_detail_{o['id']}"))

    pag = _paginate_kb(offset, total, f"uo_{status}_{offset-PAGE_SIZE}",
                       f"uo_{status}_{offset+PAGE_SIZE}")
    if pag: kb.add(*pag)
    kb.add(InlineKeyboardButton("🔙 فیلترها", callback_data="uo_back_filters"))

    showing = min(offset + PAGE_SIZE, total)
    bot.send_message(cid,
        f"📋 {STATUS_FA.get(status,'سفارشات')}  ({showing}/{total})",
        reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "uo_back_filters")
@safe_handler
def cb_uo_back(call: CallbackQuery):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📋 فیلتر انتخاب کن:",
                     reply_markup=order_filter_kb("uo"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("uord_detail_"))
@safe_handler
def cb_user_order_detail(call: CallbackQuery):
    cid = call.message.chat.id; user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id, "⛔️"); return
    order_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    order = db_get_order_full(order_id)
    if not order or order["user_id"] != user["id"]:
        bot.send_message(cid, texts["order_not_found"]); return

    summary = _format_order_summary(order["items"], order["fabric"],
                                    order["delivery_date"], order.get("note",""))
    text = (f"📦 سفارش #{order_id}\n"
            f"وضعیت: {STATUS_FA.get(order['status'],'—')}\n"
            f"تاریخ ثبت: {to_jalali_full(order['created_at'])}\n\n"
            f"{summary}")
    if order.get("rejection_reason"):
        text += f"\n\n❌ دلیل رد: {order['rejection_reason']}"
    if order.get("history"):
        text += "\n\n📜 تاریخچه‌ی وضعیت:"
        for h in order["history"]:
            ts = to_jalali_full(h["changed_at"])
            text += f"\n  • {STATUS_FA.get(h['new_status'], h['new_status'])}  |  {ts}"

    bot.send_message(cid, text, reply_markup=user_order_actions_kb(order_id, order["status"]))

@bot.callback_query_handler(func=lambda c: c.data.startswith("uord_cancel_"))
@safe_handler
def cb_user_cancel_order(call: CallbackQuery):
    cid = call.message.chat.id; user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id, "⛔️"); return
    order_id = int(call.data.split("_")[2])
    ok = db_cancel_order(order_id, user["id"])
    if ok:
        logger.info("Order cancelled by user | order_id=%s | user=%s", order_id, user["name"])
        bot.answer_callback_query(call.id, "✅ سفارش لغو شد.")
        bot.send_message(cid, f"🚫 سفارش #{order_id} لغو شد و مبلغ آن از بدهی شما کم شد.")
        _notify_admins(f"🚫 سفارش #{order_id} توسط کاربر {user['name']} لغو شد.")
    else:
        bot.answer_callback_query(call.id, texts["order_cancel_error"])

# ── ویرایش سفارش کاربر ───────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("uord_edit_"))
@safe_handler
def cb_user_edit_order(call: CallbackQuery):
    cid = call.message.chat.id; user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id, "⛔️"); return
    order_id = int(call.data.split("_")[2])
    order = db_get_order_full(order_id)
    if not order or order["user_id"] != user["id"] or order["status"] != "pending":
        bot.answer_callback_query(call.id, texts["order_not_editable"]); return
    bot.answer_callback_query(call.id)
    models = db_get_active_models()
    # items قبلی رو به state بریز
    prev_items = [{"model_id": i["model_id"], "model_name": i["model_name"],
                   "quantity": i["quantity"], "unit_price": int(i["unit_price"]),
                   "line_total": int(i["line_total"])} for i in order["items"]]
    set_state(cid, S_EDIT_SEL_MODEL, edit_order_id=order_id, items=prev_items,
              models=models, current_model_id=None)
    bot.send_message(cid,
        f"✏️ ویرایش سفارش #{order_id}\n\n"
        f"سبد فعلی:\n{_format_cart(prev_items)}\n\n"
        "مدل‌ها رو دوباره انتخاب کن یا «تمومه» بزن تا همینا بمونن:",
        reply_markup=models_kb(models, [i["model_id"] for i in prev_items]))

@bot.callback_query_handler(func=lambda c: c.data.startswith("mdl_") and
    get_state(c.message.chat.id).get("step") in (S_EDIT_SEL_MODEL, S_EDIT_ADD_MORE))
@safe_handler
def cb_edit_model(call: CallbackQuery):
    cid = call.message.chat.id
    if call.data == "mdl_done":
        _edit_proceed_fabric(cid); bot.answer_callback_query(call.id); return
    model_id = int(call.data.split("_")[1])
    model = db_get_model_by_id(model_id)
    if not model: bot.answer_callback_query(call.id, texts["model_not_found"]); return
    set_state(cid, S_EDIT_ENTER_QTY, current_model_id=model_id)
    bot.answer_callback_query(call.id)
    bot.send_message(cid, f"✅ {model['name']} ({model['price']:,})\nتعداد:", reply_markup=qty_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("qty_") and
    get_state(c.message.chat.id).get("step") == S_EDIT_ENTER_QTY)
@safe_handler
def cb_edit_qty(call: CallbackQuery):
    cid = call.message.chat.id
    if call.data == "qty_manual":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, "✏️ تعداد:")
        bot.register_next_step_handler(msg, _edit_recv_manual_qty); return
    qty = int(call.data.split("_")[1]); bot.answer_callback_query(call.id)
    _edit_add_item(cid, qty)

@safe_handler
def _edit_recv_manual_qty(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_EDIT_ENTER_QTY: return
    try:
        qty = int(message.text.strip())
        if qty <= 0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid, texts["positive_number"]); bot.register_next_step_handler(msg, _edit_recv_manual_qty); return
    _edit_add_item(cid, qty)

def _edit_add_item(cid: int, qty: int):
    state = get_state(cid); mid = state["data"]["current_model_id"]
    model = db_get_model_by_id(mid); items = state["data"]["items"]
    existing = next((i for i in items if i["model_id"] == mid), None)
    if existing:
        existing["quantity"] += qty; existing["line_total"] = existing["quantity"] * existing["unit_price"]
    else:
        items.append({"model_id": mid, "model_name": model["name"], "quantity": qty,
                      "unit_price": int(model["price"]), "line_total": qty * int(model["price"])})
    set_state(cid, S_EDIT_ADD_MORE, items=items, current_model_id=None)
    models = state["data"]["models"]
    bot.send_message(cid, f"✅ اضافه شد!\n\n{_format_cart(items)}\n\nمدل دیگه یا «تمومه»:",
                     reply_markup=models_kb(models, [i["model_id"] for i in items]))

def _edit_proceed_fabric(cid: int):
    items = get_state(cid)["data"]["items"]
    if not items:
        bot.send_message(cid, texts["select_at_least_one"]); return
    set_state(cid, S_EDIT_FABRIC)
    msg = bot.send_message(cid, f"{_format_cart(items)}\n\n🪡 نوع پارچه:")
    bot.register_next_step_handler(msg, _edit_recv_fabric)

@safe_handler
def _edit_recv_fabric(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_EDIT_FABRIC: return
    set_state(cid, S_EDIT_DATE, fabric=message.text.strip())
    bot.send_message(cid, "📅 تاریخ تحویل:", reply_markup=date_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("date_") and
    get_state(c.message.chat.id).get("step") == S_EDIT_DATE)
@safe_handler
def cb_edit_date(call: CallbackQuery):
    cid = call.message.chat.id
    if call.data == "date_manual":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, "✏️ تاریخ (YYYY-MM-DD):")
        bot.register_next_step_handler(msg, _edit_recv_manual_date); return
    bot.answer_callback_query(call.id)
    _edit_show_confirm(cid, call.data[5:])

@safe_handler
def _edit_recv_manual_date(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_EDIT_DATE: return
    if not validate_jalali(message.text.strip()):
        msg = bot.send_message(cid, texts["invalid_date"])
        bot.register_next_step_handler(msg, _edit_recv_manual_date); return
    _edit_show_confirm(cid, jalali_to_gregorian(message.text.strip()))

def _edit_show_confirm(cid: int, delivery_date: str):
    set_state(cid, S_EDIT_CONFIRM, delivery_date=delivery_date)
    d = get_state(cid)["data"]
    new_total = sum(i["line_total"] for i in d["items"])
    summary = _format_order_summary(d["items"], d["fabric"], delivery_date, "")
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("✅ ذخیره ویرایش", callback_data="edit_confirm"),
           InlineKeyboardButton("❌ انصراف",        callback_data="edit_cancel"))
    bot.send_message(cid, f"📝 تغییرات:\n\n{summary}\n\n💵 جمع جدید: {new_total:,}", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "edit_confirm")
@safe_handler
def cb_edit_confirm(call: CallbackQuery):
    cid = call.message.chat.id; user = db_get_user(cid)
    state = get_state(cid)
    if state.get("step") != S_EDIT_CONFIRM: bot.answer_callback_query(call.id,"⚠️"); return
    d = state["data"]; new_total = sum(i["line_total"] for i in d["items"])
    db_update_order_items(d["edit_order_id"], d["fabric"], d["delivery_date"],
                          d["items"], new_total, user["id"])
    logger.info("Order edited | order_id=%s | user=%s | new_total=%s", d["edit_order_id"], user["name"] if user else cid, new_total)
    clear_state(cid); bot.answer_callback_query(call.id, "✅ ذخیره شد.")
    bot.send_message(cid, f"✅ سفارش #{d['edit_order_id']} ویرایش شد و منتظر تایید مجدد ادمین است.",
                     reply_markup=main_menu_kb())
    _notify_admins(f"✏️ سفارش #{d['edit_order_id']} ویرایش شد.", admin_order_kb(d["edit_order_id"]))

@bot.callback_query_handler(func=lambda c: c.data == "edit_cancel")
@safe_handler
def cb_edit_cancel(call: CallbackQuery):
    clear_state(call.message.chat.id); bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, texts["edit_cancelled_msg"], reply_markup=main_menu_kb())

# ════════════════════════════════════════════════════════════════
#  ثبت سفارش جدید
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📦 ثبت سفارش")
@safe_handler
def btn_new_order(message: Message):
    cid = message.chat.id; user = db_get_user(cid)
    if not user: bot.send_message(cid, texts["no_access_short"]); return
    if user["is_banned"]:
        bot.send_message(cid, texts["account_banned_msg_user"]); return
    models = db_get_active_models()
    if not models: bot.send_message(cid, texts["no_active_models"]); return
    set_state(cid, S_SEL_MODEL, items=[], models=models, current_model_id=None)
    bot.send_message(cid,
        "📦 ثبت سفارش جدید\nمدل‌ها رو انتخاب کن:",
        reply_markup=models_kb(models, []))

@bot.callback_query_handler(func=lambda c: c.data.startswith("mdl_") and
    get_state(c.message.chat.id).get("step") in (S_SEL_MODEL, S_ADD_MORE))
@safe_handler
def cb_model(call: CallbackQuery):
    cid = call.message.chat.id
    if call.data == "mdl_done":
        items = get_state(cid)["data"].get("items", [])
        if not items: bot.answer_callback_query(call.id, "⚠️ حداقل یک مدل انتخاب کن!"); return
        bot.answer_callback_query(call.id)
        set_state(cid, S_ENTER_FABRIC)
        msg = bot.send_message(cid, f"{_format_cart(items)}\n\n🪡 نوع پارچه:")
        bot.register_next_step_handler(msg, _recv_fabric); return
    model_id = int(call.data.split("_")[1]); model = db_get_model_by_id(model_id)
    if not model: bot.answer_callback_query(call.id, "❌"); return
    set_state(cid, S_ENTER_QTY, current_model_id=model_id)
    bot.answer_callback_query(call.id)
    bot.send_message(cid, f"✅ {model['name']} ({model['price']:,})\nتعداد:", reply_markup=qty_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("qty_") and
    get_state(c.message.chat.id).get("step") == S_ENTER_QTY)
@safe_handler
def cb_qty(call: CallbackQuery):
    cid = call.message.chat.id
    if call.data == "qty_manual":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, "✏️ تعداد:")
        bot.register_next_step_handler(msg, _recv_manual_qty); return
    bot.answer_callback_query(call.id)
    _add_to_cart(cid, int(call.data.split("_")[1]))

@safe_handler
def _recv_manual_qty(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_ENTER_QTY: return
    try:
        qty = int(message.text.strip())
        if qty <= 0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid, texts["positive_number"]); bot.register_next_step_handler(msg, _recv_manual_qty); return
    _add_to_cart(cid, qty)

def _add_to_cart(cid: int, qty: int):
    state = get_state(cid); mid = state["data"]["current_model_id"]
    model = db_get_model_by_id(mid); items = state["data"]["items"]
    ex = next((i for i in items if i["model_id"] == mid), None)
    if ex:
        ex["quantity"] += qty; ex["line_total"] = ex["quantity"] * ex["unit_price"]
    else:
        items.append({"model_id": mid, "model_name": model["name"], "quantity": qty,
                      "unit_price": int(model["price"]), "line_total": qty * int(model["price"])})
    set_state(cid, S_ADD_MORE, items=items, current_model_id=None)
    models = state["data"]["models"]
    bot.send_message(cid, f"✅ اضافه شد!\n\n{_format_cart(items)}\n\nمدل دیگه یا «تمومه»:",
                     reply_markup=models_kb(models, [i["model_id"] for i in items]))

@safe_handler
def _recv_fabric(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_ENTER_FABRIC: return
    set_state(cid, S_ENTER_DATE, fabric=message.text.strip())
    bot.send_message(cid, "📅 تاریخ تحویل:", reply_markup=date_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("date_") and
    get_state(c.message.chat.id).get("step") == S_ENTER_DATE)
@safe_handler
def cb_date(call: CallbackQuery):
    cid = call.message.chat.id
    if call.data == "date_manual":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, "✏️ تاریخ (YYYY-MM-DD):")
        bot.register_next_step_handler(msg, _recv_manual_date); return
    bot.answer_callback_query(call.id); _proceed_to_note(cid, call.data[5:])

@safe_handler
def _recv_manual_date(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_ENTER_DATE: return
    if not validate_jalali(message.text.strip()):
        msg = bot.send_message(cid, texts["invalid_date"])
        bot.register_next_step_handler(msg, _recv_manual_date); return
    _proceed_to_note(cid, jalali_to_gregorian(message.text.strip()))

def _proceed_to_note(cid: int, delivery_date: str):
    set_state(cid, S_ENTER_NOTE, delivery_date=delivery_date)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⏭ بدون یادداشت", callback_data="note_skip"))
    msg = bot.send_message(cid, "📝 یادداشت برای ادمین (یا رد کن):", reply_markup=kb)
    bot.register_next_step_handler(msg, _recv_note)

@bot.callback_query_handler(func=lambda c: c.data == "note_skip")
@safe_handler
def cb_note_skip(call: CallbackQuery):
    cid = call.message.chat.id
    if get_state(cid).get("step") != S_ENTER_NOTE:
        bot.answer_callback_query(call.id,"⚠️"); return
    bot.answer_callback_query(call.id); set_state(cid, S_ENTER_NOTE, note="")
    _show_order_confirm(cid)

@safe_handler
def _recv_note(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_ENTER_NOTE: return
    set_state(cid, S_ENTER_NOTE, note=message.text.strip()); _show_order_confirm(cid)

def _show_order_confirm(cid: int):
    d = get_state(cid)["data"]; set_state(cid, S_CONFIRM)
    summary = _format_order_summary(d["items"], d["fabric"], d["delivery_date"], d.get("note",""))
    bot.send_message(cid, f"{summary}\n\n⬆️ تایید می‌کنی؟", reply_markup=order_confirm_kb())

@bot.callback_query_handler(func=lambda c: c.data == "ord_confirm")
@safe_handler
def cb_order_confirm(call: CallbackQuery):
    cid = call.message.chat.id
    if get_state(cid).get("step") != S_CONFIRM:
        bot.answer_callback_query(call.id,"⚠️"); return
    bot.answer_callback_query(call.id, "⏳ ثبت...")
    d = get_state(cid)["data"]; user = db_get_user(cid)
    total = sum(i["line_total"] for i in d["items"])
    oid = db_create_order(user["id"], total, d["fabric"], d["delivery_date"],
                          d.get("note",""), d["items"])
    logger.info("New order created | order_id=%s | user=%s | total=%s", oid, user["name"], total)
    clear_state(cid)
    summary = _format_order_summary(d["items"], d["fabric"], d["delivery_date"], d.get("note",""))
    bot.send_message(cid, f"✅ سفارش #{oid} ثبت شد!\n\n{summary}\n\n⏳ منتظر تایید ادمین.",
                     reply_markup=main_menu_kb())
    _notify_admins(f"🔔 سفارش جدید #{oid}\n👤 {user['name']}\n\n{summary}",
                   admin_order_kb(oid))

@bot.callback_query_handler(func=lambda c: c.data == "ord_cancel")
@safe_handler
def cb_order_cancel(call: CallbackQuery):
    clear_state(call.message.chat.id); bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, texts["order_cancelled_msg"], reply_markup=main_menu_kb())

# ════════════════════════════════════════════════════════════════
#  بخش مالی کاربر
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "💰 بخش مالی")
@safe_handler
def btn_finance(message: Message):
    cid = message.chat.id; user = db_get_user(cid)
    if not user: bot.send_message(cid, texts["no_access_short"]); return
    fin = db_get_user_finance(user["id"])
    cmd = int(fin["current_month_debt"]) if fin else 0
    prv = int(fin["previous_debt"])      if fin else 0
    locked = db_get_active_installments_sum(user["id"])
    pnd = db_get_pending_payments(user["id"])
    pnd_sum = sum(int(p["amount"]) for p in pnd)
    text = (f"💰 بخش مالی\n━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 بدهی ماه جاری: {cmd:,} تومان\n"
            f"💳 بدهی قبلی:     {prv:,} تومان\n")
    if locked:
        text += f"   └ از این مقدار، {locked:,} تومان قسط‌بندی شده (از «اقساط من» پرداخت کنید)\n"
    text += f"📊 جمع کل:        {cmd+prv:,} تومان"
    if pnd_sum: text += f"\n⏳ در انتظار تایید: {pnd_sum:,} تومان"
    text += "\n━━━━━━━━━━━━━━━━━━━━"
    bot.send_message(cid, text, reply_markup=finance_main_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("fin_hist_"))
@safe_handler
def cb_fin_history(call: CallbackQuery):
    cid = call.message.chat.id; user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id,"⛔️"); return
    offset = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    txs, total = db_get_transactions_paged(user["id"], offset)
    if not txs: bot.send_message(cid, texts["no_transactions"]); return

    STATUS_T = {"pending":"⏳","approved":"✅","rejected":"❌",None:""}
    lines = [f"📜 تراکنش‌ها  ({min(offset+PAGE_SIZE,total)}/{total})\n"]
    for tx in txs:
        tp = TYPE_FA.get(tx["type"], tx["type"])
        st = STATUS_T.get(tx.get("status"))
        dt = to_jalali_full(tx["created_at"])
        lines.append(f"• {tp} {st}  {int(tx['amount']):,}ت  |  {dt}")

    kb = InlineKeyboardMarkup(row_width=2)
    pag = _paginate_kb(offset, total, f"fin_hist_{offset-PAGE_SIZE}", f"fin_hist_{offset+PAGE_SIZE}")
    if pag: kb.add(*pag)
    bot.send_message(cid, "\n".join(lines), reply_markup=kb if pag else None)

@bot.callback_query_handler(func=lambda c: c.data == "fin_inst")
@safe_handler
def cb_fin_inst(call: CallbackQuery):
    cid = call.message.chat.id; user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id,"⛔️"); return
    bot.answer_callback_query(call.id)
    insts = db_get_installments(user["id"])
    if not insts: bot.send_message(cid, texts["no_active_installments"]); return
    lines = ["📋 اقساط فعال:\n"]
    kb = InlineKeyboardMarkup(row_width=1)
    for i, inst in enumerate(insts, 1):
        remain = int(inst["total_amount"]) - int(inst["paid_amount"])
        lines.append(f"{i}. کل:{int(inst['total_amount']):,}  |  باقی:{remain:,}\n"
                     f"   {inst['num_installments']} قسط × {int(inst['per_installment']):,}")
        kb.add(InlineKeyboardButton(f"💳 پرداخت قسط #{inst['id']} (باقی:{remain:,})",
                                    callback_data=f"fin_pay_inst_{inst['id']}"))
    bot.send_message(cid, "\n".join(lines), reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("fin_pay_inst_"))
@safe_handler
def cb_fin_pay_inst(call: CallbackQuery):
    """شروع فلوی پرداخت برای یک قسط مشخص."""
    cid = call.message.chat.id; user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id,"⛔️"); return
    inst_id = int(call.data.split("_")[3])
    insts = db_get_installments(user["id"])
    inst = next((i for i in insts if i["id"] == inst_id), None)
    if not inst:
        bot.answer_callback_query(call.id, texts["installment_not_found"]); return
    remain = int(inst["total_amount"]) - int(inst["paid_amount"])
    if remain <= 0:
        bot.answer_callback_query(call.id, texts["installment_already_paid"]); return
    bot.answer_callback_query(call.id)
    set_state(cid, S_PAY_AMOUNT, max_pay=remain, cmd=0, prv=remain, installment_id=inst_id)
    msg = bot.send_message(cid,
        f"💳 پرداخت قسط #{inst_id}\n"
        f"باقی‌مانده‌ی این قسط: {remain:,} تومان\n\n"
        f"مبلغ پرداختی (تومان):")
    bot.register_next_step_handler(msg, _recv_pay_amount)

@bot.callback_query_handler(func=lambda c: c.data == "fin_pending")
@safe_handler
def cb_fin_pending(call: CallbackQuery):
    cid = call.message.chat.id; user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id,"⛔️"); return
    bot.answer_callback_query(call.id)
    pnd = db_get_pending_payments(user["id"])
    if not pnd: bot.send_message(cid, texts["no_pending_payments"]); return
    lines = ["⏳ در انتظار تایید:\n"]
    for p in pnd:
        lines.append(f"• {int(p['amount']):,} تومان  |  {to_jalali_full(p['created_at'])}")
    bot.send_message(cid, "\n".join(lines))

@bot.callback_query_handler(func=lambda c: c.data == "fin_new_pay")
@safe_handler
def cb_fin_new_pay(call: CallbackQuery):
    """
    پرداخت عادی بدهی (ماه جاری + بدهی قبلیِ آزاد).
    بخشی از previous_debt که قسط‌بندی شده، اینجا قابل پرداخت نیست -
    باید از طریق دکمه‌ی «پرداخت این قسط» در لیست اقساط پرداخت بشه.
    """
    cid = call.message.chat.id; user = db_get_user(cid)
    if not user: bot.answer_callback_query(call.id,"⛔️"); return
    fin = db_get_user_finance(user["id"])
    cmd = int(fin["current_month_debt"]) if fin else 0
    prv = int(fin["previous_debt"])      if fin else 0
    locked = db_get_active_installments_sum(user["id"])
    prv_available = max(0, prv - locked)
    total = cmd + prv_available
    if total <= 0:
        bot.answer_callback_query(call.id,texts["no_free_debt"])
        if locked > 0:
            bot.send_message(cid,
                f"✅ بدهی ماه جاری و بدهی قبلیِ آزاد شما صفره.\n"
                f"⚠️ توجه: {locked:,} تومان از بدهی قبلیتون قسط‌بندی شده — "
                f"اون رو از «📋 اقساط من» پرداخت کنید.")
        return
    bot.answer_callback_query(call.id)
    set_state(cid, S_PAY_AMOUNT, max_pay=total, cmd=cmd, prv=prv_available, installment_id=None)
    extra = f"\n⚠️ {locked:,} تومان از بدهی قبلی قسط‌بندی شده و اینجا قابل پرداخت نیست." if locked else ""
    msg = bot.send_message(cid,
        f"💳 ثبت پرداخت\n\n📅 ماه جاری: {cmd:,}\n💳 قبلیِ آزاد: {prv_available:,}\n"
        f"حداکثر: {total:,}{extra}\n\nمبلغ پرداختی (تومان):")
    bot.register_next_step_handler(msg, _recv_pay_amount)

@safe_handler
def _recv_pay_amount(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_PAY_AMOUNT: return
    try:
        amount = int(message.text.strip().replace(",","").replace("،",""))
        if amount <= 0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid, texts["positive_number"]); bot.register_next_step_handler(msg, _recv_pay_amount); return
    d = get_state(cid)["data"]
    if amount > d["max_pay"]:
        msg = bot.send_message(cid, f"❌ حداکثر {d['max_pay']:,} تومان:")
        bot.register_next_step_handler(msg, _recv_pay_amount); return

    if d.get("installment_id"):
        # پرداخت قسط: کل مبلغ مستقیم از همون قسط/بدهی قبلی کسر می‌شه، تفکیک ماه‌جاری/قبلی معنی نداره
        set_state(cid, S_PAY_RECEIPT, pay_amount=amount, paid_current=0, paid_previous=amount)
        msg = bot.send_message(cid,
            f"💡 این مبلغ بابت قسط #{d['installment_id']} از بدهی قبلی کسر می‌شه.\n\n"
            + texts["send_receipt"])
    else:
        pc = min(amount, d["cmd"]); pp = min(amount - pc, d["prv"])
        set_state(cid, S_PAY_RECEIPT, pay_amount=amount, paid_current=pc, paid_previous=pp)
        msg = bot.send_message(cid,
            f"💡 از ماه جاری: {pc:,}  |  از قبلی: {pp:,}\n\n📎 فیش واریزی رو ارسال کن:")
    bot.register_next_step_handler(msg, _recv_receipt)

@safe_handler
def _recv_receipt(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_PAY_RECEIPT: return
    file_id = None
    if message.photo:       file_id = message.photo[-1].file_id
    elif message.document:  file_id = message.document.file_id
    if not file_id:
        msg = bot.send_message(cid, texts["send_image_or_file"])
        bot.register_next_step_handler(msg, _recv_receipt); return
    set_state(cid, S_PAY_NOTE, receipt_file_id=file_id)
    msg = bot.send_message(cid,
        "📝 اگه توضیح یا یادداشتی برای این پرداخت داری بنویس (مثلاً شماره پیگیری بانکی یا توضیح واریز).\n"
        "اگه چیزی نداری «-» بفرست:")
    bot.register_next_step_handler(msg, _recv_pay_note)

@safe_handler
def _recv_pay_note(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_PAY_NOTE: return
    note = message.text.strip() if message.text else "-"
    note = None if note == "-" else note
    set_state(cid, S_PAY_CONFIRM, user_note=note)
    d = get_state(cid)["data"]
    note_line = f"\n📝 یادداشت: {note}" if note else ""
    bot.send_message(cid,
        f"📋 مبلغ: {d['pay_amount']:,} تومان\n  └ ماه جاری: {d['paid_current']:,}\n"
        f"  └ قبلی: {d['paid_previous']:,}{note_line}\n\nارسال می‌کنی؟",
        reply_markup=pay_confirm_kb())

@bot.callback_query_handler(func=lambda c: c.data == "pay_submit")
@safe_handler
def cb_pay_submit(call: CallbackQuery):
    cid = call.message.chat.id
    if get_state(cid).get("step") != S_PAY_CONFIRM:
        bot.answer_callback_query(call.id,"⚠️"); return
    bot.answer_callback_query(call.id,"⏳ ارسال...")
    d = get_state(cid)["data"]; user = db_get_user(cid)
    inst_id = d.get("installment_id"); user_note = d.get("user_note")
    tx_id = db_create_payment_request(user["id"], d["pay_amount"], d["receipt_file_id"], inst_id, user_note)
    logger.info("Payment request submitted | tx_id=%s | user=%s | amount=%s", tx_id, user["name"], d["pay_amount"])
    clear_state(cid)
    bot.send_message(cid, f"✅ درخواست #{tx_id} ارسال شد.\nمنتظر تایید ادمین باش.",
                     reply_markup=main_menu_kb())
    note_line = f"\n📝 یادداشت کاربر: {user_note}" if user_note else ""
    if inst_id:
        cap = (f"🔔 پرداخت قسط جدید #{tx_id}\n👤 {user['name']}\n"
               f"📋 قسط #{inst_id}\n💵 {d['pay_amount']:,} تومان{note_line}")
    else:
        cap = (f"🔔 پرداخت جدید #{tx_id}\n👤 {user['name']}\n"
               f"💵 {d['pay_amount']:,} تومان\n  └ ماه جاری:{d['paid_current']:,}\n  └ قبلی:{d['paid_previous']:,}{note_line}")
    for ac in ADMIN_IDS:
        try:
            bot.send_photo(ac, d["receipt_file_id"], caption=cap, reply_markup=admin_payment_kb(tx_id))
        except Exception:
            try: bot.send_document(ac, d["receipt_file_id"], caption=cap, reply_markup=admin_payment_kb(tx_id))
            except Exception as e: print(f"[WARN] {e}")

@bot.callback_query_handler(func=lambda c: c.data == "pay_cancel")
@safe_handler
def cb_pay_cancel(call: CallbackQuery):
    clear_state(call.message.chat.id); bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, texts["payment_cancelled"], reply_markup=main_menu_kb())

# ════════════════════════════════════════════════════════════════
#  ادمین - سفارشات
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📋 سفارشات")
@safe_handler
def btn_admin_orders(message: Message):
    cid = message.chat.id
    if not _is_admin(cid): bot.send_message(cid,"⛔️"); return
    bot.send_message(cid, texts["admin_orders_filter"], reply_markup=order_filter_kb("aord"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("aord_") and c.data != "aord_back_filters")
@safe_handler
def cb_admin_orders(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    parts = call.data.split("_"); status = parts[1]; offset = int(parts[2])
    is_super = _is_superadmin(cid)
    orders, total = db_get_orders_paged(status, offset, show_price=is_super)
    bot.answer_callback_query(call.id)
    if not orders:
        bot.send_message(cid, f"هیچ سفارشی در «{STATUS_FA.get(status,status)}» وجود ندارد."); return

    kb = InlineKeyboardMarkup(row_width=1)
    for o in orders:
        st_e = STATUS_EMOJI.get(o["status"],"•")
        price_part = f"  |  {int(o['total_price']):,}ت" if is_super and "total_price" in o else ""
        delivery = to_jalali(o['delivery_date'], '%m/%d') if o.get('delivery_date') else "—"
        lbl = f"{st_e} #{o['id']} {o['user_name']}  📅{delivery}{price_part}"
        kb.add(InlineKeyboardButton(lbl, callback_data=f"aodet_{o['id']}"))

    pag = _paginate_kb(offset, total, f"aord_{status}_{offset-PAGE_SIZE}", f"aord_{status}_{offset+PAGE_SIZE}")
    if pag: kb.add(*pag)
    kb.add(InlineKeyboardButton("🔙 فیلترها", callback_data="aord_back_filters"))
    bot.send_message(cid, f"📋 {STATUS_FA.get(status,'سفارشات')}  ({min(offset+PAGE_SIZE,total)}/{total})",
                     reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "aord_back_filters")
@safe_handler
def cb_ao_back(call: CallbackQuery):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "📋 فیلتر:", reply_markup=order_filter_kb("aord"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("aodet_"))
@safe_handler
def cb_admin_order_detail(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    order_id = int(call.data.split("_")[1]); is_super = _is_superadmin(cid)
    bot.answer_callback_query(call.id)
    order = db_get_order_full(order_id)
    if not order: bot.send_message(cid,texts["order_not_found"]); return
    user = db_get_user_by_db_id(order["user_id"])
    if is_super:
        items_text = "\n".join(f"  • {i['model_name']} ×{i['quantity']} = {int(i['line_total']):,}" for i in order["items"])
        text = (f"📦 سفارش #{order_id}  |  {STATUS_FA.get(order['status'],'—')}\n"
                f"👤 {user['name'] if user else '—'}  |  📅 ثبت: {to_jalali_full(order['created_at'])}\n"
                f"🚚 تحویل: {to_jalali(order['delivery_date'])}  |  🧵 پارچه: {order['fabric']}\n\n"
                f"{items_text}\n\n💵 {int(order['total_price']):,} تومان")
    else:
        items = db_get_order_items_no_price(order_id)
        items_text = "\n".join(f"  • {i['model_name']} ×{i['quantity']}" for i in items)
        text = (f"📦 سفارش #{order_id}  |  {STATUS_FA.get(order['status'],'—')}\n"
                f"👤 {user['name'] if user else '—'}  |  📅 ثبت: {to_jalali_full(order['created_at'])}\n"
                f"🚚 تحویل: {to_jalali(order['delivery_date'])}  |  🧵 پارچه: {order['fabric']}\n\n{items_text}")
    if order.get("note"): text += f"\n📝 یادداشت: {order['note']}"
    if order.get("rejection_reason"): text += f"\n❌ دلیل رد: {order['rejection_reason']}"
    if order.get("history"):
        text += "\n\n📜 تاریخچه‌ی وضعیت:"
        for h in order["history"]:
            ts = to_jalali_full(h["changed_at"])
            by = f" - {h['admin_name']}" if h.get("admin_name") else ""
            text += f"\n  • {STATUS_FA.get(h['new_status'], h['new_status'])}  |  {ts}{by}"
    bot.send_message(cid, text, reply_markup=order_status_change_kb(order_id, order["status"], is_super))

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_st_"))
@safe_handler
def cb_admin_set_status(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    parts = call.data.split("_"); order_id = int(parts[2]); status = parts[3]
    admin_id = _admin_db_id(cid)
    if status == "rejected":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, f"❌ دلیل رد سفارش #{order_id}:")
        bot.register_next_step_handler(msg, _admin_reject_reason, order_id); return
    if status == "cancelled":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, f"🚫 دلیل لغو سفارش #{order_id} (یا «-» برای رد کردن دلیل):")
        bot.register_next_step_handler(msg, _admin_cancel_reason, order_id); return
    db_update_order_status(order_id, status, admin_db_id=admin_id)
    logger.info("Order status updated | order_id=%s | status=%s | admin_cid=%s", order_id, status, cid)
    bot.answer_callback_query(call.id, "✅ بروز شد.")
    bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=None)
    bot.send_message(cid, f"سفارش #{order_id} → {STATUS_FA.get(status,status)}")
    _notify_user_order_status(order_id, status)

@safe_handler
def _admin_reject_reason(message: Message, order_id: int):
    reason = message.text.strip()
    admin_id = _admin_db_id(message.chat.id)
    db_update_order_status(order_id, "rejected", reason, admin_db_id=admin_id)
    bot.send_message(message.chat.id, f"✅ سفارش #{order_id} رد شد.")
    _notify_user_order_status(order_id, "rejected", reason)

@safe_handler
def _admin_cancel_reason(message: Message, order_id: int):
    reason = message.text.strip()
    reason = None if reason == "-" else reason
    admin_id = _admin_db_id(message.chat.id)
    db_cancel_order(order_id, admin_db_id=admin_id)
    bot.send_message(message.chat.id, f"✅ سفارش #{order_id} لغو شد.")
    _notify_user_order_status(order_id, "cancelled", reason)

def _notify_user_order_status(order_id: int, status: str, reason: str = None):
    order = db_get_order_full(order_id)
    if not order: return
    user = db_get_user_by_db_id(order["user_id"])
    if not user: return
    msgs = {
        "approved":  f"🎉 سفارش #{order_id} تایید شد.",
        "producing": f"🔧 سفارش #{order_id} در حال تولید است.",
        "ready":     f"📦 سفارش #{order_id} آماده تحویل است.",
        "delivered": f"✅ سفارش #{order_id} تحویل داده شد.",
        "rejected":  f"❌ سفارش #{order_id} رد شد." + (f"\nدلیل: {reason}" if reason else ""),
        "cancelled": f"🚫 سفارش #{order_id} لغو شد.",
    }
    if status in msgs:
        try: bot.send_message(user["cid"], f"{msgs[status]}")
        except Exception as e: print(f"[WARN] {e}")

# ── تایید/رد سفارش از پیام ادمین ─────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_approve_"))
@safe_handler
def cb_approve(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    order_id = int(call.data.split("_")[2])
    db_update_order_status(order_id, "approved")
    bot.answer_callback_query(call.id,"✅ تایید شد.")
    bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=None)
    _notify_user_order_status(order_id, "approved")

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_reject_"))
@safe_handler
def cb_reject(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    order_id = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    msg = bot.send_message(cid, f"❌ دلیل رد #{order_id}:")
    bot.register_next_step_handler(msg, _admin_reject_reason, order_id)

# ── تایید/رد پرداخت ──────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_ok_"))
@safe_handler
def cb_pay_approve(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    tx_id = int(call.data.split("_")[2]); result = db_approve_payment(tx_id, _admin_db_id(cid))
    if not result: bot.answer_callback_query(call.id,texts["already_processed"]); return
    bot.answer_callback_query(call.id,"✅ تایید شد.")
    bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=None)
    user = db_get_user_by_db_id(result["user_db_id"])

    if result.get("is_installment"):
        bot.send_message(cid, f"✅ پرداخت قسط #{tx_id} تایید شد.\n"
                         f"📋 قسط #{result['installment_id']}  |  💵 {result['amount']:,}\n"
                         f"باقی‌مانده‌ی قسط: {result['installment_remaining']:,}\n"
                         f"بدهی قبلی جدید: {result['new_prev']:,}")
        if user:
            try:
                bot.send_message(user["cid"],
                    f"✅ پرداخت قسط {result['amount']:,} تومانی تایید شد!\n"
                    f"باقی‌مانده‌ی این قسط: {result['installment_remaining']:,}\n"
                    f"💳 بدهی قبلی: {result['new_prev']:,}")
            except: pass
    else:
        bot.send_message(cid, f"✅ پرداخت #{tx_id} تایید شد.\n"
                         f"💵 {result['amount']:,}  |  ماه جاری:{result['paid_current']:,}  |  قبلی:{result['paid_previous']:,}\n"
                         f"وضعیت: ماه جاری:{result['new_cmd']:,}  قبلی:{result['new_prev']:,}")
        if user:
            try:
                bot.send_message(user["cid"],
                    f"✅ پرداخت {result['amount']:,} تومانی تایید شد!\n"
                    f"📅 بدهی ماه جاری: {result['new_cmd']:,}\n💳 بدهی قبلی: {result['new_prev']:,}")
            except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_no_"))
@safe_handler
def cb_pay_reject(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    tx_id = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    msg = bot.send_message(cid, f"❌ دلیل رد پرداخت #{tx_id}:")
    bot.register_next_step_handler(msg, _pay_reject_reason, tx_id)

@safe_handler
def _pay_reject_reason(message: Message, tx_id: int):
    reason = message.text.strip()
    db_reject_payment(tx_id, _admin_db_id(message.chat.id), reason)
    logger.info("Payment rejected | tx_id=%s | admin_cid=%s | reason=%s", tx_id, message.chat.id, reason)
    bot.send_message(message.chat.id, f"✅ پرداخت #{tx_id} رد شد.")
    tx = db_get_transaction_by_id(tx_id)
    if tx:
        user = db_get_user_by_db_id(tx["user_id"])
        if user:
            try: bot.send_message(user["cid"],
                     f"❌ پرداخت {int(tx['amount']):,} تومانی رد شد.\nدلیل: {reason}")
            except: pass

# ════════════════════════════════════════════════════════════════
#  سوپرادمین - کاربران
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "👥 کاربران")
@safe_handler
def btn_sa_users(message: Message):
    cid = message.chat.id
    if not _is_superadmin(cid): bot.send_message(cid,"⛔️"); return
    users = db_get_all_users_summary()
    total_debt = sum(int(u["current_month_debt"])+int(u["previous_debt"]) for u in users)
    banned = sum(1 for u in users if u["is_banned"])
    # صفحه‌بندی کاربران
    page_users = users[:PAGE_SIZE]
    kb = sa_users_kb(page_users)
    if len(users) > PAGE_SIZE:
        kb.add(InlineKeyboardButton(f"بعدی {min(PAGE_SIZE,len(users)-PAGE_SIZE)}تا ▶️",
                                    callback_data=f"sa_ulist_{PAGE_SIZE}"))
    bot.send_message(cid,
        f"👥 کاربران ({len(users)} نفر)\n🔴 مسدود:{banned}  |  💰 کل:{total_debt:,}",
        reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_ulist_"))
@safe_handler
def cb_sa_ulist_page(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    offset = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    users = db_get_all_users_summary()
    page_users = users[offset:offset+PAGE_SIZE]
    kb = sa_users_kb(page_users)
    pag = _paginate_kb(offset, len(users), f"sa_ulist_{offset-PAGE_SIZE}", f"sa_ulist_{offset+PAGE_SIZE}")
    if pag: kb.add(*pag)
    bot.send_message(cid, f"👥 کاربران ({min(offset+PAGE_SIZE,len(users))}/{len(users)})", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "sa_users_list")
@safe_handler
def cb_sa_users_list(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    bot.answer_callback_query(call.id)
    users = db_get_all_users_summary()
    bot.send_message(cid, "👥 لیست کاربران:", reply_markup=sa_users_kb(users[:PAGE_SIZE]))

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_u_") and
    not any(c.data.startswith(p) for p in ["sa_unban_","sa_ban_","sa_ulist_"]))
@safe_handler
def cb_sa_user_detail(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    report = db_get_user_full_report(uid)
    if not report: bot.send_message(cid,texts["user_not_found"]); return
    u = report["user"]; f = report["finance"] or {}
    cmd = int(f.get("current_month_debt",0)); prv = int(f.get("previous_debt",0))
    text = (f"👤 {u['name']}  |  {'🚫' if u['is_banned'] else '✅'}\n"
            f"CID: {u['cid']}  |  @{u['username'] or '—'}\n\n"
            f"💰 ماه جاری: {cmd:,}  |  قبلی: {prv:,}  |  جمع: {cmd+prv:,}\n")
    if u["is_banned"] and u.get("ban_reason"): text += f"🚫 دلیل: {u['ban_reason']}\n"
    if report["installments"]:
        text += f"\n📋 اقساط: {len(report['installments'])} مورد فعال\n"
    if report["orders"]:
        text += f"\n📦 آخرین سفارشات:\n"
        for o in report["orders"][:5]:
            st = STATUS_EMOJI.get(o["status"],"•")
            text += f"  {st} #{o['id']}  {int(o['total_price']):,}ت  {to_jalali(o['created_at'], '%m/%d')}\n"
    if report["transactions"]:
        text += f"\n💳 آخرین تراکنش‌ها:\n"
        for tx in report["transactions"][:5]:
            tp = TYPE_FA.get(tx["type"],"•")
            text += f"  {tp} {int(tx['amount']):,}ت  {to_jalali(tx['created_at'], '%m/%d')}\n"
    bot.send_message(cid, text, reply_markup=sa_user_actions_kb(uid, u["is_banned"]))

# ── جزئیات کاربر: لیست کامل سفارشات ────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_uord_"))
@safe_handler
def cb_sa_user_orders(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    orders = db_get_user_orders_all(uid)
    if not orders:
        bot.send_message(cid, texts["no_orders_for_user"]); return
    kb = InlineKeyboardMarkup(row_width=1)
    for o in orders:
        st = STATUS_EMOJI.get(o["status"], "•")
        kb.add(InlineKeyboardButton(
            f"{st} #{o['id']}  |  {int(o['total_price']):,}ت  |  {to_jalali(o['created_at'],'%m/%d')}",
            callback_data=f"aodet_{o['id']}"))
    kb.add(InlineKeyboardButton("🔙 برگشت به کاربر", callback_data=f"sa_u_{uid}"))
    bot.send_message(cid, f"📦 همه‌ی سفارشات ({len(orders)} مورد):", reply_markup=kb)

# ── جزئیات کاربر: لیست کامل تراکنش‌ها/پرداخت‌ها ─────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_utx_"))
@safe_handler
def cb_sa_user_transactions(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    txs = db_get_user_transactions_all(uid)
    if not txs:
        bot.send_message(cid, texts["no_transactions_for_user"]); return
    kb = InlineKeyboardMarkup(row_width=1)
    for tx in txs:
        tp = TYPE_FA.get(tx["type"], "•")
        st_lbl = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(tx.get("status"), "")
        rec = "📎" if tx.get("receipt_file_id") else ""
        kb.add(InlineKeyboardButton(
            f"{tp} {st_lbl}{rec} {int(tx['amount']):,}ت  |  {to_jalali(tx['created_at'],'%m/%d')}",
            callback_data=f"sa_txd_{tx['id']}"))
    kb.add(InlineKeyboardButton("🔙 برگشت به کاربر", callback_data=f"sa_u_{uid}"))
    bot.send_message(cid, f"💳 همه‌ی تراکنش‌ها ({len(txs)} مورد):", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_txd_"))
@safe_handler
def cb_sa_tx_detail(call: CallbackQuery):
    """جزئیات کامل یک تراکنش - شامل یادداشت کاربر و رسید پرداخت (در صورت وجود)."""
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    tx_id = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    tx = db_get_transaction_by_id(tx_id)
    if not tx: bot.send_message(cid, "❌ تراکنش پیدا نشد."); return
    user = db_get_user_by_db_id(tx["user_id"])
    tp = TYPE_FA.get(tx["type"], tx["type"])
    st_lbl = {"pending": "⏳ در انتظار", "approved": "✅ تایید شده", "rejected": "❌ رد شده"}.get(tx.get("status"), "—")
    text = (f"{tp}  |  {st_lbl}\n"
            f"👤 {user['name'] if user else '—'}\n"
            f"💵 مبلغ: {int(tx['amount']):,} تومان\n"
            f"📅 {to_jalali_full(tx['created_at'])}\n")
    if tx.get("installment_id"):
        text += f"📋 مربوط به قسط #{tx['installment_id']}\n"
    if tx.get("user_note"):
        text += f"\n📝 یادداشت کاربر: {tx['user_note']}\n"
    if tx.get("description"):
        text += f"\nℹ️ {tx['description']}\n"
    if tx.get("receipt_file_id"):
        try:
            bot.send_photo(cid, tx["receipt_file_id"], caption=text)
        except Exception:
            try:
                bot.send_document(cid, tx["receipt_file_id"], caption=text)
            except Exception:
                bot.send_message(cid, text + "\n" + texts["receipt_not_shown"])
    else:
        bot.send_message(cid, text)

# ── جزئیات کاربر: لیست کامل اقساط ───────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_uinst_"))
@safe_handler
def cb_sa_user_installments_list(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    insts = db_get_installments(uid)
    if not insts:
        bot.send_message(cid, texts["no_installments_for_user"]); return
    kb = InlineKeyboardMarkup(row_width=1)
    for i in insts:
        done = "✅" if int(i["paid_amount"]) >= int(i["total_amount"]) else "⏳"
        kb.add(InlineKeyboardButton(
            f"{done} قسط #{i['id']}  |  {int(i['paid_amount']):,}/{int(i['total_amount']):,}ت",
            callback_data=f"sa_instd_{i['id']}"))
    kb.add(InlineKeyboardButton("🔙 برگشت به کاربر", callback_data=f"sa_u_{uid}"))
    bot.send_message(cid, f"📋 همه‌ی اقساط ({len(insts)} مورد):", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_instd_"))
@safe_handler
def cb_sa_installment_detail(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    inst_id = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    inst = db_get_installment_by_id(inst_id)
    if not inst: bot.send_message(cid, "❌ قسط پیدا نشد."); return
    user = db_get_user_by_db_id(inst["user_id"])
    paid = int(inst["paid_amount"]); total = int(inst["total_amount"])
    remaining = total - paid
    text = (f"📋 قسط #{inst_id}\n"
            f"👤 {user['name'] if user else '—'}\n"
            f"💵 کل: {total:,}  |  پرداخت‌شده: {paid:,}  |  باقی: {remaining:,}\n"
            f"📊 {inst['num_installments']} قسط × {int(inst['per_installment']):,} تومان\n"
            f"📅 ثبت‌شده: {to_jalali_full(inst['created_at'])}"
            f"{' | توسط ' + inst['admin_name'] if inst.get('admin_name') else ''}\n")
    due = inst.get("due_dates")
    if due:
        try:
            dates = json.loads(due) if isinstance(due, str) else due
            def _greg_str_to_jalali(s):
                try:
                    import datetime as _dt
                    g = _dt.date.fromisoformat(s)
                    return jdatetime.date.fromgregorian(date=g).strftime("%Y/%m/%d")
                except Exception:
                    return s
            text += "\n📆 سررسیدها:\n" + "\n".join(f"  • {_greg_str_to_jalali(d)}" for d in dates)
        except Exception:
            pass
    payments = db_get_installment_payments(inst_id)
    if payments:
        text += "\n\n💳 پرداخت‌های ثبت‌شده برای این قسط:\n"
        for p in payments:
            st_lbl = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(p.get("status"), "")
            text += f"  {st_lbl} {int(p['amount']):,}ت  |  {to_jalali(p['created_at'],'%m/%d')}\n"
    bot.send_message(cid, text)


@bot.callback_query_handler(func=lambda c: c.data == "sa_add_user")
@safe_handler
def cb_sa_add_user(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    bot.answer_callback_query(call.id)
    set_state(cid, S_SA_ADD_USER_CID)
    msg = bot.send_message(cid,"➕ کاربر جدید\nChat ID:")
    bot.register_next_step_handler(msg, _sa_user_cid)

@safe_handler
def _sa_user_cid(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_ADD_USER_CID: return
    try: new_cid = int(m.text.strip())
    except ValueError:
        msg = bot.send_message(cid,"❌ Chat ID عدد باشه:"); bot.register_next_step_handler(msg,_sa_user_cid); return
    set_state(cid, S_SA_ADD_USER_NAME, new_cid=new_cid)
    msg = bot.send_message(cid,"نام:"); bot.register_next_step_handler(msg,_sa_user_name)

@safe_handler
def _sa_user_name(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_ADD_USER_NAME: return
    set_state(cid, S_SA_ADD_USER_UN, new_name=m.text.strip())
    msg = bot.send_message(cid,"یوزرنیم (بدون @ یا ۰):"); bot.register_next_step_handler(msg,_sa_user_un)

@safe_handler
def _sa_user_un(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_ADD_USER_UN: return
    un = None if m.text.strip()=="0" else m.text.strip()
    d = get_state(cid)["data"]; admin = db_get_admin(cid)
    ok = db_add_user(d["new_cid"], d["new_name"], un, admin["id"])
    clear_state(cid)
    if ok:
        logger.info("New user added | cid=%s | name=%s | by_admin=%s", d["new_cid"], d["new_name"], cid)
        bot.send_message(cid, f"✅ کاربر {d['new_name']} اضافه شد.")
        try: bot.send_message(d["new_cid"], texts["account_activated"])
        except: pass
    else:
        bot.send_message(cid,texts["user_already_registered"])

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_ban_"))
@safe_handler
def cb_sa_ban(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    db_ban_user(uid, texts["ban_reason_admin"])
    logger.info("User banned by superadmin | uid=%s | admin_cid=%s", uid, cid)
    bot.send_message(cid,texts["user_banned_msg"])
    user = db_get_user_by_db_id(uid)
    if user:
        try: bot.send_message(user["cid"],texts["account_banned_by_admin"])
        except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_unban_"))
@safe_handler
def cb_sa_unban(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    db_unban_user(uid)
    logger.info("User unbanned by superadmin | uid=%s | admin_cid=%s", uid, cid)
    bot.send_message(cid,texts["user_unbanned_msg"])
    user = db_get_user_by_db_id(uid)
    if user:
        try: bot.send_message(user["cid"],texts["account_unbanned"])
        except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_deact_user_"))
@safe_handler
def cb_sa_deact_user(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[3]); bot.answer_callback_query(call.id)
    db_deactivate_user(uid); bot.send_message(cid,texts["user_deactivated_msg"])

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_del_user_"))
@safe_handler
def cb_sa_del_user(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[3])
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🔴 بله، حذف کامل", callback_data=f"sa_del_confirm_{uid}"),
           InlineKeyboardButton("❌ انصراف",          callback_data="sa_del_abort"))
    bot.answer_callback_query(call.id)
    bot.send_message(cid,texts["confirm_delete_user"],
                     reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_del_confirm_"))
@safe_handler
def cb_sa_del_confirm(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[3])
    user = db_get_user_by_db_id(uid)
    db_delete_user(uid)
    logger.info("User permanently deleted | uid=%s | name=%s | by_admin=%s", uid, user["name"] if user else "?", cid)
    bot.answer_callback_query(call.id,"✅ حذف شد.")
    bot.send_message(cid, f"🗑 کاربر {user['name'] if user else uid} حذف شد.")

@bot.callback_query_handler(func=lambda c: c.data == "sa_del_abort")
@safe_handler
def cb_sa_del_abort(call: CallbackQuery):
    bot.answer_callback_query(call.id,"❌ لغو شد.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_transfer_"))
@safe_handler
def cb_sa_transfer(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); admin = db_get_admin(cid)
    bot.answer_callback_query(call.id,"⏳...")
    amount = db_transfer_debt_to_previous(uid, admin["id"])
    if amount == 0: bot.send_message(cid,texts["no_current_debt"]); return
    logger.info("Debt transferred to previous | uid=%s | amount=%s | by_admin=%s", uid, amount, cid)
    bot.send_message(cid, f"✅ {amount:,} تومان منتقل شد. پنل باز شد.")
    user = db_get_user_by_db_id(uid)
    if user:
        try: bot.send_message(user["cid"],
                 f"🔔 بدهی ماه جاری ({amount:,}ت) به بدهی قبلی منتقل شد. پنل باز است.")
        except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_adddebt_"))
@safe_handler
def cb_sa_adddebt(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    set_state(cid, S_SA_DEBT_AMOUNT, target_uid=uid)
    msg = bot.send_message(cid,"➕ مبلغ بدهی دستی (تومان):")
    bot.register_next_step_handler(msg,_sa_debt_amt)

@safe_handler
def _sa_debt_amt(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_DEBT_AMOUNT: return
    try:
        amount = int(m.text.strip().replace(",","").replace("،",""))
        if amount<=0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid,texts["positive_number"]); bot.register_next_step_handler(msg,_sa_debt_amt); return
    set_state(cid, S_SA_DEBT_DESC, debt_amount=amount)
    msg = bot.send_message(cid,"توضیح:"); bot.register_next_step_handler(msg,_sa_debt_desc)

@safe_handler
def _sa_debt_desc(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_DEBT_DESC: return
    d = get_state(cid)["data"]; admin = db_get_admin(cid)
    db_add_manual_debt(d["target_uid"], d["debt_amount"], m.text.strip(), admin["id"])
    logger.info("Manual debt added | uid=%s | amount=%s | by_admin=%s", d["target_uid"], d["debt_amount"], cid)
    clear_state(cid)
    bot.send_message(cid, f"✅ {d['debt_amount']:,} تومان بدهی اضافه شد.")
    user = db_get_user_by_db_id(d["target_uid"])
    if user:
        try: bot.send_message(user["cid"],
                 f"🔔 {d['debt_amount']:,} تومان به بدهی ماه جاری شما اضافه شد.")
        except: pass

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_inst_"))
@safe_handler
def cb_sa_inst(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    uid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    fin = db_get_user_finance(uid)
    prev = int(fin["previous_debt"]) if fin else 0
    already = db_get_active_installments_sum(uid)
    available = prev - already
    if available <= 0:
        bot.send_message(cid,
            f"⚠️ بدهی قبلی این کاربر {prev:,} تومانه که "
            f"{already:,} تومانش از قبل قسط‌بندی شده.\n"
            f"چیزی برای قسط‌بندی جدید آزاد نیست.")
        return
    set_state(cid, S_SA_INST_AMOUNT, target_uid=uid, inst_available=available)
    msg = bot.send_message(cid,
        f"📋 قسط‌بندی جدید\n"
        f"💳 بدهی قبلی: {prev:,}  |  قبلاً قسط‌بندی شده: {already:,}\n"
        f"✅ حداکثر قابل قسط‌بندی: {available:,} تومان\n\n"
        f"کل مبلغ قسط‌بندی رو وارد کن:")
    bot.register_next_step_handler(msg,_sa_inst_amt)

@safe_handler
def _sa_inst_amt(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_INST_AMOUNT: return
    try:
        amount = int(m.text.strip().replace(",","").replace("،",""))
        if amount<=0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid,texts["positive_number"]); bot.register_next_step_handler(msg,_sa_inst_amt); return
    available = get_state(cid)["data"]["inst_available"]
    if amount > available:
        msg = bot.send_message(cid,
            f"❌ این مبلغ از سقف قابل قسط‌بندی ({available:,} تومان) بیشتره.\nمجدد وارد کن:")
        bot.register_next_step_handler(msg,_sa_inst_amt); return
    set_state(cid, S_SA_INST_COUNT, inst_total=amount)
    msg = bot.send_message(cid,"تعداد اقساط:"); bot.register_next_step_handler(msg,_sa_inst_count)

@safe_handler
def _sa_inst_count(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_INST_COUNT: return
    try:
        count = int(m.text.strip()); assert count>0
    except (ValueError, AssertionError):
        msg = bot.send_message(cid,texts["positive_number"]); bot.register_next_step_handler(msg,_sa_inst_count); return
    d = get_state(cid)["data"]; per = d["inst_total"]//count
    set_state(cid, S_SA_INST_DATES, inst_count=count, per_inst=per)
    msg = bot.send_message(cid,
        f"هر قسط: {per:,} تومان\n\n{count} تاریخ سررسید (خط به خط، شمسی — مثال: ۱۴۰۳/۰۶/۱۵):")
    bot.register_next_step_handler(msg,_sa_inst_dates)

@safe_handler
def _sa_inst_dates(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_INST_DATES: return
    lines = [l.strip() for l in m.text.strip().splitlines() if l.strip()]
    d = get_state(cid)["data"]
    if len(lines) != d["inst_count"]:
        msg = bot.send_message(cid,f"❌ باید {d['inst_count']} تاریخ باشه:")
        bot.register_next_step_handler(msg,_sa_inst_dates); return
    for l in lines:
        if not validate_jalali(l):
            msg = bot.send_message(cid, f"❌ تاریخ شمسی معتبر نیست: {l}\nفرمت: ۱۴۰۳/۰۶/۱۵")
            bot.register_next_step_handler(msg, _sa_inst_dates); return
    lines = [jalali_to_gregorian(l) for l in lines]
    admin = db_get_admin(cid)
    iid, err_available = db_add_installment(d["target_uid"], d["inst_total"], d["inst_count"], lines, admin["id"])
    clear_state(cid)
    if iid is None:
        logger.warning("Installment creation failed (exceeds available debt) | uid=%s | requested=%s | available=%s", d["target_uid"], d["inst_total"], err_available)
        bot.send_message(cid,
            f"❌ مبلغ قسط‌بندی از سقف بدهی قبلیِ آزاد ({err_available:,} تومان) بیشتر شده "
            f"(احتمالاً همزمان یه قسط‌بندی دیگه ثبت شده). دوباره از منوی کاربر امتحان کن.")
        return
    logger.info("Installment created | iid=%s | uid=%s | total=%s | count=%s | by_admin=%s", iid, d["target_uid"], d["inst_total"], d["inst_count"], cid)
    bot.send_message(cid, f"✅ قسط‌بندی #{iid} ثبت شد.")
    user = db_get_user_by_db_id(d["target_uid"])
    if user:
        try: bot.send_message(user["cid"],
                 f"📋 قسط‌بندی {d['inst_total']:,} تومان ({d['inst_count']} قسط) تنظیم شد.")
        except: pass

# ════════════════════════════════════════════════════════════════
#  سوپرادمین - مدل‌ها
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📦 مدل‌ها")
@safe_handler
def btn_sa_models(message: Message):
    cid = message.chat.id
    if not _is_superadmin(cid): bot.send_message(cid,"⛔️"); return
    models = db_get_all_models()
    bot.send_message(cid, f"📦 مدل‌ها ({len(models)})",
                     reply_markup=sa_models_kb(models))

@bot.callback_query_handler(func=lambda c: c.data == "sa_models_list")
@safe_handler
def cb_sa_models_list(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    bot.answer_callback_query(call.id)
    bot.send_message(cid,"📦 مدل‌ها:", reply_markup=sa_models_kb(db_get_all_models()))

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_mdl_") and
    not any(c.data.startswith(p) for p in ["sa_mdl_tog_","sa_mdl_price_","sa_mdl_deact_","sa_mdl_del_"]))
@safe_handler
def cb_sa_mdl_detail(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    mid = int(call.data.split("_")[2]); bot.answer_callback_query(call.id)
    model = db_get_model_by_id(mid)
    if not model: bot.send_message(cid,texts["model_not_found"]); return
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT mp.price, mp.set_at, a.name AS aname FROM model_prices mp
        LEFT JOIN admins a ON a.id=mp.set_by WHERE mp.model_id=%s ORDER BY mp.set_at DESC LIMIT 5
    """, (mid,)); hist = cur.fetchall(); cur.close(); conn.close()
    hist_text = "\n".join(f"  • {int(p['price']):,}  {to_jalali(p['set_at'])}  {p['aname'] or '—'}" for p in hist)
    bot.send_message(cid,
        f"📦 {model['name']}  |  {'✅ فعال' if model['is_active'] else '❌ غیرفعال'}\n"
        f"قیمت فعلی: {int(model['price']):,}\n\n📜 تاریخچه:\n{hist_text or '—'}",
        reply_markup=sa_model_actions_kb(mid, model["is_active"]))

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_mdl_tog_"))
@safe_handler
def cb_mdl_toggle(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    mid = int(call.data.split("_")[3]); new = db_toggle_model(mid)
    bot.answer_callback_query(call.id, f"{'✅ فعال' if new else '❌ غیرفعال'}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_mdl_price_"))
@safe_handler
def cb_mdl_price(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    mid = int(call.data.split("_")[3]); bot.answer_callback_query(call.id)
    set_state(cid, S_SA_SET_PRICE, price_mid=mid)
    msg = bot.send_message(cid,"💲 قیمت جدید (تومان):")
    bot.register_next_step_handler(msg,_sa_set_price)

@safe_handler
def _sa_set_price(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_SET_PRICE: return
    try:
        price = int(m.text.strip().replace(",","").replace("،",""))
        if price<=0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid,texts["positive_number"]); bot.register_next_step_handler(msg,_sa_set_price); return
    d = get_state(cid)["data"]; admin = db_get_admin(cid)
    model = db_get_model_by_id(d["price_mid"])
    db_set_model_price(d["price_mid"], price, admin["id"]); clear_state(cid)
    logger.info("Model price updated | model_id=%s | name=%s | price=%s | by_admin=%s", d["price_mid"], model["name"], price, cid)
    bot.send_message(cid, f"✅ قیمت {model['name']} → {price:,} تومان")

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_mdl_deact_"))
@safe_handler
def cb_mdl_deact(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    mid = int(call.data.split("_")[3]); db_deactivate_model(mid)
    bot.answer_callback_query(call.id,"🗑 غیرفعال شد.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_mdl_del_") and
    not c.data.startswith("sa_mdl_del_ok_"))
@safe_handler
def cb_mdl_del(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    mid = int(call.data.split("_")[3])
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🔴 حذف کامل", callback_data=f"sa_mdl_del_ok_{mid}"),
           InlineKeyboardButton("❌ انصراف",    callback_data="sa_del_abort"))
    bot.answer_callback_query(call.id)
    bot.send_message(cid,texts["confirm_delete_model"], reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_mdl_del_ok_"))
@safe_handler
def cb_mdl_del_ok(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    mid = int(call.data.split("_")[4]); db_delete_model(mid)
    bot.answer_callback_query(call.id,"✅ حذف شد.")
    bot.send_message(cid,f"🗑 مدل #{mid} حذف شد.")

@bot.callback_query_handler(func=lambda c: c.data == "sa_add_model")
@safe_handler
def cb_sa_add_model(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    bot.answer_callback_query(call.id); set_state(cid, S_SA_MDL_NAME)
    msg = bot.send_message(cid,"➕ نام مدل جدید:"); bot.register_next_step_handler(msg,_sa_mdl_name)

@safe_handler
def _sa_mdl_name(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_MDL_NAME: return
    set_state(cid, S_SA_MDL_PRICE, mdl_name=m.text.strip())
    msg = bot.send_message(cid,f"قیمت اولیه {m.text.strip()} (تومان):")
    bot.register_next_step_handler(msg,_sa_mdl_price)

@safe_handler
def _sa_mdl_price(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_MDL_PRICE: return
    try:
        price = int(m.text.strip().replace(",","").replace("،",""))
        if price<=0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid,texts["positive_number"]); bot.register_next_step_handler(msg,_sa_mdl_price); return
    d = get_state(cid)["data"]; admin = db_get_admin(cid)
    mid = db_add_model(d["mdl_name"], price, admin["id"]); clear_state(cid)
    logger.info("Model added | model_id=%s | name=%s | price=%s | by_admin=%s", mid, d["mdl_name"], price, cid)
    bot.send_message(cid, f"✅ مدل {d['mdl_name']} (قیمت:{price:,}) اضافه شد. #{mid}")

# ════════════════════════════════════════════════════════════════
#  مدیریت ادمین‌ها
# ════════════════════════════════════════════════════════════════

@db_safe
def db_get_all_admins():
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM admins ORDER BY role DESC, name")
    rows = cur.fetchall(); cur.close(); conn.close(); return rows

@db_safe
def db_delete_admin(aid: int):
    """
    قبل از حذف ادمین، تمام ارجاعات FK به این ادمین رو NULL می‌کنه
    تا constraint violation نخوره (order_status_history.changed_by, transactions.created_by, ...).
    """
    conn = get_connection(); cur = conn.cursor()
    cur.execute("UPDATE order_status_history SET changed_by=NULL WHERE changed_by=%s", (aid,))
    cur.execute("UPDATE transactions       SET created_by=NULL  WHERE created_by=%s",  (aid,))
    cur.execute("UPDATE model_prices       SET set_by=NULL      WHERE set_by=%s",       (aid,))
    cur.execute("UPDATE installments       SET created_by=NULL  WHERE created_by=%s",   (aid,))
    cur.execute("UPDATE users              SET added_by=NULL    WHERE added_by=%s",      (aid,))
    cur.execute("DELETE FROM admins WHERE id=%s", (aid,))
    conn.commit(); cur.close(); conn.close()

def sa_admins_kb(admins):
    kb = InlineKeyboardMarkup(row_width=1)
    for a in admins:
        role_e = "🔑" if a["role"] == "superadmin" else "🛠"
        kb.add(InlineKeyboardButton(f"{role_e} {a['name']}", callback_data=f"sa_adm_detail_{a['id']}"))
    kb.add(InlineKeyboardButton("➕ افزودن ادمین", callback_data="sa_add_admin"))
    return kb

@bot.message_handler(func=lambda m: m.text == "👮 ادمین‌ها")
@safe_handler
def btn_sa_admins(message: Message):
    cid = message.chat.id
    if not _is_superadmin(cid): bot.send_message(cid,"⛔️"); return
    admins = db_get_all_admins()
    bot.send_message(cid, f"👮 ادمین‌ها ({len(admins)})", reply_markup=sa_admins_kb(admins))

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_adm_detail_"))
@safe_handler
def cb_sa_admin_detail(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    aid = int(call.data.split("_")[3]); bot.answer_callback_query(call.id)
    admin = db_get_admin_by_db_id(aid)
    if not admin: bot.send_message(cid,texts["admin_not_found"]); return
    kb = InlineKeyboardMarkup(row_width=1)
    if admin["cid"] != cid:  # نمیتونه خودشو حذف کنه
        kb.add(InlineKeyboardButton("🗑 حذف ادمین", callback_data=f"sa_adm_del_{aid}"))
    kb.add(InlineKeyboardButton("🔙 لیست ادمین‌ها", callback_data="sa_admins_list"))
    bot.send_message(cid,
        f"👮 {admin['name']}\nنقش: {'🔑 سوپرادمین' if admin['role']=='superadmin' else '🛠 ادمین عادی'}\n"
        f"CID: {admin['cid']}", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "sa_admins_list")
@safe_handler
def cb_sa_admins_list(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    bot.answer_callback_query(call.id)
    bot.send_message(cid, "👮 ادمین‌ها:", reply_markup=sa_admins_kb(db_get_all_admins()))

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_adm_del_"))
@safe_handler
def cb_sa_admin_del(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    aid = int(call.data.split("_")[3])
    admin = db_get_admin_by_db_id(aid)
    if admin and admin["cid"] == cid:
        bot.answer_callback_query(call.id,texts["cant_delete_self"]); return
    db_delete_admin(aid); bot.answer_callback_query(call.id,"✅ حذف شد.")
    bot.send_message(cid, f"🗑 ادمین {admin['name'] if admin else aid} حذف شد.")

@bot.callback_query_handler(func=lambda c: c.data == "sa_add_admin")
@safe_handler
def cb_sa_add_admin(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    bot.answer_callback_query(call.id); set_state(cid, S_SA_ADD_ADM_CID)
    msg = bot.send_message(cid,"➕ افزودن ادمین جدید\nChat ID:")
    bot.register_next_step_handler(msg,_sa_adm_cid)

@safe_handler
def _sa_adm_cid(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_ADD_ADM_CID: return
    try: new_cid = int(m.text.strip())
    except ValueError:
        msg = bot.send_message(cid,"❌ Chat ID عدد:"); bot.register_next_step_handler(msg,_sa_adm_cid); return
    set_state(cid, S_SA_ADD_ADM_NAME, adm_cid=new_cid)
    msg = bot.send_message(cid,"نام ادمین:"); bot.register_next_step_handler(msg,_sa_adm_name)

@safe_handler
def _sa_adm_name(m: Message):
    cid = m.chat.id
    if get_state(cid).get("step") != S_SA_ADD_ADM_NAME: return
    set_state(cid, S_SA_ADD_ADM_ROLE, adm_name=m.text.strip())
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("🛠 ادمین عادی",  callback_data="sa_adm_role_admin"),
           InlineKeyboardButton("🔑 سوپرادمین",   callback_data="sa_adm_role_superadmin"))
    bot.send_message(cid,"نقش ادمین:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_adm_role_"))
@safe_handler
def cb_sa_adm_role(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid): bot.answer_callback_query(call.id,"⛔️"); return
    role = call.data.split("_")[3]; bot.answer_callback_query(call.id)
    d = get_state(cid)["data"]
    ok = db_add_admin(d["adm_cid"], d["adm_name"], role); clear_state(cid)
    if ok:
        bot.send_message(cid, f"✅ ادمین {d['adm_name']} ({role}) اضافه شد.")
        try: bot.send_message(d["adm_cid"],texts["admin_added_notification"])
        except: pass
    else:
        bot.send_message(cid,texts["admin_already_exists"])

# ════════════════════════════════════════════════════════════════
#  سوپرادمین - گزارش مالی
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📊 گزارش مالی")
@safe_handler
def btn_sa_report(message: Message):
    cid = message.chat.id
    if not _is_superadmin(cid): bot.send_message(cid,"⛔️"); return
    conn = get_connection(); cur = conn.cursor(dictionary=True)
    cur.execute("SELECT SUM(current_month_debt) AS cmd, SUM(previous_debt) AS prv FROM user_finance")
    fin = cur.fetchone()
    cur.execute("SELECT COUNT(*) AS cnt FROM orders WHERE status='pending'"); po = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_banned=TRUE"); bn = cur.fetchone()["cnt"]
    cur.execute("SELECT COUNT(*) AS cnt FROM transactions WHERE type='payment' AND status='pending'"); pp = cur.fetchone()["cnt"]
    cur.execute("SELECT COALESCE(SUM(total_price),0) AS t FROM orders WHERE MONTH(created_at)=MONTH(NOW()) AND YEAR(created_at)=YEAR(NOW())")
    mo = int(cur.fetchone()["t"]); cur.close(); conn.close()
    cmd = int(fin["cmd"] or 0); prv = int(fin["prv"] or 0)
    bot.send_message(cid,
        f"📊 گزارش مالی\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 بدهی ماه جاری: {cmd:,}\n"
        f"💳 بدهی قبلی:     {prv:,}\n"
        f"📊 جمع کل:        {cmd+prv:,}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 سفارشات این ماه: {mo:,}\n"
        f"⏳ سفارش pending:   {po}\n"
        f"⏳ پرداخت pending:  {pp}\n"
        f"🚫 مسدودین:         {bn}")

# ════════════════════════════════════════════════════════════════
#  سوپرادمین - ارسال پیام همگانی
# ════════════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📣 ارسال پیام همگانی")
@safe_handler
def btn_sa_broadcast(message: Message):
    cid = message.chat.id
    if not _is_superadmin(cid): bot.send_message(cid,"⛔️"); return
    set_state(cid, S_SA_BROADCAST)
    msg = bot.send_message(cid,texts["broadcast_prompt"])
    bot.register_next_step_handler(msg, _sa_broadcast_recv)

@safe_handler
def _sa_broadcast_recv(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != S_SA_BROADCAST: return
    text = message.text.strip(); clear_state(cid)
    users = db_get_all_users_summary()
    logger.info("Broadcast started | total_users=%s | by_admin=%s", len(users), cid)
    bot.send_message(cid, f"⏳ ارسال به {len(users)} کاربر...")
    threading.Thread(target=_do_broadcast, args=(cid, users, text), daemon=True).start()

def _do_broadcast(cid: int, users: list, text: str):
    sent = 0; failed = 0
    for u in users:
        try:
            bot.send_message(u["cid"], f"📣 پیام از ادمین:\n\n{text}")
            sent += 1
            time.sleep(0.05)
        except Exception:
            failed += 1
    bot.send_message(cid, f"✅ ارسال تموم شد.\nموفق: {sent}  |  ناموفق: {failed}")

# ════════════════════════════════════════════════════════════════
#  SCHEDULER - بن اتوماتیک (شمسی)
# ════════════════════════════════════════════════════════════════

def _jalali_deadline() -> datetime:
    now_j = jdatetime.datetime.now()
    month = now_j.month; year = now_j.year
    days = 31 if month <= 6 else (30 if month <= 11 else
           (30 if jdatetime.datetime(year,1,1).isleap() else 29))
    end_j = jdatetime.datetime(year, month, days, 0, 0, 0)
    deadline_j = end_j + jdatetime.timedelta(days=5)
    return deadline_j.togregorian().replace(hour=2, minute=0, second=0)

def _schedule_next(target: datetime):
    wait = max((target - datetime.now()).total_seconds(), 0)
    next_j = jdatetime.datetime.fromgregorian(datetime=target)
    print(f"[SCHEDULER] Next run: {next_j.strftime('%Y/%m/%d %H:%M')} ({wait/3600:.1f}h)")
    sched_logger.info("Next scheduled run: %s (in %.1f hours)", next_j.strftime('%Y/%m/%d %H:%M'), wait/3600)
    t = threading.Timer(wait, _run_ban_check)
    t.daemon = True; t.start()

def _run_ban_check():
    """
    این تابع از threading.Timer صدا زده می‌شه (نه از telebot)،
    پس باید خودش try/except داشته باشه وگرنه کل thread بدون پیام خطا می‌میره
    و scheduler ماه بعد دیگه زمان‌بندی نمی‌شه.
    """
    try:
        _run_ban_check_inner()
    except Exception as e:
        print(f"[SCHEDULER ERROR] {type(e).__name__}: {e}")
        sched_logger.exception("Error in auto-ban check execution: %s: %s", type(e).__name__, e)
        traceback.print_exc()
        for admin_cid in ADMIN_IDS:
            try:
                bot.send_message(admin_cid,
                    f"⚠️ خطا در اجرای بررسی بن خودکار\n{type(e).__name__}: {str(e)[:300]}")
            except Exception:
                pass
        # حتی اگه خطا خورد، باید ماه بعد دوباره تلاش کنه
        try:
            _schedule_next_month()
        except Exception:
            pass


def _run_ban_check_inner():
    now_j = jdatetime.datetime.now(); month_name = now_j.strftime("%B %Y")
    print(f"[SCHEDULER] Auto-ban check — {now_j.strftime('%Y/%m/%d %H:%M')}")
    sched_logger.info("Starting auto-ban check — %s", now_j.strftime('%Y/%m/%d %H:%M'))
    debtors = db_get_users_with_current_debt()
    if not debtors:
        print("[SCHEDULER] No debtors found.")
        sched_logger.info("No debtors found.")
        _schedule_next_month(); return
    banned = []
    skipped = 0
    for u in debtors:
        # Re-verify debt is still unpaid (prevents banning users who paid between fetch and loop)
        fresh_fin = db_get_user_finance(u["id"])
        if not fresh_fin or int(fresh_fin.get("current_month_debt", 0)) <= 0:
            sched_logger.info("Skipping user id=%s (debt cleared before ban)", u["id"])
            skipped += 1
            continue
        # Also skip if already banned (avoid duplicate ban_reason overwrite)
        fresh_user = db_get_user_by_db_id(u["id"])
        if fresh_user and fresh_user.get("is_banned"):
            sched_logger.info("Skipping user id=%s (already banned)", u["id"])
            skipped += 1
            continue
        reason = f"عدم تسویه {month_name} — {int(fresh_fin['current_month_debt']):,} تومان"
        db_ban_user(u["id"], reason); banned.append(u)
        sched_logger.info("User banned: id=%s | name=%s | debt=%s Toman",
                          u["id"], u.get("name", "—"), int(fresh_fin["current_month_debt"]))
        try: bot.send_message(u["cid"],
                 f"🚫 حساب مسدود شد.\nبدهی ماه {month_name}: {int(fresh_fin['current_month_debt']):,} تومان\nبا ادمین تماس بگیرید.")
        except: pass
    if skipped:
        sched_logger.info("Skipped %d user(s) (paid or already banned before loop)", skipped)
    report = f"🔴 بن اتوماتیک — {month_name}\nتعداد: {len(banned)}\n\n"
    report += "\n".join(f"• {u['name']}  {int(u['current_month_debt']):,}ت" for u in banned)
    _notify_admins(report)
    print(f"[SCHEDULER] {len(banned)} user(s) banned.")
    sched_logger.info("Auto-ban check finished. Total banned: %d", len(banned))
    _schedule_next_month()

def _schedule_next_month():
    now_j = jdatetime.datetime.now(); month = now_j.month; year = now_j.year
    nm = 1 if month==12 else month+1; ny = year+1 if month==12 else year
    days = 31 if nm<=6 else (30 if nm<=11 else (30 if jdatetime.datetime(ny,1,1).isleap() else 29))
    next_deadline = jdatetime.datetime(ny,nm,days,0,0,0)+jdatetime.timedelta(days=5)
    _schedule_next(next_deadline.togregorian().replace(hour=2,minute=0,second=0))

def start_scheduler():
    deadline = _jalali_deadline()
    if datetime.now() >= deadline:
        print("[SCHEDULER] Deadline already passed — running immediately...")
        t = threading.Timer(10, _run_ban_check); t.daemon = True; t.start()
    else:
        _schedule_next(deadline)

# ════════════════════════════════════════════════════════════════
#  LISTENER & RUN
# ════════════════════════════════════════════════════════════════

def info_listener(messages):
    for m in messages:
        print(f"[MSG] {m.chat.username or m.chat.id}: {m.text or m.content_type}")
        logger.info("MSG | user=%s | cid=%s | content=%s",
                    m.chat.username or "—", m.chat.id, m.text or m.content_type)

bot.set_update_listener(info_listener)

print("Bot started...")
logger.info("=" * 60)
logger.info("Bot started successfully")

try:
    start_scheduler()
except Exception as e:
    print(f"[STARTUP ERROR] Scheduler startup failed: {e}")
    sched_logger.exception("Scheduler startup failed: %s", e)
    traceback.print_exc()
    print("[STARTUP] Bot running without auto-ban scheduler.")

# infinity_polling خودش retry داخلی داره، ولی برای اطمینان بیشتر
# (مثلاً قطعی کامل شبکه که polling رو از کار بندازه) یه لایه‌ی محافظ بیرونی هم می‌ذاریم
while True:
    try:
        bot.infinity_polling(timeout=20, long_polling_timeout=20)
    except Exception as e:
        print(f"[POLLING ERROR] {type(e).__name__}: {e}")
        err_logger.exception("Polling error: %s: %s", type(e).__name__, e)
        traceback.print_exc()
        print("[POLLING] Retrying in 5 seconds...")
        logger.info("Retrying polling in 5 seconds...")
        time.sleep(5)