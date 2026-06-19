import telebot
import mysql.connector
import threading
import jdatetime
from datetime import datetime, timedelta
from telebot.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardButton, InlineKeyboardMarkup,
    Message, CallbackQuery
)
from config import BOT_TOKEN, DATABASE_CONFIG, DB_NAME, ADMIN_IDS

telebot.apihelper.API_URL = "http://tapi.bale.ai/bot{0}/{1}"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════

def get_connection():
    return mysql.connector.connect(database=DB_NAME, **DATABASE_CONFIG)


def db_get_user(cid: int):
    """کاربر رو با chat_id پیدا می‌کنه. اگه نبود None برمی‌گردونه."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE cid = %s", (cid,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def db_get_active_models():
    """همه مدل‌های فعال + آخرین قیمتشون رو برمی‌گردونه."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.id, m.name, mp.price
        FROM models m
        JOIN model_prices mp ON mp.id = (
            SELECT id FROM model_prices
            WHERE model_id = m.id
            ORDER BY set_at DESC
            LIMIT 1
        )
        WHERE m.is_active = TRUE
        ORDER BY m.name
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def db_get_model_by_id(model_id: int):
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.id, m.name, mp.price
        FROM models m
        JOIN model_prices mp ON mp.id = (
            SELECT id FROM model_prices
            WHERE model_id = m.id
            ORDER BY set_at DESC
            LIMIT 1
        )
        WHERE m.id = %s
    """, (model_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def db_get_user_finance(user_db_id: int):
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM user_finance WHERE user_id = %s", (user_db_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def db_create_order(user_db_id: int, total_price: int,
                    fabric: str, delivery_date: str, note: str,
                    items: list) -> int:
    """
    سفارش + آیتم‌ها رو ذخیره می‌کنه.
    items = [{"model_id": .., "quantity": .., "unit_price": .., "line_total": ..}, ...]
    بدهی ماه جاری کاربر هم اضافه می‌شه.
    برمی‌گردونه order_id.
    """
    conn = get_connection()
    cur = conn.cursor()

    # ثبت سفارش
    cur.execute("""
        INSERT INTO orders (user_id, total_price, fabric, delivery_date, note)
        VALUES (%s, %s, %s, %s, %s)
    """, (user_db_id, total_price, fabric, delivery_date, note))
    order_id = cur.lastrowid

    # ثبت آیتم‌ها
    for item in items:
        cur.execute("""
            INSERT INTO order_items (order_id, model_id, quantity, unit_price, line_total)
            VALUES (%s, %s, %s, %s, %s)
        """, (order_id, item["model_id"], item["quantity"],
              item["unit_price"], item["line_total"]))

    # اضافه کردن به بدهی ماه جاری
    cur.execute("""
        INSERT INTO user_finance (user_id, current_month_debt)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE current_month_debt = current_month_debt + %s
    """, (user_db_id, total_price, total_price))

    # ثبت تراکنش
    cur.execute("""
        INSERT INTO transactions (user_id, type, amount, description)
        VALUES (%s, 'order', %s, %s)
    """, (user_db_id, total_price, f"سفارش #{order_id}"))

    conn.commit()
    cur.close(); conn.close()
    return order_id


def db_update_order_status(order_id: int, status: str,
                            rejection_reason: str = None):
    conn = get_connection()
    cur = conn.cursor()
    if rejection_reason:
        cur.execute("""
            UPDATE orders SET status=%s, rejection_reason=%s
            WHERE id=%s
        """, (status, rejection_reason, order_id))
    else:
        cur.execute("UPDATE orders SET status=%s WHERE id=%s",
                    (status, order_id))
    conn.commit()
    cur.close(); conn.close()


def db_get_order_with_items(order_id: int):
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    order = cur.fetchone()
    if order:
        cur.execute("""
            SELECT oi.*, m.name AS model_name
            FROM order_items oi
            JOIN models m ON m.id = oi.model_id
            WHERE oi.order_id = %s
        """, (order_id,))
        order["items"] = cur.fetchall()
    cur.close(); conn.close()
    return order


def db_get_user_by_db_id(user_db_id: int):
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE id = %s", (user_db_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


# ── توابع مالی ───────────────────────────────────────────

def db_get_transactions(user_db_id: int, limit: int = 20) -> list:
    """آخرین تراکنش‌های کاربر."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT t.*, a.name AS admin_name
        FROM transactions t
        LEFT JOIN admins a ON a.id = t.created_by
        WHERE t.user_id = %s
        ORDER BY t.created_at DESC
        LIMIT %s
    """, (user_db_id, limit))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def db_get_installments(user_db_id: int) -> list:
    """اقساط فعال کاربر (پرداخت نشده کامل)."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT i.*, a.name AS admin_name
        FROM installments i
        LEFT JOIN admins a ON a.id = i.created_by
        WHERE i.user_id = %s AND i.paid_amount < i.total_amount
        ORDER BY i.created_at DESC
    """, (user_db_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def db_get_pending_payments(user_db_id: int) -> list:
    """پرداخت‌های در انتظار تایید ادمین."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM transactions
        WHERE user_id = %s AND type = 'payment' AND status = 'pending'
        ORDER BY created_at DESC
    """, (user_db_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def db_create_payment_request(user_db_id: int, amount: int,
                               file_id: str) -> int:
    """
    ثبت درخواست پرداخت (در انتظار تایید ادمین).
    status='pending' - هنوز از بدهی کم نشده.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO transactions (user_id, type, amount, description, status, receipt_file_id)
        VALUES (%s, 'payment', %s, 'درخواست پرداخت در انتظار تایید', 'pending', %s)
    """, (user_db_id, amount, file_id))
    tx_id = cur.lastrowid
    conn.commit()
    cur.close(); conn.close()
    return tx_id


def db_approve_payment(tx_id: int, admin_db_id: int) -> dict:
    """
    تایید پرداخت توسط ادمین:
      1. اول از current_month_debt کم میشه
      2. مازاد از previous_debt کم میشه
      3. پرداخت بیشتر از کل بدهی مجاز نیست (قبلاً validate شده)
    برمی‌گردونه: {paid_current, paid_previous, user_db_id}
    """
    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    # اطلاعات تراکنش
    cur.execute("SELECT * FROM transactions WHERE id = %s", (tx_id,))
    tx = cur.fetchone()
    if not tx or tx["status"] != "pending":
        cur.close(); conn.close()
        return None

    user_db_id = tx["user_id"]
    amount = int(tx["amount"])

    # وضعیت مالی کاربر (با قفل برای جلوگیری از race condition)
    cur.execute("""
        SELECT * FROM user_finance WHERE user_id = %s FOR UPDATE
    """, (user_db_id,))
    finance = cur.fetchone()

    cmd = int(finance["current_month_debt"]) if finance else 0
    prev = int(finance["previous_debt"]) if finance else 0

    # محاسبه توزیع پرداخت
    paid_current = min(amount, cmd)
    remaining = amount - paid_current
    paid_previous = min(remaining, prev)

    new_cmd  = cmd  - paid_current
    new_prev = prev - paid_previous

    # بروزرسانی بدهی
    cur.execute("""
        UPDATE user_finance
        SET current_month_debt = %s, previous_debt = %s
        WHERE user_id = %s
    """, (new_cmd, new_prev, user_db_id))

    # تایید تراکنش
    cur.execute("""
        UPDATE transactions
        SET status = 'approved', created_by = %s,
            description = %s
        WHERE id = %s
    """, (admin_db_id,
          f"پرداخت تایید شد | ماه جاری: {paid_current:,} | قبلی: {paid_previous:,}",
          tx_id))

    conn.commit()
    cur.close(); conn.close()

    return {
        "user_db_id":    user_db_id,
        "amount":        amount,
        "paid_current":  paid_current,
        "paid_previous": paid_previous,
        "new_cmd":       new_cmd,
        "new_prev":      new_prev,
    }


def db_reject_payment(tx_id: int, admin_db_id: int, reason: str):
    """رد پرداخت توسط ادمین - بدهی تغییری نمی‌کنه."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE transactions
        SET status = 'rejected', created_by = %s, description = %s
        WHERE id = %s AND status = 'pending'
    """, (admin_db_id, f"رد شد: {reason}", tx_id))
    conn.commit()
    cur.close(); conn.close()


def db_get_transaction_by_id(tx_id: int) -> dict:
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM transactions WHERE id = %s", (tx_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


# ── توابع سوپرادمین ──────────────────────────────────────

def db_get_admin(cid: int) -> dict | None:
    """ادمین رو با chat_id پیدا می‌کنه."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM admins WHERE cid = %s", (cid,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def db_get_all_users_summary() -> list:
    """لیست کاربران با خلاصه مالی برای نمایش در پنل."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT u.id, u.cid, u.name, u.username, u.is_banned,
               COALESCE(uf.current_month_debt, 0) AS current_month_debt,
               COALESCE(uf.previous_debt, 0)      AS previous_debt
        FROM users u
        LEFT JOIN user_finance uf ON uf.user_id = u.id
        ORDER BY u.name
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def db_get_user_full_report(user_db_id: int) -> dict:
    """گزارش کامل یک کاربر: مشخصات + مالی + سفارشات + تراکنش‌ها + اقساط."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT * FROM users WHERE id = %s", (user_db_id,))
    user = cur.fetchone()
    if not user:
        cur.close(); conn.close()
        return None

    cur.execute("SELECT * FROM user_finance WHERE user_id = %s", (user_db_id,))
    finance = cur.fetchone()

    cur.execute("""
        SELECT o.id, o.total_price, o.fabric, o.delivery_date,
               o.status, o.created_at,
               COUNT(oi.id) AS item_count
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.id
        WHERE o.user_id = %s
        GROUP BY o.id
        ORDER BY o.created_at DESC
        LIMIT 10
    """, (user_db_id,))
    orders = cur.fetchall()

    cur.execute("""
        SELECT * FROM transactions
        WHERE user_id = %s
        ORDER BY created_at DESC LIMIT 15
    """, (user_db_id,))
    transactions = cur.fetchall()

    cur.execute("""
        SELECT * FROM installments
        WHERE user_id = %s
        ORDER BY created_at DESC
    """, (user_db_id,))
    installments = cur.fetchall()

    cur.close(); conn.close()
    return {
        "user":         user,
        "finance":      finance,
        "orders":       orders,
        "transactions": transactions,
        "installments": installments,
    }


def db_add_user(cid: int, name: str, username: str, added_by: int) -> bool:
    """کاربر جدید اضافه می‌کنه. اگه قبلاً بود False برمی‌گردونه."""
    conn = get_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (cid, name, username, added_by)
            VALUES (%s, %s, %s, %s)
        """, (cid, name, username, added_by))
        user_id = cur.lastrowid
        cur.execute("""
            INSERT INTO user_finance (user_id, current_month_debt, previous_debt)
            VALUES (%s, 0, 0)
        """, (user_id,))
        conn.commit()
        return True
    except mysql.connector.IntegrityError:
        return False
    finally:
        cur.close(); conn.close()


def db_unban_user(user_db_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users SET is_banned = FALSE, ban_reason = NULL
        WHERE id = %s
    """, (user_db_id,))
    conn.commit()
    cur.close(); conn.close()


def db_transfer_debt_to_previous(user_db_id: int, admin_db_id: int) -> int:
    """
    بدهی ماه جاری رو به بدهی قبلی منتقل می‌کنه و پنل کاربر رو باز می‌کنه.
    برمی‌گردونه مبلغ منتقل‌شده.
    """
    conn = get_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT * FROM user_finance WHERE user_id = %s FOR UPDATE
    """, (user_db_id,))
    finance = cur.fetchone()
    if not finance:
        cur.close(); conn.close()
        return 0

    amount = int(finance["current_month_debt"])
    if amount <= 0:
        cur.close(); conn.close()
        return 0

    cur.execute("""
        UPDATE user_finance
        SET previous_debt      = previous_debt + %s,
            current_month_debt = 0
        WHERE user_id = %s
    """, (amount, user_db_id))

    # آنبن کاربر
    cur.execute("""
        UPDATE users SET is_banned = FALSE, ban_reason = NULL
        WHERE id = %s
    """, (user_db_id,))

    # ثبت تراکنش
    cur.execute("""
        INSERT INTO transactions (user_id, type, amount, description, created_by)
        VALUES (%s, 'month_transfer', %s, 'انتقال بدهی ماه جاری به بدهی قبلی', %s)
    """, (user_db_id, amount, admin_db_id))

    conn.commit()
    cur.close(); conn.close()
    return amount


def db_add_manual_debt(user_db_id: int, amount: int,
                        description: str, admin_db_id: int):
    """اضافه کردن بدهی دستی به ماه جاری."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_finance (user_id, current_month_debt)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE current_month_debt = current_month_debt + %s
    """, (user_db_id, amount, amount))
    cur.execute("""
        INSERT INTO transactions (user_id, type, amount, description, created_by)
        VALUES (%s, 'debt_added', %s, %s, %s)
    """, (user_db_id, amount, description, admin_db_id))
    conn.commit()
    cur.close(); conn.close()


def db_add_model(name: str, price: int, admin_db_id: int) -> int:
    """مدل جدید + قیمت اولیه. برمی‌گردونه model_id."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO models (name) VALUES (%s)", (name,))
    model_id = cur.lastrowid
    cur.execute("""
        INSERT INTO model_prices (model_id, price, set_by)
        VALUES (%s, %s, %s)
    """, (model_id, price, admin_db_id))
    conn.commit()
    cur.close(); conn.close()
    return model_id


def db_toggle_model(model_id: int) -> bool:
    """فعال/غیرفعال کردن مدل. برمی‌گردونه وضعیت جدید."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT is_active FROM models WHERE id = %s", (model_id,))
    row = cur.fetchone()
    new_status = not row["is_active"]
    cur.execute("UPDATE models SET is_active = %s WHERE id = %s",
                (new_status, model_id))
    conn.commit()
    cur.close(); conn.close()
    return new_status


def db_set_model_price(model_id: int, price: int, admin_db_id: int):
    """قیمت جدید برای مدل."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO model_prices (model_id, price, set_by)
        VALUES (%s, %s, %s)
    """, (model_id, price, admin_db_id))
    conn.commit()
    cur.close(); conn.close()


def db_add_installment(user_db_id: int, total_amount: int,
                        num_installments: int, due_dates: list,
                        admin_db_id: int) -> int:
    """قسط‌بندی جدید برای کاربر."""
    import json
    per = total_amount // num_installments
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO installments
            (user_id, total_amount, num_installments, per_installment, due_dates, created_by)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (user_db_id, total_amount, num_installments, per,
          json.dumps(due_dates), admin_db_id))
    inst_id = cur.lastrowid
    conn.commit()
    cur.close(); conn.close()
    return inst_id


def db_get_all_orders_pending() -> list:
    """سفارشات در انتظار تایید برای ادمین عادی (بدون قیمت)."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT o.id, o.fabric, o.delivery_date, o.status, o.note, o.created_at,
               u.name AS user_name
        FROM orders o
        JOIN users u ON u.id = o.user_id
        WHERE o.status = 'pending'
        ORDER BY o.created_at ASC
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def db_get_order_items_no_price(order_id: int) -> list:
    """آیتم‌های سفارش بدون قیمت - برای ادمین عادی."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.name AS model_name, oi.quantity
        FROM order_items oi
        JOIN models m ON m.id = oi.model_id
        WHERE oi.order_id = %s
    """, (order_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


# ════════════════════════════════════════════════════════
#  STATE MANAGEMENT
#  user_states[cid] = {"step": "...", "data": {...}}
# ════════════════════════════════════════════════════════

user_states: dict = {}

STEP_SELECT_MODEL   = "select_model"
STEP_ENTER_QUANTITY = "enter_quantity"
STEP_ADD_MORE       = "add_more"
STEP_ENTER_FABRIC   = "enter_fabric"
STEP_ENTER_DATE     = "enter_date"
STEP_ENTER_NOTE     = "enter_note"
STEP_CONFIRM        = "confirm"

# مالی
STEP_PAY_ENTER_AMOUNT = "pay_enter_amount"
STEP_PAY_SEND_RECEIPT = "pay_send_receipt"
STEP_PAY_CONFIRM      = "pay_confirm"

# سوپرادمین
STEP_SA_ADD_USER_CID      = "sa_add_user_cid"
STEP_SA_ADD_USER_NAME     = "sa_add_user_name"
STEP_SA_ADD_USER_USERNAME = "sa_add_user_username"
STEP_SA_ADD_MODEL_NAME    = "sa_add_model_name"
STEP_SA_ADD_MODEL_PRICE   = "sa_add_model_price"
STEP_SA_SET_PRICE_AMOUNT  = "sa_set_price_amount"
STEP_SA_ADD_DEBT_AMOUNT   = "sa_add_debt_amount"
STEP_SA_ADD_DEBT_DESC     = "sa_add_debt_desc"
STEP_SA_INST_AMOUNT       = "sa_inst_amount"
STEP_SA_INST_COUNT        = "sa_inst_count"
STEP_SA_INST_DATES        = "sa_inst_dates"


def get_state(cid: int) -> dict:
    return user_states.get(cid, {})

def set_state(cid: int, step: str, **kwargs):
    if cid not in user_states:
        user_states[cid] = {"step": step, "data": {}}
    user_states[cid]["step"] = step
    user_states[cid]["data"].update(kwargs)

def clear_state(cid: int):
    user_states.pop(cid, None)


# ════════════════════════════════════════════════════════
#  KEYBOARDS
# ════════════════════════════════════════════════════════

def main_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("📦 ثبت سفارش"),
        KeyboardButton("💰 بخش مالی"),
    )
    kb.add(
        KeyboardButton("📋 سفارش‌های من"),
        KeyboardButton("👤 پروفایل من"),
    )
    return kb


def models_kb(models: list, selected_ids: list) -> InlineKeyboardMarkup:
    """لیست مدل‌ها - مدل‌های انتخاب‌شده با ✅ نمایش داده میشن."""
    kb = InlineKeyboardMarkup(row_width=2)
    buttons = []
    for m in models:
        label = f"{'✅ ' if m['id'] in selected_ids else ''}{m['name']} ({m['price']:,})"
        buttons.append(InlineKeyboardButton(label, callback_data=f"model_{m['id']}"))
    kb.add(*buttons)
    kb.add(InlineKeyboardButton("✅ انتخاب مدل‌ها تمومه، ادامه", callback_data="models_done"))
    return kb


def quantity_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=3)
    # دکمه‌های سریع برای تعداد
    kb.add(
        InlineKeyboardButton("۱", callback_data="qty_1"),
        InlineKeyboardButton("۵", callback_data="qty_5"),
        InlineKeyboardButton("۱۰", callback_data="qty_10"),
        InlineKeyboardButton("۲۰", callback_data="qty_20"),
        InlineKeyboardButton("۵۰", callback_data="qty_50"),
        InlineKeyboardButton("✏️ دستی", callback_data="qty_manual"),
    )
    return kb


def date_kb() -> InlineKeyboardMarkup:
    """پیشنهاد تاریخ‌های تحویل."""
    kb = InlineKeyboardMarkup(row_width=2)
    today = datetime.now()
    options = [7, 14, 21, 30]
    for d in options:
        dt = today + timedelta(days=d)
        label = dt.strftime("%Y/%m/%d") + f"  ({d} روز دیگه)"
        kb.add(InlineKeyboardButton(label, callback_data=f"date_{dt.strftime('%Y-%m-%d')}"))
    kb.add(InlineKeyboardButton("✏️ تاریخ دلخواه (YYYY-MM-DD)", callback_data="date_manual"))
    return kb


def order_confirm_kb(order_id_temp: str = "new") -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ تایید و ارسال سفارش", callback_data="order_confirm"),
        InlineKeyboardButton("❌ انصراف", callback_data="order_cancel"),
    )
    return kb


def admin_order_kb(order_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ تایید سفارش", callback_data=f"admin_approve_{order_id}"),
        InlineKeyboardButton("❌ رد سفارش",    callback_data=f"admin_reject_{order_id}"),
    )
    return kb


def finance_main_kb() -> InlineKeyboardMarkup:
    """منوی اصلی بخش مالی - inline."""
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("💳 ثبت پرداخت جدید",       callback_data="fin_new_payment"),
        InlineKeyboardButton("📜 تاریخچه تراکنش‌ها",     callback_data="fin_history"),
        InlineKeyboardButton("📋 اقساط من",               callback_data="fin_installments"),
        InlineKeyboardButton("⏳ پرداخت‌های در انتظار",   callback_data="fin_pending"),
    )
    return kb


def pay_confirm_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ ارسال درخواست",  callback_data="pay_submit"),
        InlineKeyboardButton("❌ انصراف",          callback_data="pay_cancel"),
    )
    return kb


def admin_payment_kb(tx_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ تایید پرداخت", callback_data=f"admin_pay_approve_{tx_id}"),
        InlineKeyboardButton("❌ رد پرداخت",    callback_data=f"admin_pay_reject_{tx_id}"),
    )
    return kb


# ── کیبوردهای ادمین عادی ─────────────────────────────────

def admin_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    kb.add(KeyboardButton("📋 سفارشات در انتظار"))
    return kb


def admin_order_list_kb(orders: list) -> InlineKeyboardMarkup:
    """لیست سفارشات pending برای ادمین عادی."""
    kb = InlineKeyboardMarkup(row_width=1)
    for o in orders:
        label = f"#{o['id']} — {o['user_name']} — {o['created_at'].strftime('%m/%d')}"
        kb.add(InlineKeyboardButton(label, callback_data=f"adm_order_{o['id']}"))
    return kb


def admin_order_status_kb(order_id: int) -> InlineKeyboardMarkup:
    """تغییر وضعیت سفارش توسط ادمین عادی."""
    kb = InlineKeyboardMarkup(row_width=2)
    statuses = [
        ("✅ تایید",          "approved"),
        ("🔧 در حال تولید",   "producing"),
        ("📦 آماده تحویل",    "ready"),
        ("🚚 تحویل داده شد",  "delivered"),
        ("❌ رد سفارش",       "rejected"),
    ]
    for label, status in statuses:
        kb.add(InlineKeyboardButton(label, callback_data=f"adm_setstatus_{order_id}_{status}"))
    return kb


# ── کیبوردهای سوپرادمین ──────────────────────────────────

def superadmin_menu_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        KeyboardButton("👥 کاربران"),
        KeyboardButton("📦 مدل‌ها"),
    )
    kb.add(
        KeyboardButton("📋 سفارشات در انتظار"),
        KeyboardButton("📊 گزارش مالی"),
    )
    return kb


def sa_users_list_kb(users: list) -> InlineKeyboardMarkup:
    """لیست کاربران با خلاصه بدهی."""
    kb = InlineKeyboardMarkup(row_width=1)
    for u in users:
        debt = int(u["current_month_debt"]) + int(u["previous_debt"])
        banned = "🚫" if u["is_banned"] else "✅"
        label = f"{banned} {u['name']}  |  بدهی: {debt:,}"
        kb.add(InlineKeyboardButton(label, callback_data=f"sa_user_{u['id']}"))
    kb.add(InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="sa_add_user"))
    return kb


def sa_user_actions_kb(user_db_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    ban_label = "✅ رفع مسدودی" if is_banned else "🚫 مسدود کردن"
    ban_cb    = f"sa_unban_{user_db_id}" if is_banned else f"sa_ban_{user_db_id}"
    kb.add(
        InlineKeyboardButton(ban_label,                  callback_data=ban_cb),
        InlineKeyboardButton("🔄 انتقال بدهی به قبلی",  callback_data=f"sa_transfer_{user_db_id}"),
    )
    kb.add(
        InlineKeyboardButton("➕ افزودن بدهی دستی",     callback_data=f"sa_adddebt_{user_db_id}"),
        InlineKeyboardButton("📋 قسط‌بندی",             callback_data=f"sa_inst_{user_db_id}"),
    )
    kb.add(InlineKeyboardButton("🔙 بازگشت به لیست",   callback_data="sa_users_list"))
    return kb


def sa_models_kb(models: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for m in models:
        status = "✅" if m["is_active"] else "❌"
        label  = f"{status} {m['name']}  |  {m['price']:,} تومان"
        kb.add(InlineKeyboardButton(label, callback_data=f"sa_model_{m['id']}"))
    kb.add(InlineKeyboardButton("➕ افزودن مدل جدید", callback_data="sa_add_model"))
    return kb


def sa_model_actions_kb(model_id: int, is_active: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    toggle_label = "❌ غیرفعال کن" if is_active else "✅ فعال کن"
    kb.add(
        InlineKeyboardButton(toggle_label,          callback_data=f"sa_modeltoggle_{model_id}"),
        InlineKeyboardButton("💲 تغییر قیمت",       callback_data=f"sa_modelprice_{model_id}"),
    )
    kb.add(InlineKeyboardButton("🔙 بازگشت به مدل‌ها", callback_data="sa_models_list"))
    return kb


# ════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════

def format_order_summary(items: list, fabric: str,
                          delivery_date: str, note: str) -> str:
    """متن خلاصه فاکتور."""
    lines = ["🧾 <b>خلاصه سفارش:</b>\n"]
    total = 0
    for i, item in enumerate(items, 1):
        line_t = item["quantity"] * item["unit_price"]
        total += line_t
        lines.append(
            f"{i}. <b>{item['model_name']}</b>\n"
            f"   تعداد: {item['quantity']}  |  قیمت واحد: {item['unit_price']:,}\n"
            f"   جمع: {line_t:,} تومان"
        )
    lines.append(f"\n💵 <b>جمع کل: {total:,} تومان</b>")
    lines.append(f"🪡 پارچه: {fabric}")
    lines.append(f"📅 تاریخ تحویل: {delivery_date}")
    if note:
        lines.append(f"📝 یادداشت: {note}")
    return "\n".join(lines)


def notify_admins_new_order(order_id: int, user_cid: int, summary_text: str):
    """به همه ادمین‌ها پیام می‌فرسته."""
    text = (
        f"🔔 <b>سفارش جدید #{order_id}</b>\n"
        f"از کاربر: <code>{user_cid}</code>\n\n"
        f"{summary_text}"
    )
    for admin_cid in ADMIN_IDS:
        try:
            bot.send_message(admin_cid, text,
                             reply_markup=admin_order_kb(order_id))
        except Exception as e:
            print(f"[WARN] نتونستم به ادمین {admin_cid} پیام بدم: {e}")


# ════════════════════════════════════════════════════════
#  LISTENER (logger)
# ════════════════════════════════════════════════════════

def info_listener(messages):
    for message in messages:
        uid = message.chat.id
        uname = message.chat.username or "بدون یوزرنیم"
        text = message.text or f"[{message.content_type}]"
        print(f"[MSG] {uname}({uid}): {text}")

bot.set_update_listener(info_listener)


# ════════════════════════════════════════════════════════
#  HANDLERS - عمومی
# ════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def cmd_start(message: Message):
    cid = message.chat.id
    clear_state(cid)

    # چک سوپرادمین / ادمین
    admin = db_get_admin(cid)
    if admin:
        role = admin["role"]
        if role == "superadmin":
            bot.send_message(
                cid,
                f"سلام <b>{admin['name']}</b> 👋\n🔑 پنل سوپرادمین",
                reply_markup=superadmin_menu_kb()
            )
        else:
            bot.send_message(
                cid,
                f"سلام <b>{admin['name']}</b> 👋\n🛠 پنل ادمین",
                reply_markup=admin_menu_kb()
            )
        return

    # چک کاربر عادی
    user = db_get_user(cid)
    if not user:
        bot.send_message(cid, "⛔️ شما دسترسی به این ربات ندارید.\nبا ادمین تماس بگیرید.")
        return

    bot.send_message(
        cid,
        f"سلام <b>{message.from_user.first_name}</b> 👋\nبه ربات خوش اومدی.",
        reply_markup=main_menu_kb()
    )


@bot.message_handler(func=lambda m: m.text == "👤 پروفایل من")
def btn_profile(message: Message):
    cid = message.chat.id
    user = db_get_user(cid)
    if not user:
        bot.send_message(cid, "⛔️ دسترسی ندارید."); return

    finance = db_get_user_finance(user["id"])
    cmd = finance["current_month_debt"] if finance else 0
    prev = finance["previous_debt"] if finance else 0

    text = (
        f"👤 <b>پروفایل شما</b>\n\n"
        f"نام: {user['name']}\n"
        f"یوزرنیم: @{user['username'] or '—'}\n"
        f"وضعیت: {'🚫 مسدود' if user['is_banned'] else '✅ فعال'}\n\n"
        f"💰 بدهی ماه جاری: <b>{cmd:,}</b> تومان\n"
        f"💳 بدهی قبلی: <b>{prev:,}</b> تومان"
    )
    bot.send_message(cid, text, reply_markup=main_menu_kb())


@bot.message_handler(func=lambda m: m.text == "💰 بخش مالی")
def btn_finance(message: Message):
    cid = message.chat.id
    user = db_get_user(cid)
    if not user:
        bot.send_message(cid, "⛔️ دسترسی ندارید."); return

    _show_finance_summary(cid, user)


@bot.message_handler(func=lambda m: m.text == "📋 سفارش‌های من")
def btn_my_orders(message: Message):
    bot.send_message(message.chat.id, "🔧 سفارش‌های من در حال توسعه است.")


# ════════════════════════════════════════════════════════
#  HANDLERS - ثبت سفارش
# ════════════════════════════════════════════════════════

@bot.message_handler(func=lambda m: m.text == "📦 ثبت سفارش")
def btn_new_order(message: Message):
    cid = message.chat.id
    user = db_get_user(cid)

    if not user:
        bot.send_message(cid, "⛔️ دسترسی ندارید."); return

    if user["is_banned"]:
        bot.send_message(
            cid,
            "🚫 <b>حساب شما مسدود است.</b>\n"
            "امکان ثبت سفارش ندارید.\n"
            "برای اطلاعات بیشتر با ادمین تماس بگیرید."
        )
        return

    models = db_get_active_models()
    if not models:
        bot.send_message(cid, "⚠️ هیچ مدل فعالی وجود ندارد. با ادمین تماس بگیرید.")
        return

    # شروع state - سبد خرید خالی
    set_state(cid, STEP_SELECT_MODEL,
              items=[],           # [{"model_id","model_name","quantity","unit_price","line_total"}]
              models=models,
              fabric=None,
              delivery_date=None,
              note=None,
              current_model_id=None)

    bot.send_message(
        cid,
        "📦 <b>ثبت سفارش جدید</b>\n\n"
        "مدل‌های مورد نظرت رو انتخاب کن.\n"
        "می‌تونی چند مدل انتخاب کنی.\n"
        "بعد از انتخاب، <b>«انتخاب مدل‌ها تمومه»</b> رو بزن.",
        reply_markup=models_kb(models, [])
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("model_"))
def cb_model_selected(call: CallbackQuery):
    cid = call.message.chat.id
    state = get_state(cid)

    if state.get("step") not in (STEP_SELECT_MODEL, STEP_ADD_MORE):
        bot.answer_callback_query(call.id, "⚠️ ابتدا ثبت سفارش رو شروع کن.")
        return

    model_id = int(call.data.split("_")[1])
    model = db_get_model_by_id(model_id)

    if not model:
        bot.answer_callback_query(call.id, "❌ مدل پیدا نشد."); return

    # ذخیره مدل انتخاب‌شده، برو به وارد کردن تعداد
    set_state(cid, STEP_ENTER_QUANTITY, current_model_id=model_id)

    bot.answer_callback_query(call.id)
    bot.send_message(
        cid,
        f"✅ <b>{model['name']}</b> انتخاب شد. (قیمت واحد: {model['price']:,} تومان)\n\n"
        f"تعداد رو انتخاب یا وارد کن:",
        reply_markup=quantity_confirm_kb()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("qty_"))
def cb_quantity(call: CallbackQuery):
    cid = call.message.chat.id
    state = get_state(cid)

    if state.get("step") != STEP_ENTER_QUANTITY:
        bot.answer_callback_query(call.id, "⚠️ مرحله اشتباه."); return

    action = call.data.split("_")[1]

    if action == "manual":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, "✏️ تعداد مورد نظر رو تایپ کن (عدد):")
        bot.register_next_step_handler(msg, _receive_manual_quantity)
        return

    qty = int(action)
    bot.answer_callback_query(call.id)
    _add_item_to_cart(cid, qty)


def _receive_manual_quantity(message: Message):
    cid = message.chat.id
    state = get_state(cid)

    if state.get("step") != STEP_ENTER_QUANTITY:
        return

    try:
        qty = int(message.text.strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(cid, "❌ لطفاً یک عدد مثبت وارد کن:")
        bot.register_next_step_handler(msg, _receive_manual_quantity)
        return

    _add_item_to_cart(cid, qty)


def _add_item_to_cart(cid: int, qty: int):
    """آیتم رو به سبد اضافه می‌کنه و نمایش می‌ده."""
    state = get_state(cid)
    model_id = state["data"]["current_model_id"]
    model = db_get_model_by_id(model_id)

    line_total = qty * model["price"]

    # چک کن همین مدل قبلاً توی سبد هست یا نه
    items = state["data"]["items"]
    existing = next((i for i in items if i["model_id"] == model_id), None)
    if existing:
        # تعداد رو update کن
        existing["quantity"] += qty
        existing["line_total"] = existing["quantity"] * existing["unit_price"]
    else:
        items.append({
            "model_id":   model_id,
            "model_name": model["name"],
            "quantity":   qty,
            "unit_price": model["price"],
            "line_total": line_total,
        })

    set_state(cid, STEP_ADD_MORE, items=items, current_model_id=None)

    # نمایش سبد فعلی
    cart_text = _format_cart(items)
    models = state["data"]["models"]
    selected_ids = [i["model_id"] for i in items]

    bot.send_message(
        cid,
        f"✅ به سبد اضافه شد!\n\n{cart_text}\n\n"
        "مدل دیگه‌ای هم می‌خوای اضافه کنی؟\n"
        "یا «انتخاب مدل‌ها تمومه» رو بزن.",
        reply_markup=models_kb(models, selected_ids)
    )


def _format_cart(items: list) -> str:
    lines = ["🛒 <b>سبد سفارش:</b>"]
    total = 0
    for i, item in enumerate(items, 1):
        total += item["line_total"]
        lines.append(
            f"{i}. {item['model_name']}  ×{item['quantity']}"
            f"  =  {item['line_total']:,} تومان"
        )
    lines.append(f"\n💵 جمع: <b>{total:,} تومان</b>")
    return "\n".join(lines)


@bot.callback_query_handler(func=lambda c: c.data == "models_done")
def cb_models_done(call: CallbackQuery):
    cid = call.message.chat.id
    state = get_state(cid)

    if state.get("step") not in (STEP_SELECT_MODEL, STEP_ADD_MORE):
        bot.answer_callback_query(call.id, "⚠️ مرحله اشتباه."); return

    items = state["data"].get("items", [])
    if not items:
        bot.answer_callback_query(call.id, "⚠️ حداقل یک مدل انتخاب کن!")
        return

    bot.answer_callback_query(call.id)
    set_state(cid, STEP_ENTER_FABRIC)

    msg = bot.send_message(
        cid,
        f"{_format_cart(items)}\n\n"
        "🪡 نوع پارچه رو وارد کن (متن آزاد):"
    )
    bot.register_next_step_handler(msg, _receive_fabric)


def _receive_fabric(message: Message):
    cid = message.chat.id
    state = get_state(cid)
    if state.get("step") != STEP_ENTER_FABRIC: return

    fabric = message.text.strip()
    if not fabric:
        msg = bot.send_message(cid, "❌ پارچه نمی‌تونه خالی باشه:")
        bot.register_next_step_handler(msg, _receive_fabric)
        return

    set_state(cid, STEP_ENTER_DATE, fabric=fabric)
    bot.send_message(
        cid,
        "📅 تاریخ تحویل رو انتخاب کن:",
        reply_markup=date_kb()
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("date_"))
def cb_date_selected(call: CallbackQuery):
    cid = call.message.chat.id
    state = get_state(cid)

    if state.get("step") != STEP_ENTER_DATE:
        bot.answer_callback_query(call.id, "⚠️ مرحله اشتباه."); return

    action = call.data[5:]  # بعد از "date_"

    if action == "manual":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, "✏️ تاریخ رو به فرمت YYYY-MM-DD وارد کن:\nمثلاً: 2025-03-15")
        bot.register_next_step_handler(msg, _receive_manual_date)
        return

    bot.answer_callback_query(call.id)
    _proceed_to_note(cid, action)


def _receive_manual_date(message: Message):
    cid = message.chat.id
    state = get_state(cid)
    if state.get("step") != STEP_ENTER_DATE: return

    text = message.text.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        msg = bot.send_message(cid, "❌ فرمت اشتباه. مثال: 2025-03-15")
        bot.register_next_step_handler(msg, _receive_manual_date)
        return

    _proceed_to_note(cid, text)


def _proceed_to_note(cid: int, delivery_date: str):
    set_state(cid, STEP_ENTER_NOTE, delivery_date=delivery_date)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("⏭ بدون یادداشت", callback_data="note_skip"))
    msg = bot.send_message(
        cid,
        "📝 یادداشتی برای ادمین داری؟ بنویس یا رد کن:",
        reply_markup=kb
    )
    bot.register_next_step_handler(msg, _receive_note)


@bot.callback_query_handler(func=lambda c: c.data == "note_skip")
def cb_note_skip(call: CallbackQuery):
    cid = call.message.chat.id
    state = get_state(cid)
    if state.get("step") != STEP_ENTER_NOTE:
        bot.answer_callback_query(call.id, "⚠️ مرحله اشتباه."); return
    bot.answer_callback_query(call.id)
    set_state(cid, STEP_ENTER_NOTE, note="")
    _show_order_confirm(cid)


def _receive_note(message: Message):
    cid = message.chat.id
    state = get_state(cid)
    if state.get("step") != STEP_ENTER_NOTE: return
    set_state(cid, STEP_ENTER_NOTE, note=message.text.strip())
    _show_order_confirm(cid)


def _show_order_confirm(cid: int):
    state = get_state(cid)
    d = state["data"]
    set_state(cid, STEP_CONFIRM)

    summary = format_order_summary(
        d["items"], d["fabric"], d["delivery_date"], d.get("note", "")
    )
    bot.send_message(
        cid,
        f"{summary}\n\n"
        "⬆️ سفارش بالا رو تایید می‌کنی؟",
        reply_markup=order_confirm_kb()
    )


@bot.callback_query_handler(func=lambda c: c.data == "order_confirm")
def cb_order_confirm(call: CallbackQuery):
    cid = call.message.chat.id
    state = get_state(cid)

    if state.get("step") != STEP_CONFIRM:
        bot.answer_callback_query(call.id, "⚠️ مرحله اشتباه."); return

    bot.answer_callback_query(call.id, "⏳ در حال ثبت...")
    d = state["data"]
    user = db_get_user(cid)
    total = sum(i["line_total"] for i in d["items"])

    order_id = db_create_order(
        user_db_id=user["id"],
        total_price=total,
        fabric=d["fabric"],
        delivery_date=d["delivery_date"],
        note=d.get("note", ""),
        items=d["items"]
    )

    clear_state(cid)

    summary = format_order_summary(
        d["items"], d["fabric"], d["delivery_date"], d.get("note", "")
    )

    bot.send_message(
        cid,
        f"✅ <b>سفارش #{order_id} ثبت شد!</b>\n\n"
        f"{summary}\n\n"
        "⏳ منتظر تایید ادمین بمان.",
        reply_markup=main_menu_kb()
    )

    notify_admins_new_order(order_id, cid, summary)


@bot.callback_query_handler(func=lambda c: c.data == "order_cancel")
def cb_order_cancel(call: CallbackQuery):
    cid = call.message.chat.id
    bot.answer_callback_query(call.id)
    clear_state(cid)
    bot.send_message(
        cid,
        "❌ سفارش لغو شد.",
        reply_markup=main_menu_kb()
    )


# ════════════════════════════════════════════════════════
#  HANDLERS - ادمین (تایید/رد سفارش)
# ════════════════════════════════════════════════════════

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_approve_"))
def cb_admin_approve(call: CallbackQuery):
    cid = call.message.chat.id
    if cid not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    order_id = int(call.data.split("_")[2])
    db_update_order_status(order_id, "approved")
    bot.answer_callback_query(call.id, "✅ سفارش تایید شد.")
    bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=None)
    bot.send_message(cid, f"✅ سفارش #{order_id} تایید شد.")

    # اطلاع به کاربر
    order = db_get_order_with_items(order_id)
    if order:
        user = db_get_user_by_db_id(order["user_id"])
        if user:
            bot.send_message(
                user["cid"],
                f"🎉 <b>سفارش #{order_id} شما تایید شد!</b>\n"
                "سفارشتون وارد مرحله تولید شد."
            )


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_reject_"))
def cb_admin_reject(call: CallbackQuery):
    cid = call.message.chat.id
    if cid not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    order_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)

    msg = bot.send_message(cid, f"❌ دلیل رد سفارش #{order_id} رو بنویس:")
    bot.register_next_step_handler(msg, _admin_receive_reject_reason, order_id)


def _admin_receive_reject_reason(message: Message, order_id: int):
    cid = message.chat.id
    reason = message.text.strip()
    db_update_order_status(order_id, "rejected", rejection_reason=reason)
    bot.send_message(cid, f"✅ سفارش #{order_id} رد شد.\nدلیل: {reason}")

    # اطلاع به کاربر
    order = db_get_order_with_items(order_id)
    if order:
        user = db_get_user_by_db_id(order["user_id"])
        if user:
            bot.send_message(
                user["cid"],
                f"❌ <b>سفارش #{order_id} شما رد شد.</b>\n"
                f"دلیل: {reason}"
            )


# ════════════════════════════════════════════════════════
#  HANDLERS - بخش مالی کاربر
# ════════════════════════════════════════════════════════

def _show_finance_summary(cid: int, user: dict):
    """نمایش خلاصه مالی + دکمه‌های inline."""
    finance = db_get_user_finance(user["id"])
    cmd  = int(finance["current_month_debt"]) if finance else 0
    prev = int(finance["previous_debt"])      if finance else 0
    total_debt = cmd + prev

    # پرداخت‌های در انتظار
    pending = db_get_pending_payments(user["id"])
    pending_sum = sum(int(p["amount"]) for p in pending)

    # اقساط فعال
    installments = db_get_installments(user["id"])
    inst_remaining = sum(
        int(i["total_amount"]) - int(i["paid_amount"]) for i in installments
    )

    text = (
        "💰 <b>بخش مالی</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 بدهی ماه جاری:   <b>{cmd:,}</b> تومان\n"
        f"💳 بدهی قبلی:        <b>{prev:,}</b> تومان\n"
        f"📊 جمع کل بدهی:     <b>{total_debt:,}</b> تومان\n"
    )
    if pending_sum:
        text += f"\n⏳ در انتظار تایید:  <b>{pending_sum:,}</b> تومان"
    if inst_remaining:
        text += f"\n📋 باقی اقساط:       <b>{inst_remaining:,}</b> تومان"

    text += "\n━━━━━━━━━━━━━━━━━━━━"

    bot.send_message(cid, text, reply_markup=finance_main_kb())


# ── نمایش تاریخچه تراکنش‌ها ──────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "fin_history")
def cb_fin_history(call: CallbackQuery):
    cid = call.message.chat.id
    user = db_get_user(cid)
    if not user:
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    bot.answer_callback_query(call.id)
    txs = db_get_transactions(user["id"], limit=20)

    if not txs:
        bot.send_message(cid, "📜 هنوز هیچ تراکنشی ثبت نشده.")
        return

    TYPE_LABEL = {
        "order":          "🛒 سفارش",
        "payment":        "💵 پرداخت",
        "debt_added":     "➕ بدهی اضافه",
        "installment":    "📋 قسط",
        "month_transfer": "🔄 انتقال ماه",
    }
    STATUS_LABEL = {
        "pending":  "⏳ در انتظار",
        "approved": "✅ تایید شده",
        "rejected": "❌ رد شده",
        None:       "",
    }

    lines = ["📜 <b>آخرین ۲۰ تراکنش:</b>\n"]
    for tx in txs:
        t_type   = TYPE_LABEL.get(tx["type"], tx["type"])
        t_status = STATUS_LABEL.get(tx.get("status"), "")
        t_date   = tx["created_at"].strftime("%Y/%m/%d") if tx["created_at"] else "—"
        t_amount = int(tx["amount"])
        sign     = "+" if tx["type"] in ("debt_added",) else "-" if tx["type"] == "payment" else ""
        by       = f" (توسط {tx['admin_name']})" if tx.get("admin_name") else ""

        lines.append(
            f"• {t_type} {t_status}{by}\n"
            f"  {sign}{t_amount:,} تومان  |  {t_date}\n"
            f"  {tx.get('description','') or ''}"
        )

    # تلگرام محدودیت 4096 کاراکتر داره، بشکونیم اگه لازم بود
    full_text = "\n".join(lines)
    if len(full_text) > 4000:
        full_text = full_text[:4000] + "\n\n... (ادامه دارد)"

    bot.send_message(cid, full_text)


# ── نمایش اقساط ──────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "fin_installments")
def cb_fin_installments(call: CallbackQuery):
    cid = call.message.chat.id
    user = db_get_user(cid)
    if not user:
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    bot.answer_callback_query(call.id)
    installments = db_get_installments(user["id"])

    if not installments:
        bot.send_message(cid, "📋 هیچ قسط فعالی ندارید.")
        return

    lines = ["📋 <b>اقساط فعال:</b>\n"]
    for i, inst in enumerate(installments, 1):
        total   = int(inst["total_amount"])
        paid    = int(inst["paid_amount"])
        remain  = total - paid
        per_ins = int(inst["per_installment"])
        num     = inst["num_installments"]
        by      = inst.get("admin_name") or "ادمین"
        date    = inst["created_at"].strftime("%Y/%m/%d") if inst["created_at"] else "—"

        lines.append(
            f"<b>قسط {i}</b>  (تنظیم شده توسط {by} در {date})\n"
            f"  کل: {total:,}  |  پرداخت شده: {paid:,}\n"
            f"  باقی‌مانده: <b>{remain:,}</b> تومان\n"
            f"  تعداد اقساط: {num}  |  هر قسط: {per_ins:,} تومان"
        )

    bot.send_message(cid, "\n\n".join(lines))


# ── پرداخت‌های در انتظار ─────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "fin_pending")
def cb_fin_pending(call: CallbackQuery):
    cid = call.message.chat.id
    user = db_get_user(cid)
    if not user:
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    bot.answer_callback_query(call.id)
    pending = db_get_pending_payments(user["id"])

    if not pending:
        bot.send_message(cid, "⏳ هیچ پرداختی در انتظار تایید ندارید.")
        return

    lines = ["⏳ <b>پرداخت‌های در انتظار تایید:</b>\n"]
    for p in pending:
        amount = int(p["amount"])
        date   = p["created_at"].strftime("%Y/%m/%d %H:%M") if p["created_at"] else "—"
        lines.append(f"• مبلغ: <b>{amount:,}</b> تومان  |  ارسال شده: {date}")

    bot.send_message(cid, "\n".join(lines))


# ── فلوی ثبت پرداخت جدید ────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "fin_new_payment")
def cb_fin_new_payment(call: CallbackQuery):
    cid = call.message.chat.id
    user = db_get_user(cid)
    if not user:
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    finance = db_get_user_finance(user["id"])
    cmd  = int(finance["current_month_debt"]) if finance else 0
    prev = int(finance["previous_debt"])      if finance else 0
    total_debt = cmd + prev

    if total_debt <= 0:
        bot.answer_callback_query(call.id, "✅ بدهی ندارید!")
        bot.send_message(cid, "✅ شما در حال حاضر هیچ بدهی‌ای ندارید.")
        return

    bot.answer_callback_query(call.id)
    set_state(cid, STEP_PAY_ENTER_AMOUNT,
              max_payable=total_debt,
              current_month_debt=cmd,
              previous_debt=prev)

    msg = bot.send_message(
        cid,
        f"💳 <b>ثبت پرداخت جدید</b>\n\n"
        f"📅 بدهی ماه جاری: <b>{cmd:,}</b> تومان\n"
        f"💳 بدهی قبلی:     <b>{prev:,}</b> تومان\n"
        f"📊 حداکثر قابل پرداخت: <b>{total_debt:,}</b> تومان\n\n"
        f"مبلغ پرداختی رو وارد کن (تومان):"
    )
    bot.register_next_step_handler(msg, _pay_receive_amount)


def _pay_receive_amount(message: Message):
    cid = message.chat.id
    state = get_state(cid)
    if state.get("step") != STEP_PAY_ENTER_AMOUNT: return

    text = message.text.strip().replace(",", "").replace("،", "")
    try:
        amount = int(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        msg = bot.send_message(cid, "❌ لطفاً یک عدد مثبت وارد کن (بدون کاما):")
        bot.register_next_step_handler(msg, _pay_receive_amount)
        return

    max_payable = state["data"]["max_payable"]
    if amount > max_payable:
        msg = bot.send_message(
            cid,
            f"❌ مبلغ وارد شده ({amount:,}) بیشتر از کل بدهی شما ({max_payable:,}) است.\n"
            f"حداکثر مبلغ قابل پرداخت: <b>{max_payable:,}</b> تومان\n\n"
            f"مجدد وارد کن:"
        )
        bot.register_next_step_handler(msg, _pay_receive_amount)
        return

    # پیش‌نمایش توزیع پرداخت
    cmd  = state["data"]["current_month_debt"]
    prev = state["data"]["previous_debt"]
    paid_current  = min(amount, cmd)
    paid_previous = min(amount - paid_current, prev)

    set_state(cid, STEP_PAY_SEND_RECEIPT, pay_amount=amount,
              paid_current=paid_current, paid_previous=paid_previous)

    preview = (
        f"💡 <b>توزیع این پرداخت:</b>\n"
        f"  از بدهی ماه جاری کم می‌شه: {paid_current:,} تومان\n"
    )
    if paid_previous:
        preview += f"  از بدهی قبلی کم می‌شه: {paid_previous:,} تومان\n"

    msg = bot.send_message(
        cid,
        f"✅ مبلغ <b>{amount:,}</b> تومان ثبت شد.\n\n"
        f"{preview}\n"
        f"📎 حالا تصویر فیش واریزی رو ارسال کن:"
    )
    bot.register_next_step_handler(msg, _pay_receive_receipt)


def _pay_receive_receipt(message: Message):
    cid = message.chat.id
    state = get_state(cid)
    if state.get("step") != STEP_PAY_SEND_RECEIPT: return

    # باید عکس یا فایل باشه
    file_id = None
    if message.photo:
        file_id = message.photo[-1].file_id   # بزرگ‌ترین سایز
    elif message.document:
        file_id = message.document.file_id
    else:
        msg = bot.send_message(
            cid,
            "❌ لطفاً تصویر یا فایل فیش واریزی رو ارسال کن:"
        )
        bot.register_next_step_handler(msg, _pay_receive_receipt)
        return

    set_state(cid, STEP_PAY_CONFIRM, receipt_file_id=file_id)

    d = get_state(cid)["data"]
    amount        = d["pay_amount"]
    paid_current  = d["paid_current"]
    paid_previous = d["paid_previous"]

    summary = (
        f"📋 <b>خلاصه درخواست پرداخت:</b>\n\n"
        f"💵 مبلغ: <b>{amount:,}</b> تومان\n"
        f"  └ از بدهی ماه جاری: {paid_current:,} تومان\n"
    )
    if paid_previous:
        summary += f"  └ از بدهی قبلی: {paid_previous:,} تومان\n"
    summary += "\n⬆️ ارسال می‌کنی؟"

    bot.send_message(cid, summary, reply_markup=pay_confirm_kb())


@bot.callback_query_handler(func=lambda c: c.data == "pay_submit")
def cb_pay_submit(call: CallbackQuery):
    cid = call.message.chat.id
    state = get_state(cid)
    if state.get("step") != STEP_PAY_CONFIRM:
        bot.answer_callback_query(call.id, "⚠️ مرحله اشتباه."); return

    bot.answer_callback_query(call.id, "⏳ در حال ارسال...")
    d    = state["data"]
    user = db_get_user(cid)

    tx_id = db_create_payment_request(
        user_db_id=user["id"],
        amount=d["pay_amount"],
        file_id=d["receipt_file_id"]
    )
    clear_state(cid)

    bot.send_message(
        cid,
        f"✅ <b>درخواست پرداخت #{tx_id} ارسال شد.</b>\n"
        f"منتظر تایید ادمین بمان.\n"
        f"وقتی تایید یا رد بشه بهت اطلاع می‌دیم.",
        reply_markup=main_menu_kb()
    )

    # ارسال به ادمین‌ها
    _notify_admins_payment(tx_id, user, d)


@bot.callback_query_handler(func=lambda c: c.data == "pay_cancel")
def cb_pay_cancel(call: CallbackQuery):
    cid = call.message.chat.id
    bot.answer_callback_query(call.id)
    clear_state(cid)
    bot.send_message(cid, "❌ پرداخت لغو شد.", reply_markup=main_menu_kb())


def _notify_admins_payment(tx_id: int, user: dict, data: dict):
    """ارسال فیش + اطلاعات پرداخت به ادمین‌ها."""
    caption = (
        f"🔔 <b>درخواست پرداخت جدید #{tx_id}</b>\n\n"
        f"👤 کاربر: {user['name']}\n"
        f"🆔 CID: <code>{user['cid']}</code>\n"
        f"💵 مبلغ: <b>{data['pay_amount']:,}</b> تومان\n"
        f"  └ از بدهی ماه جاری: {data['paid_current']:,}\n"
        f"  └ از بدهی قبلی: {data['paid_previous']:,}"
    )
    for admin_cid in ADMIN_IDS:
        try:
            bot.send_photo(
                admin_cid,
                data["receipt_file_id"],
                caption=caption,
                reply_markup=admin_payment_kb(tx_id)
            )
        except Exception:
            try:
                # شاید فایل بود نه عکس
                bot.send_document(
                    admin_cid,
                    data["receipt_file_id"],
                    caption=caption,
                    reply_markup=admin_payment_kb(tx_id)
                )
            except Exception as e:
                print(f"[WARN] ارسال فیش به ادمین {admin_cid} ناموفق: {e}")


# ════════════════════════════════════════════════════════
#  HANDLERS - ادمین (تایید/رد پرداخت)
# ════════════════════════════════════════════════════════

def _get_admin_db_id(admin_cid: int) -> int | None:
    """cid ادمین رو به id دیتابیس تبدیل می‌کنه."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("SELECT id FROM admins WHERE cid = %s", (admin_cid,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row["id"] if row else None


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_pay_approve_"))
def cb_admin_pay_approve(call: CallbackQuery):
    cid = call.message.chat.id
    if cid not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    tx_id = int(call.data.split("_")[3])
    admin_db_id = _get_admin_db_id(cid)

    result = db_approve_payment(tx_id, admin_db_id)
    if not result:
        bot.answer_callback_query(call.id, "⚠️ این پرداخت قبلاً پردازش شده.")
        return

    bot.answer_callback_query(call.id, "✅ پرداخت تایید شد.")
    bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=None)

    bot.send_message(
        cid,
        f"✅ <b>پرداخت #{tx_id} تایید شد.</b>\n"
        f"💵 مبلغ: {result['amount']:,} تومان\n"
        f"  └ از بدهی ماه جاری کم شد: {result['paid_current']:,}\n"
        f"  └ از بدهی قبلی کم شد: {result['paid_previous']:,}\n\n"
        f"وضعیت جدید کاربر:\n"
        f"  📅 بدهی ماه جاری: {result['new_cmd']:,}\n"
        f"  💳 بدهی قبلی: {result['new_prev']:,}"
    )

    # اطلاع به کاربر
    user = db_get_user_by_db_id(result["user_db_id"])
    if user:
        bot.send_message(
            user["cid"],
            f"✅ <b>پرداخت {result['amount']:,} تومانی شما تایید شد!</b>\n\n"
            f"📅 بدهی ماه جاری: <b>{result['new_cmd']:,}</b> تومان\n"
            f"💳 بدهی قبلی:     <b>{result['new_prev']:,}</b> تومان"
        )


@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_pay_reject_"))
def cb_admin_pay_reject(call: CallbackQuery):
    cid = call.message.chat.id
    if cid not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    tx_id = int(call.data.split("_")[3])
    bot.answer_callback_query(call.id)

    msg = bot.send_message(cid, f"❌ دلیل رد پرداخت #{tx_id} رو بنویس:")
    bot.register_next_step_handler(msg, _admin_receive_pay_reject_reason, tx_id)


def _admin_receive_pay_reject_reason(message: Message, tx_id: int):
    cid = message.chat.id
    reason = message.text.strip()
    admin_db_id = _get_admin_db_id(cid)

    tx = db_get_transaction_by_id(tx_id)
    db_reject_payment(tx_id, admin_db_id, reason)

    bot.edit_message_reply_markup(cid, message.message_id - 1, reply_markup=None)
    bot.send_message(
        cid,
        f"✅ پرداخت #{tx_id} رد شد.\nدلیل: {reason}"
    )

    # اطلاع به کاربر
    if tx:
        user = db_get_user_by_db_id(tx["user_id"])
        if user:
            bot.send_message(
                user["cid"],
                f"❌ <b>پرداخت {int(tx['amount']):,} تومانی شما رد شد.</b>\n"
                f"دلیل: {reason}\n\n"
                f"لطفاً فیش صحیح را ارسال کنید."
            )


# ════════════════════════════════════════════════════════
#  HANDLERS - ادمین عادی
# ════════════════════════════════════════════════════════

def _is_admin(cid: int) -> bool:
    return db_get_admin(cid) is not None

def _is_superadmin(cid: int) -> bool:
    a = db_get_admin(cid)
    return a is not None and a["role"] == "superadmin"


@bot.message_handler(func=lambda m: m.text == "📋 سفارشات در انتظار")
def btn_pending_orders(message: Message):
    cid = message.chat.id
    if not _is_admin(cid):
        bot.send_message(cid, "⛔️ دسترسی ندارید."); return

    orders = db_get_all_orders_pending()
    if not orders:
        bot.send_message(cid, "✅ هیچ سفارش در انتظاری وجود ندارد.")
        return

    bot.send_message(
        cid,
        f"📋 <b>{len(orders)} سفارش در انتظار تایید:</b>",
        reply_markup=admin_order_list_kb(orders)
    )


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_order_"))
def cb_adm_order_detail(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid):
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    order_id = int(call.data.split("_")[2])
    is_super  = _is_superadmin(cid)

    if is_super:
        order = db_get_order_with_items(order_id)
    else:
        order = db_get_order_with_items(order_id)

    if not order:
        bot.answer_callback_query(call.id, "❌ سفارش پیدا نشد."); return

    bot.answer_callback_query(call.id)

    STATUS_FA = {
        "pending":   "⏳ در انتظار",
        "approved":  "✅ تایید شده",
        "producing": "🔧 در حال تولید",
        "ready":     "📦 آماده تحویل",
        "delivered": "🚚 تحویل داده شد",
        "rejected":  "❌ رد شده",
    }

    if is_super:
        # سوپرادمین با قیمت می‌بینه
        items_text = "\n".join(
            f"  • {i['model_name']}  ×{i['quantity']}  =  {int(i['line_total']):,} تومان"
            for i in order["items"]
        )
        text = (
            f"📦 <b>سفارش #{order_id}</b>\n"
            f"وضعیت: {STATUS_FA.get(order['status'], order['status'])}\n"
            f"تاریخ ثبت: {order['created_at'].strftime('%Y/%m/%d %H:%M')}\n"
            f"تاریخ تحویل: {order['delivery_date']}\n"
            f"پارچه: {order['fabric']}\n\n"
            f"<b>آیتم‌ها:</b>\n{items_text}\n\n"
            f"💵 جمع کل: <b>{int(order['total_price']):,}</b> تومان"
        )
    else:
        # ادمین عادی بدون قیمت
        items = db_get_order_items_no_price(order_id)
        items_text = "\n".join(
            f"  • {i['model_name']}  ×{i['quantity']}"
            for i in items
        )
        text = (
            f"📦 <b>سفارش #{order_id}</b>\n"
            f"وضعیت: {STATUS_FA.get(order['status'], order['status'])}\n"
            f"تاریخ ثبت: {order['created_at'].strftime('%Y/%m/%d %H:%M')}\n"
            f"تاریخ تحویل: {order['delivery_date']}\n"
            f"پارچه: {order['fabric']}\n\n"
            f"<b>آیتم‌ها:</b>\n{items_text}"
        )
        if order.get("note"):
            text += f"\n\n📝 یادداشت: {order['note']}"

    bot.send_message(cid, text, reply_markup=admin_order_status_kb(order_id))


@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_setstatus_"))
def cb_adm_set_status(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_admin(cid):
        bot.answer_callback_query(call.id, "⛔️ دسترسی ندارید."); return

    parts    = call.data.split("_")
    order_id = int(parts[2])
    status   = parts[3]

    if status == "rejected":
        bot.answer_callback_query(call.id)
        msg = bot.send_message(cid, f"❌ دلیل رد سفارش #{order_id} رو بنویس:")
        bot.register_next_step_handler(msg, _admin_receive_reject_reason, order_id)
        return

    db_update_order_status(order_id, status)
    bot.answer_callback_query(call.id, "✅ وضعیت بروز شد.")
    bot.edit_message_reply_markup(cid, call.message.message_id, reply_markup=None)

    STATUS_FA = {
        "approved":  "✅ تایید شد",
        "producing": "🔧 وارد تولید شد",
        "ready":     "📦 آماده تحویل شد",
        "delivered": "🚚 تحویل داده شد",
    }
    bot.send_message(cid, f"سفارش #{order_id} → {STATUS_FA.get(status, status)}")

    # اطلاع به کاربر
    order = db_get_order_with_items(order_id)
    if order:
        user = db_get_user_by_db_id(order["user_id"])
        if user:
            user_msg = {
                "approved":  f"🎉 سفارش #{order_id} شما تایید شد و وارد تولید می‌شه.",
                "producing": f"🔧 سفارش #{order_id} شما در حال تولید است.",
                "ready":     f"📦 سفارش #{order_id} شما آماده تحویل است.",
                "delivered": f"✅ سفارش #{order_id} شما تحویل داده شد.",
            }
            if status in user_msg:
                try:
                    bot.send_message(user["cid"], f"<b>{user_msg[status]}</b>")
                except Exception as e:
                    print(f"[WARN] اطلاع‌رسانی به کاربر ناموفق: {e}")


# ════════════════════════════════════════════════════════
#  HANDLERS - سوپرادمین
# ════════════════════════════════════════════════════════

# ── منوی کاربران ─────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "👥 کاربران")
def btn_sa_users(message: Message):
    cid = message.chat.id
    if not _is_superadmin(cid):
        bot.send_message(cid, "⛔️ دسترسی ندارید."); return

    users = db_get_all_users_summary()
    if not users:
        bot.send_message(
            cid, "هنوز کاربری ثبت نشده.",
            reply_markup=sa_users_list_kb([])
        )
        return

    total_debt = sum(int(u["current_month_debt"]) + int(u["previous_debt"]) for u in users)
    banned_cnt = sum(1 for u in users if u["is_banned"])

    bot.send_message(
        cid,
        f"👥 <b>کاربران ({len(users)} نفر)</b>\n"
        f"🔴 مسدود: {banned_cnt}  |  💰 کل بدهی: {total_debt:,} تومان\n\n"
        f"یک کاربر رو انتخاب کن:",
        reply_markup=sa_users_list_kb(users)
    )


@bot.callback_query_handler(func=lambda c: c.data == "sa_users_list")
def cb_sa_users_list(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    bot.answer_callback_query(call.id)
    users = db_get_all_users_summary()
    bot.send_message(cid, "👥 لیست کاربران:", reply_markup=sa_users_list_kb(users))


@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_user_"))
def cb_sa_user_detail(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return

    user_db_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    report = db_get_user_full_report(user_db_id)
    if not report:
        bot.send_message(cid, "❌ کاربر پیدا نشد."); return

    u  = report["user"]
    f  = report["finance"] or {}
    cmd  = int(f.get("current_month_debt", 0))
    prev = int(f.get("previous_debt", 0))

    # ── خلاصه مشخصات ──
    text = (
        f"👤 <b>{u['name']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 CID: <code>{u['cid']}</code>\n"
        f"👤 یوزرنیم: @{u['username'] or '—'}\n"
        f"📅 عضویت: {u['created_at'].strftime('%Y/%m/%d') if u.get('created_at') else '—'}\n"
        f"وضعیت: {'🚫 مسدود' if u['is_banned'] else '✅ فعال'}\n"
    )
    if u["is_banned"] and u.get("ban_reason"):
        text += f"دلیل مسدودی: {u['ban_reason']}\n"

    # ── مالی ──
    text += (
        f"\n💰 <b>وضعیت مالی:</b>\n"
        f"  📅 بدهی ماه جاری: <b>{cmd:,}</b> تومان\n"
        f"  💳 بدهی قبلی:     <b>{prev:,}</b> تومان\n"
        f"  📊 جمع کل:        <b>{cmd+prev:,}</b> تومان\n"
    )

    # ── اقساط ──
    if report["installments"]:
        text += f"\n📋 <b>اقساط ({len(report['installments'])} مورد):</b>\n"
        for inst in report["installments"]:
            remain = int(inst["total_amount"]) - int(inst["paid_amount"])
            text += f"  • کل: {int(inst['total_amount']):,}  |  باقی: {remain:,}\n"

    # ── آخرین سفارشات ──
    if report["orders"]:
        STATUS_FA = {
            "pending":"⏳","approved":"✅","producing":"🔧",
            "ready":"📦","delivered":"🚚","rejected":"❌"
        }
        text += f"\n📦 <b>آخرین سفارشات:</b>\n"
        for o in report["orders"][:5]:
            st = STATUS_FA.get(o["status"], "?")
            text += (
                f"  {st} #{o['id']}  |  {int(o['total_price']):,} تومان"
                f"  |  {o['created_at'].strftime('%m/%d')}\n"
            )

    # ── آخرین تراکنش‌ها ──
    if report["transactions"]:
        TYPE_FA = {
            "order":"🛒","payment":"💵","debt_added":"➕",
            "installment":"📋","month_transfer":"🔄"
        }
        text += f"\n💳 <b>آخرین تراکنش‌ها:</b>\n"
        for tx in report["transactions"][:5]:
            tp = TYPE_FA.get(tx["type"], "•")
            st = ""
            if tx.get("status") == "pending":  st = " ⏳"
            if tx.get("status") == "rejected": st = " ❌"
            text += (
                f"  {tp}{st} {int(tx['amount']):,} تومان"
                f"  |  {tx['created_at'].strftime('%m/%d')}\n"
            )

    bot.send_message(cid, text, reply_markup=sa_user_actions_kb(user_db_id, u["is_banned"]))


# ── افزودن کاربر ─────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data == "sa_add_user")
def cb_sa_add_user(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    bot.answer_callback_query(call.id)
    set_state(cid, STEP_SA_ADD_USER_CID)
    msg = bot.send_message(cid, "➕ <b>افزودن کاربر جدید</b>\n\nChat ID کاربر رو وارد کن:")
    bot.register_next_step_handler(msg, _sa_add_user_cid)


def _sa_add_user_cid(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_ADD_USER_CID: return
    try:
        new_cid = int(message.text.strip())
    except ValueError:
        msg = bot.send_message(cid, "❌ Chat ID باید عدد باشه:")
        bot.register_next_step_handler(msg, _sa_add_user_cid); return
    set_state(cid, STEP_SA_ADD_USER_NAME, new_user_cid=new_cid)
    msg = bot.send_message(cid, "نام کاربر رو وارد کن:")
    bot.register_next_step_handler(msg, _sa_add_user_name)


def _sa_add_user_name(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_ADD_USER_NAME: return
    name = message.text.strip()
    set_state(cid, STEP_SA_ADD_USER_USERNAME, new_user_name=name)
    msg = bot.send_message(cid, "یوزرنیم رو وارد کن (بدون @ — یا عدد ۰ برای بدون یوزرنیم):")
    bot.register_next_step_handler(msg, _sa_add_user_username)


def _sa_add_user_username(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_ADD_USER_USERNAME: return
    username = message.text.strip()
    if username == "0":
        username = None
    d = get_state(cid)["data"]
    admin = db_get_admin(cid)
    success = db_add_user(d["new_user_cid"], d["new_user_name"], username, admin["id"])
    clear_state(cid)
    if success:
        bot.send_message(
            cid,
            f"✅ کاربر <b>{d['new_user_name']}</b> با موفقیت اضافه شد.\n"
            f"CID: <code>{d['new_user_cid']}</code>"
        )
        # اطلاع به کاربر جدید
        try:
            bot.send_message(
                d["new_user_cid"],
                "🎉 حساب شما در ربات فعال شد!\n"
                "برای شروع /start را بزنید."
            )
        except Exception:
            pass
    else:
        bot.send_message(cid, "⚠️ این کاربر قبلاً ثبت شده.")


# ── بن / آنبن ────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_ban_"))
def cb_sa_ban(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    user_db_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    db_ban_user(user_db_id, "مسدود شده توسط سوپرادمین")
    bot.send_message(cid, "🚫 کاربر مسدود شد.")
    user = db_get_user_by_db_id(user_db_id)
    if user:
        try:
            bot.send_message(user["cid"], "🚫 حساب شما توسط ادمین مسدود شد.")
        except Exception:
            pass


@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_unban_"))
def cb_sa_unban(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    user_db_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    db_unban_user(user_db_id)
    bot.send_message(cid, "✅ مسدودی کاربر رفع شد.")
    user = db_get_user_by_db_id(user_db_id)
    if user:
        try:
            bot.send_message(user["cid"], "✅ مسدودی حساب شما رفع شد. می‌تونید سفارش بدید.")
        except Exception:
            pass


# ── انتقال بدهی ماه جاری به قبلی ─────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_transfer_"))
def cb_sa_transfer(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    user_db_id  = int(call.data.split("_")[2])
    admin       = db_get_admin(cid)
    bot.answer_callback_query(call.id, "⏳ در حال انتقال...")
    amount = db_transfer_debt_to_previous(user_db_id, admin["id"])
    if amount == 0:
        bot.send_message(cid, "⚠️ این کاربر بدهی ماه جاری ندارد.")
        return
    bot.send_message(
        cid,
        f"✅ <b>{amount:,} تومان</b> از بدهی ماه جاری به بدهی قبلی منتقل شد.\n"
        f"پنل کاربر هم باز شد."
    )
    user = db_get_user_by_db_id(user_db_id)
    if user:
        try:
            bot.send_message(
                user["cid"],
                f"🔔 بدهی ماه جاری شما (<b>{amount:,}</b> تومان) به بدهی قبلی منتقل شد.\n"
                f"پنل شما باز است و می‌توانید سفارش ثبت کنید."
            )
        except Exception:
            pass


# ── افزودن بدهی دستی ─────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_adddebt_"))
def cb_sa_add_debt(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        return
    bot.answer_callback_query(call.id)
    user_db_id = int(call.data.split("_")[2])
    set_state(cid, STEP_SA_ADD_DEBT_AMOUNT, target_user_id=user_db_id)
    msg = bot.send_message(cid, "➕ <b>افزودن بدهی دستی</b>\n\nمبلغ بدهی رو وارد کن (تومان):")
    bot.register_next_step_handler(msg, _sa_debt_amount)


def _sa_debt_amount(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_ADD_DEBT_AMOUNT: return
    try:
        amount = int(message.text.strip().replace(",", "").replace("،", ""))
        if amount <= 0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid, "❌ عدد مثبت وارد کن:")
        bot.register_next_step_handler(msg, _sa_debt_amount); return
    set_state(cid, STEP_SA_ADD_DEBT_DESC, debt_amount=amount)
    msg = bot.send_message(cid, "توضیح بدهی رو بنویس:")
    bot.register_next_step_handler(msg, _sa_debt_desc)


def _sa_debt_desc(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_ADD_DEBT_DESC: return
    desc   = message.text.strip()
    d      = get_state(cid)["data"]
    admin  = db_get_admin(cid)
    db_add_manual_debt(d["target_user_id"], d["debt_amount"], desc, admin["id"])
    clear_state(cid)
    bot.send_message(
        cid,
        f"✅ <b>{d['debt_amount']:,} تومان</b> بدهی اضافه شد.\nتوضیح: {desc}"
    )
    user = db_get_user_by_db_id(d["target_user_id"])
    if user:
        try:
            bot.send_message(
                user["cid"],
                f"🔔 <b>{d['debt_amount']:,} تومان</b> به بدهی ماه جاری شما اضافه شد.\n"
                f"توضیح: {desc}"
            )
        except Exception:
            pass


# ── قسط‌بندی ────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_inst_"))
def cb_sa_installment(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    user_db_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    set_state(cid, STEP_SA_INST_AMOUNT, target_user_id=user_db_id)
    msg = bot.send_message(
        cid,
        "📋 <b>قسط‌بندی جدید</b>\n\n"
        "کل مبلغ قسط‌بندی رو وارد کن (تومان):"
    )
    bot.register_next_step_handler(msg, _sa_inst_amount)


def _sa_inst_amount(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_INST_AMOUNT: return
    try:
        amount = int(message.text.strip().replace(",", "").replace("،", ""))
        if amount <= 0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid, "❌ عدد مثبت وارد کن:")
        bot.register_next_step_handler(msg, _sa_inst_amount); return
    set_state(cid, STEP_SA_INST_COUNT, inst_total=amount)
    msg = bot.send_message(cid, "تعداد اقساط رو وارد کن:")
    bot.register_next_step_handler(msg, _sa_inst_count)


def _sa_inst_count(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_INST_COUNT: return
    try:
        count = int(message.text.strip())
        if count <= 0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid, "❌ عدد مثبت وارد کن:")
        bot.register_next_step_handler(msg, _sa_inst_count); return

    d   = get_state(cid)["data"]
    per = d["inst_total"] // count
    set_state(cid, STEP_SA_INST_DATES, inst_count=count, per_inst=per)

    msg = bot.send_message(
        cid,
        f"هر قسط: <b>{per:,}</b> تومان\n\n"
        f"تاریخ سررسید {count} قسط رو خط به خط وارد کن (YYYY-MM-DD):\n"
        f"مثال:\n2025-02-01\n2025-03-01"
    )
    bot.register_next_step_handler(msg, _sa_inst_dates)


def _sa_inst_dates(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_INST_DATES: return
    lines = [l.strip() for l in message.text.strip().splitlines() if l.strip()]
    d     = get_state(cid)["data"]
    count = d["inst_count"]

    if len(lines) != count:
        msg = bot.send_message(
            cid,
            f"❌ باید دقیقاً {count} تاریخ وارد کنی (الان {len(lines)} تا).\nدوباره:"
        )
        bot.register_next_step_handler(msg, _sa_inst_dates); return

    for line in lines:
        try:
            datetime.strptime(line, "%Y-%m-%d")
        except ValueError:
            msg = bot.send_message(cid, f"❌ فرمت اشتباه: {line}\nدوباره:")
            bot.register_next_step_handler(msg, _sa_inst_dates); return

    admin = db_get_admin(cid)
    inst_id = db_add_installment(
        d["target_user_id"], d["inst_total"],
        count, lines, admin["id"]
    )
    clear_state(cid)
    bot.send_message(
        cid,
        f"✅ قسط‌بندی #{inst_id} ثبت شد.\n"
        f"کل: {d['inst_total']:,}  |  {count} قسط  |  هر قسط: {d['per_inst']:,} تومان"
    )
    user = db_get_user_by_db_id(d["target_user_id"])
    if user:
        dates_text = "\n".join(f"  • {dt}" for dt in lines)
        try:
            bot.send_message(
                user["cid"],
                f"📋 <b>قسط‌بندی جدید برای شما تنظیم شد.</b>\n"
                f"کل مبلغ: {d['inst_total']:,} تومان\n"
                f"تعداد اقساط: {count}\n"
                f"هر قسط: {d['per_inst']:,} تومان\n\n"
                f"تاریخ‌های سررسید:\n{dates_text}"
            )
        except Exception:
            pass


# ── مدیریت مدل‌ها ────────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "📦 مدل‌ها")
def btn_sa_models(message: Message):
    cid = message.chat.id
    if not _is_superadmin(cid):
        bot.send_message(cid, "⛔️ دسترسی ندارید."); return
    models = db_get_active_models()
    # همه مدل‌ها رو می‌خوایم نه فقط active
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.id, m.name, m.is_active,
               COALESCE((
                   SELECT price FROM model_prices
                   WHERE model_id = m.id ORDER BY set_at DESC LIMIT 1
               ), 0) AS price
        FROM models m ORDER BY m.name
    """)
    all_models = cur.fetchall()
    cur.close(); conn.close()

    if not all_models:
        bot.send_message(cid, "هنوز مدلی ثبت نشده.", reply_markup=sa_models_kb([]))
        return

    bot.send_message(
        cid,
        f"📦 <b>مدل‌ها ({len(all_models)} مورد)</b>\nیک مدل انتخاب کن:",
        reply_markup=sa_models_kb(all_models)
    )


@bot.callback_query_handler(func=lambda c: c.data == "sa_models_list")
def cb_sa_models_list(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    bot.answer_callback_query(call.id)
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT m.id, m.name, m.is_active,
               COALESCE((SELECT price FROM model_prices
                         WHERE model_id=m.id ORDER BY set_at DESC LIMIT 1), 0) AS price
        FROM models m ORDER BY m.name
    """)
    all_models = cur.fetchall()
    cur.close(); conn.close()
    bot.send_message(cid, "📦 لیست مدل‌ها:", reply_markup=sa_models_kb(all_models))


@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_model_") and not c.data.startswith("sa_modelt") and not c.data.startswith("sa_modelp"))
def cb_sa_model_detail(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    model_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    model = db_get_model_by_id(model_id)
    if not model:
        bot.send_message(cid, "❌ مدل پیدا نشد."); return

    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT mp.price, mp.set_at, a.name AS admin_name
        FROM model_prices mp
        LEFT JOIN admins a ON a.id = mp.set_by
        WHERE mp.model_id = %s ORDER BY mp.set_at DESC LIMIT 5
    """, (model_id,))
    price_history = cur.fetchall()
    cur.execute("SELECT is_active FROM models WHERE id=%s", (model_id,))
    m_row = cur.fetchone()
    cur.close(); conn.close()

    hist_text = "\n".join(
        f"  • {int(p['price']):,}  |  {p['set_at'].strftime('%Y/%m/%d')}"
        f"  |  {p['admin_name'] or '—'}"
        for p in price_history
    )
    text = (
        f"📦 <b>{model['name']}</b>\n"
        f"وضعیت: {'✅ فعال' if m_row['is_active'] else '❌ غیرفعال'}\n"
        f"قیمت فعلی: <b>{int(model['price']):,}</b> تومان\n\n"
        f"📜 تاریخچه قیمت:\n{hist_text or '  —'}"
    )
    bot.send_message(cid, text, reply_markup=sa_model_actions_kb(model_id, m_row["is_active"]))


@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_modeltoggle_"))
def cb_sa_model_toggle(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    model_id   = int(call.data.split("_")[2])
    new_status = db_toggle_model(model_id)
    status_fa  = "✅ فعال" if new_status else "❌ غیرفعال"
    bot.answer_callback_query(call.id, f"وضعیت مدل: {status_fa}")
    bot.send_message(cid, f"مدل #{model_id} → {status_fa}")


@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_modelprice_"))
def cb_sa_model_price(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    model_id = int(call.data.split("_")[2])
    bot.answer_callback_query(call.id)
    set_state(cid, STEP_SA_SET_PRICE_AMOUNT, price_model_id=model_id)
    model = db_get_model_by_id(model_id)
    msg = bot.send_message(
        cid,
        f"💲 تغییر قیمت <b>{model['name']}</b>\n"
        f"قیمت فعلی: {int(model['price']):,} تومان\n\n"
        f"قیمت جدید رو وارد کن (تومان):"
    )
    bot.register_next_step_handler(msg, _sa_set_price)


def _sa_set_price(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_SET_PRICE_AMOUNT: return
    try:
        price = int(message.text.strip().replace(",", "").replace("،", ""))
        if price <= 0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid, "❌ عدد مثبت وارد کن:")
        bot.register_next_step_handler(msg, _sa_set_price); return

    d        = get_state(cid)["data"]
    admin    = db_get_admin(cid)
    model    = db_get_model_by_id(d["price_model_id"])
    db_set_model_price(d["price_model_id"], price, admin["id"])
    clear_state(cid)
    bot.send_message(
        cid,
        f"✅ قیمت <b>{model['name']}</b> به <b>{price:,}</b> تومان بروز شد."
    )


@bot.callback_query_handler(func=lambda c: c.data == "sa_add_model")
def cb_sa_add_model(call: CallbackQuery):
    cid = call.message.chat.id
    if not _is_superadmin(cid):
        bot.answer_callback_query(call.id, "⛔️"); return
    bot.answer_callback_query(call.id)
    set_state(cid, STEP_SA_ADD_MODEL_NAME)
    msg = bot.send_message(cid, "➕ <b>افزودن مدل جدید</b>\n\nنام مدل رو وارد کن:")
    bot.register_next_step_handler(msg, _sa_add_model_name)


def _sa_add_model_name(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_ADD_MODEL_NAME: return
    name = message.text.strip()
    set_state(cid, STEP_SA_ADD_MODEL_PRICE, new_model_name=name)
    msg = bot.send_message(cid, f"قیمت اولیه <b>{name}</b> رو وارد کن (تومان):")
    bot.register_next_step_handler(msg, _sa_add_model_price)


def _sa_add_model_price(message: Message):
    cid = message.chat.id
    if get_state(cid).get("step") != STEP_SA_ADD_MODEL_PRICE: return
    try:
        price = int(message.text.strip().replace(",", "").replace("،", ""))
        if price <= 0: raise ValueError
    except ValueError:
        msg = bot.send_message(cid, "❌ عدد مثبت وارد کن:")
        bot.register_next_step_handler(msg, _sa_add_model_price); return

    d       = get_state(cid)["data"]
    admin   = db_get_admin(cid)
    model_id = db_add_model(d["new_model_name"], price, admin["id"])
    clear_state(cid)
    bot.send_message(
        cid,
        f"✅ مدل <b>{d['new_model_name']}</b> با قیمت <b>{price:,}</b> تومان اضافه شد.\n"
        f"شناسه مدل: #{model_id}"
    )


# ── گزارش مالی کلی ───────────────────────────────────────

@bot.message_handler(func=lambda m: m.text == "📊 گزارش مالی")
def btn_sa_finance_report(message: Message):
    cid = message.chat.id
    if not _is_superadmin(cid):
        bot.send_message(cid, "⛔️ دسترسی ندارید."); return

    conn = get_connection()
    cur  = conn.cursor(dictionary=True)

    cur.execute("""
        SELECT
            SUM(current_month_debt) AS total_cmd,
            SUM(previous_debt)      AS total_prev
        FROM user_finance
    """)
    fin = cur.fetchone()

    cur.execute("SELECT COUNT(*) AS cnt FROM orders WHERE status='pending'")
    pending_orders = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) AS cnt FROM users WHERE is_banned=TRUE")
    banned_cnt = cur.fetchone()["cnt"]

    cur.execute("""
        SELECT COUNT(*) AS cnt FROM transactions
        WHERE type='payment' AND status='pending'
    """)
    pending_pays = cur.fetchone()["cnt"]

    cur.execute("""
        SELECT SUM(total_price) AS total
        FROM orders
        WHERE MONTH(created_at)=MONTH(NOW()) AND YEAR(created_at)=YEAR(NOW())
    """)
    this_month = cur.fetchone()["total"] or 0

    cur.close(); conn.close()

    total_cmd  = int(fin["total_cmd"]  or 0)
    total_prev = int(fin["total_prev"] or 0)

    bot.send_message(
        cid,
        f"📊 <b>گزارش مالی کلی</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 جمع بدهی ماه جاری:  <b>{total_cmd:,}</b> تومان\n"
        f"💳 جمع بدهی قبلی:       <b>{total_prev:,}</b> تومان\n"
        f"📊 جمع کل بدهی‌ها:      <b>{total_cmd+total_prev:,}</b> تومان\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 سفارشات این ماه:     <b>{int(this_month):,}</b> تومان\n"
        f"⏳ سفارش در انتظار:     <b>{pending_orders}</b> مورد\n"
        f"⏳ پرداخت در انتظار:    <b>{pending_pays}</b> مورد\n"
        f"🚫 کاربران مسدود:       <b>{banned_cnt}</b> نفر"
    )


# ════════════════════════════════════════════════════════
#  SCHEDULER - سیستم بن اتوماتیک (شمسی)
# ════════════════════════════════════════════════════════

def db_get_users_with_current_debt() -> list:
    """کاربرانی که بدهی ماه جاری دارن و بن نیستن."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT u.id, u.cid, u.name, uf.current_month_debt
        FROM users u
        JOIN user_finance uf ON uf.user_id = u.id
        WHERE uf.current_month_debt > 0
          AND u.is_banned = FALSE
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def db_ban_user(user_db_id: int, reason: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users SET is_banned = TRUE, ban_reason = %s
        WHERE id = %s
    """, (reason, user_db_id))
    conn.commit()
    cur.close(); conn.close()


def db_get_users_banned_for_debt() -> list:
    """کاربران بن‌شده به دلیل بدهی ماه جاری (برای گزارش به ادمین)."""
    conn = get_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT u.id, u.cid, u.name, uf.current_month_debt
        FROM users u
        JOIN user_finance uf ON uf.user_id = u.id
        WHERE u.is_banned = TRUE
          AND u.ban_reason LIKE 'عدم تسویه ماه%'
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def _jalali_month_end() -> datetime:
    """
    محاسبه آخرین لحظه ماه شمسی جاری + ۵ روز grace period.
    مثلاً اگه الان ۱۴۰۳/۰۶/۱۵ باشه:
      پایان ماه ۶ = ۱۴۰۳/۰۶/۳۱
      deadline    = ۱۴۰۳/۰۷/۰۵  ساعت ۲۳:۵۹:۵۹
    """
    now_j = jdatetime.datetime.now()
    month = now_j.month
    year  = now_j.year

    # طول ماه‌های شمسی
    if month <= 6:
        days_in_month = 31
    elif month <= 11:
        days_in_month = 30
    else:  # اسفند
        days_in_month = 30 if jdatetime.datetime(year, 1, 1).isleap() else 29

    # آخر ماه + ۵ روز grace
    end_of_month_j = jdatetime.datetime(year, month, days_in_month, 23, 59, 59)
    deadline_j     = end_of_month_j + jdatetime.timedelta(days=5)

    return deadline_j.togregorian()


def _seconds_until(target: datetime) -> float:
    """ثانیه‌های مانده تا یک datetime میلادی."""
    now  = datetime.now()
    diff = (target - now).total_seconds()
    return max(diff, 0)


def run_ban_check():
    """
    بررسی و بن کاربران بدهکار.
    این تابع ۵ روز بعد از پایان هر ماه شمسی اجرا میشه.
    """
    now_j        = jdatetime.datetime.now()
    month_name   = now_j.strftime("%B %Y")   # مثلاً: شهریور ۱۴۰۳

    print(f"[SCHEDULER] اجرای بررسی بن  —  {now_j.strftime('%Y/%m/%d %H:%M')}")

    debtors = db_get_users_with_current_debt()

    if not debtors:
        print("[SCHEDULER] هیچ بدهکاری برای بن یافت نشد.")
        _schedule_next_ban_check()
        return

    banned_users = []
    for user in debtors:
        reason = f"عدم تسویه ماه {month_name} — بدهی: {int(user['current_month_debt']):,} تومان"
        db_ban_user(user["id"], reason)
        banned_users.append(user)

        # اطلاع به کاربر
        try:
            bot.send_message(
                user["cid"],
                f"🚫 <b>حساب شما مسدود شد.</b>\n\n"
                f"به دلیل عدم تسویه بدهی ماه <b>{month_name}</b>\n"
                f"مبلغ: <b>{int(user['current_month_debt']):,}</b> تومان\n\n"
                f"برای رفع مسدودی با ادمین تماس بگیرید."
            )
        except Exception as e:
            print(f"[WARN] اطلاع‌رسانی به کاربر {user['cid']} ناموفق: {e}")

    # گزارش به ادمین‌ها
    report_lines = [
        f"🔴 <b>گزارش بن اتوماتیک — {month_name}</b>\n",
        f"تعداد کاربران بن‌شده: <b>{len(banned_users)}</b>\n"
    ]
    for u in banned_users:
        report_lines.append(
            f"• {u['name']}  |  بدهی: {int(u['current_month_debt']):,} تومان"
        )

    report_text = "\n".join(report_lines)
    for admin_cid in ADMIN_IDS:
        try:
            bot.send_message(admin_cid, report_text)
        except Exception as e:
            print(f"[WARN] ارسال گزارش به ادمین {admin_cid} ناموفق: {e}")

    print(f"[SCHEDULER] {len(banned_users)} کاربر بن شدن.")

    # زمان‌بندی ماه بعد
    _schedule_next_ban_check()


def _schedule_next_ban_check():
    """
    زمان‌بندی اجرای بعدی:
    ابتدا محاسبه می‌کنیم ماه شمسی بعدی کِی تموم میشه + ۵ روز.
    """
    now_j  = jdatetime.datetime.now()
    month  = now_j.month
    year   = now_j.year

    # برو ماه بعد
    if month == 12:
        next_month = 1
        next_year  = year + 1
    else:
        next_month = month + 1
        next_year  = year

    # طول ماه بعدی
    if next_month <= 6:
        days_in_next = 31
    elif next_month <= 11:
        days_in_next = 30
    else:
        days_in_next = 30 if jdatetime.datetime(next_year, 1, 1).isleap() else 29

    # deadline = آخر ماه بعدی + ۵ روز grace، ساعت ۰۲:۰۰ بامداد
    deadline_j  = jdatetime.datetime(next_year, next_month, days_in_next, 0, 0, 0)
    deadline_j  = deadline_j + jdatetime.timedelta(days=5)
    # ساعت ۰۲:۰۰ بامداد که ترافیک کمه
    target_greg = deadline_j.togregorian().replace(hour=2, minute=0, second=0)

    wait_seconds = _seconds_until(target_greg)

    next_j_str = jdatetime.datetime.fromgregorian(datetime=target_greg).strftime("%Y/%m/%d %H:%M")
    print(f"[SCHEDULER] بررسی بعدی: {next_j_str}  ({wait_seconds/3600:.1f} ساعت دیگه)")

    timer = threading.Timer(wait_seconds, run_ban_check)
    timer.daemon = True   # با بسته شدن ربات thread هم بسته میشه
    timer.start()


def start_scheduler():
    """
    شروع scheduler هنگام راه‌اندازی ربات.
    اگه الان بعد از deadline ماه جاریم و هنوز بن نزدیم، فوری اجرا می‌کنه.
    وگرنه منتظر deadline بعدی میمونه.
    """
    now_greg     = datetime.now()
    deadline_greg = _jalali_month_end()   # deadline ماه جاری (آخر ماه + ۵ روز)

    if now_greg >= deadline_greg:
        # از deadline رد شدیم، فوری چک کن
        print("[SCHEDULER] از deadline رد شدیم — اجرای فوری بررسی بن...")
        timer = threading.Timer(10, run_ban_check)   # ۱۰ ثانیه تأخیر برای آماده شدن ربات
        timer.daemon = True
        timer.start()
    else:
        # منتظر deadline ماه جاری
        wait_seconds = _seconds_until(deadline_greg)
        deadline_j   = jdatetime.datetime.fromgregorian(datetime=deadline_greg)
        print(
            f"[SCHEDULER] بررسی بن برنامه‌ریزی شد: "
            f"{deadline_j.strftime('%Y/%m/%d %H:%M')}  "
            f"({wait_seconds/3600:.1f} ساعت دیگه)"
        )
        timer = threading.Timer(wait_seconds, run_ban_check)
        timer.daemon = True
        timer.start()


# ════════════════════════════════════════════════════════
#  RUN
# ════════════════════════════════════════════════════════

print("ربات شروع به کار کرد...")
start_scheduler()
bot.infinity_polling()