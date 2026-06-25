# Texts.py — All Persian user-facing strings used by the bot.
# Edit this file to change messages shown to users without touching the bot logic.

texts = {
    # ── Access & Auth ────────────────────────────────────────────────
    "no_access":                  "⛔️ دسترسی ندارید. با ادمین تماس بگیرید.",
    "no_access_short":            "⛔️ دسترسی ندارید.",
    "superadmin_welcome":         "🔑 پنل سوپرادمین",
    "admin_welcome":              "🛠 پنل ادمین",

    # ── Status Labels ────────────────────────────────────────────────
    "status_banned":              "🚫 مسدود",
    "status_active":              "✅ فعال",

    # ── Orders ───────────────────────────────────────────────────────
    "order_filter_prompt":        "📋 سفارش‌های من\nفیلتر انتخاب کن:",
    "order_not_found":            "❌ سفارش پیدا نشد.",
    "order_cancel_error":         "⚠️ فقط سفارش‌های در انتظار قابل لغو است.",
    "order_not_editable":         "⚠️ قابل ویرایش نیست.",
    "order_cancelled_msg":        "❌ سفارش لغو شد.",
    "edit_cancelled_msg":         "❌ ویرایش لغو شد.",
    "no_active_models":           "⚠️ هیچ مدل فعالی وجود ندارد.",
    "select_at_least_one":        "⚠️ حداقل یک مدل انتخاب کن.",
    "invalid_date":               "❌ تاریخ شمسی معتبر نیست. فرمت: ۱۴۰۳/۰۶/۱۵",
    "positive_number":            "❌ عدد مثبت:",
    "account_banned_msg_user":    "🚫 حساب شما مسدود است.\nبا ادمین تماس بگیرید.",

    # ── Finance ──────────────────────────────────────────────────────
    "no_transactions":            "📜 هیچ تراکنشی ثبت نشده.",
    "no_active_installments":     "📋 هیچ قسط فعالی ندارید.",
    "no_pending_payments":        "⏳ هیچ پرداختی در انتظار تایید ندارید.",
    "no_free_debt":               "✅ بدهی آزاد ندارید!",
    "installment_not_found":      "❌ قسط پیدا نشد یا قبلاً تکمیل شده.",
    "installment_already_paid":   "✅ این قسط قبلاً کامل پرداخت شده.",
    "send_receipt":               "📎 فیش واریزی رو ارسال کن:",
    "send_image_or_file":         "❌ لطفاً تصویر یا فایل ارسال کن:",
    "payment_cancelled":          "❌ پرداخت لغو شد.",

    # ── Admin Panel ──────────────────────────────────────────────────
    "admin_orders_filter":        "📋 سفارشات\nفیلتر:",
    "already_processed":          "⚠️ قبلاً پردازش شده.",

    # ── SuperAdmin – Users ───────────────────────────────────────────
    "user_not_found":             "❌ کاربر پیدا نشد.",
    "no_orders_for_user":         "📦 این کاربر هیچ سفارشی ندارد.",
    "no_transactions_for_user":   "💳 این کاربر هیچ تراکنشی ندارد.",
    "no_installments_for_user":   "📋 این کاربر هیچ قسطی ندارد.",
    "user_banned_msg":            "🚫 کاربر مسدود شد.",
    "user_unbanned_msg":          "✅ مسدودی رفع شد.",
    "user_deactivated_msg":       "🗑 کاربر غیرفعال شد.",
    "confirm_delete_user":        "⚠️ حذف کامل کاربر\nهمه سفارشات و تراکنش‌ها هم پاک میشن. مطمئنی؟",
    "no_current_debt":            "⚠️ بدهی ماه جاری ندارد.",
    "user_already_registered":    "⚠️ این کاربر قبلاً ثبت شده.",
    "cant_delete_self":           "⚠️ نمی‌تونی خودتو حذف کنی.",
    "ban_reason_admin":           "مسدود توسط سوپرادمین",
    "deactivated_by_admin":       "حذف شده توسط سوپرادمین",
    "account_banned_by_admin":    "🚫 حساب شما مسدود شد. با ادمین تماس بگیرید.",
    "account_unbanned":           "✅ مسدودی حساب شما رفع شد.",

    # ── SuperAdmin – Models ──────────────────────────────────────────
    "model_not_found":            "❌ مدل پیدا نشد.",
    "confirm_delete_model":       "⚠️ حذف مدل\nمطمئنی؟ (سفارشات قبلی تاثیر می‌گیرن)",

    # ── SuperAdmin – Admins ──────────────────────────────────────────
    "admin_not_found":            "❌ پیدا نشد.",
    "admin_already_exists":       "⚠️ این کاربر قبلاً ادمین است.",
    "admin_added_notification":   "🎉 شما به عنوان ادمین اضافه شدید!\n/start را بزنید.",

    # ── Bot → User Notifications ─────────────────────────────────────
    "account_activated":          "🎉 حساب شما فعال شد!\n/start را بزنید.",
    "db_error_msg":               "⚠️ خطا در ارتباط با دیتابیس. لطفاً چند لحظه دیگر دوباره امتحان کنید.",
    "unexpected_error_msg":       "⚠️ خطای غیرمنتظره رخ داد. به ادمین اطلاع داده شد.",
    "receipt_not_shown":          "⚠️ نمایش رسید ممکن نشد.",

    # ── SuperAdmin – Broadcast ───────────────────────────────────────
    "broadcast_prompt":           "📣 پیامی که می‌خوای به همه کاربران ارسال بشه رو بنویس:",

    # ── Helpers ──────────────────────────────────────────────────────
    "cart_header":                "🛒 سبد سفارش:",
    "order_summary_header":       "🧾 خلاصه سفارش:\n",
    "cancel_order_history":       "لغو سفارش",
    "debt_transfer_desc":         "انتقال بدهی ماه جاری به قبلی",
}