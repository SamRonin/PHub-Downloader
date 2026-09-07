STRINGS = {
    "fa": {
        "start": (
            "👋 سلام <b>{name}</b>!\n\n"
            "🎬 فقط لینک ویدیو Pornhub را برایم بفرست تا کیفیت‌ها و حجم هر کدام را نشانت بدهم، "
            "بعد کیفیت دلخواه را انتخاب کنی و دانلود شروع شود.\n\n"
            "📦 سهمیه روزانه: <b>{quota}</b>\n"
            "⭐ وضعیت: <b>{status}</b>\n\n"
            "👥 با لینک رفرال خودت، به‌ازای هر <b>{ref_needed}</b> کاربرِ جدید <b>{pro_days}</b> روز اشتراک Pro بگیر "
            "(سرعت نامحدود + سهمیه ۲ گیگ):\n"
            "<code>{ref_link}</code>"
        ),
        "help": (
            "ℹ️ <b>راهنما</b>\n\n"
            "• لینک ویدیو را بفرست تا مشخصات و کیفیت‌ها نمایش داده شود.\n"
            "• روی دکمه کیفیت بزن تا دانلود شروع شود.\n"
            "• فایل‌های تا ۵۰ مگ مستقیم در تلگرام ارسال می‌شوند؛ فایل‌های بزرگ‌تر با لینک Pixeldrain.\n"
            "• لینک‌های Pixeldrain به‌صورت خودکار بعد از ۳۰ دقیقه حذف می‌شوند.\n\n"
            "دستورات:\n"
            "/start - شروع و لینک رفرال\n"
            "/lang - تغییر زبان\n"
            "/status - وضعیت سهمیه امروز"
        ),
        "status": (
            "📊 <b>وضعیت شما</b>\n\n"
            "⭐ اشتراک: <b>{status}</b>\n"
            "📥 مصرف امروز: <b>{used}</b> از <b>{quota}</b>\n"
            "👥 رفرال‌های تأییدشده: <b>{refs}</b>\n"
            "🎬 کل دانلودها: <b>{dls}</b>"
        ),
        "lang_set": "🌐 زبان به فارسی تغییر کرد.",
        "send_link": "لطفاً لینک ویدیو Pornhub را بفرست.",
        "invalid_link": "❌ این لینک معتبر نیست. لطفاً فقط لینک ویدیو Pornhub را بفرست.",
        "fetching": "🔎 در حال دریافت اطلاعات ویدیو...",
        "expired": "⌛ این پیام منقضی شده است. لطفاً لینک را دوباره بفرست.",
        "banned": "⛔ دسترسی شما مسدود شده است.",
        "already_processing": "⏳ یک دانلود دیگر در حال انجام است. لطفاً صبر کن.",
        "quota_exceeded": (
            "❌ سهمیه روزانه‌ات تمام شده یا برای این کیفیت کافی نیست.\n"
            "📥 مصرف امروز: <b>{used}</b> از <b>{quota}</b>\n"
            "⭐ با اشتراک Pro سهمیه‌ات ۲ گیگ و سرعتت نامحدود می‌شود."
        ),
        "downloading": "⏳ در حال دانلود... {pct}%",
        "uploading": "☁️ در حال آپلود به Pixeldrain... {pct}%",
        "sending": "📤 در حال ارسال فایل در تلگرام...",
        "done_direct": "✅ دانلود کامل شد!\n🎬 کیفیت: <b>{quality}p</b>\n📦 حجم: <b>{size}</b>",
        "done_link": (
            "✅ آماده شد!\n🎬 کیفیت: <b>{quality}p</b>\n📦 حجم: <b>{size}</b>\n\n"
            "⬇️ <b>لینک دانلود:</b>\n{link}\n\n"
            "🗑 این لینک تا <b>{minutes}</b> دقیقه دیگر به‌صورت خودکار حذف می‌شود."
        ),
        "direct_btn": "⬇️ دانلود مستقیم",
        "failed": "❌ خطا در دانلود. لطفاً بعداً دوباره امتحان کن.",
        "too_big": "❌ حجم این ویدیو از حد مجاز ({max}) بیشتر است.",
        "views": "👀 {n} بازدید",
        "sizes_header": "📦 حجم هر کیفیت:",
        "pro_active": "⭐ Pro فعال (تا {date})",
        "free_user": "کاربر عادی",
        "pro_granted": (
            "🎉 <b>تبریک!</b> اشتراک Pro شما فعال شد (تا {date}).\n"
            "✅ سرعت دانلود نامحدود\n✅ سهمیه روزانه ۲ گیگ"
        ),
        "ref_ok": "✅ رفرال شما با موفقیت ثبت شد! با {refs} رفرالِ جدیدِ بعدی به اشتراک Pro می‌رسی.",
        "not_new": "⚠️ فقط کاربران جدید با لینک رفرال می‌توانند معرفی شوند.",
        "sizes_unknown": "نامشخص",
        "admin_denied": "⛔ فقط ادمین‌ها به این بخش دسترسی دارند.",
        "admin_panel": "🛠 <b>پنل ادمین</b>\n\n👥 کاربران: <b>{users}</b>\n⭐ پرو فعال: <b>{pro}</b>\n📥 دانلود امروز: <b>{dls}</b>\n☁️ فایل فعال Pixeldrain: <b>{files}</b>",
        "btn_stats": "📊 آمار",
        "btn_users": "👥 کاربران",
        "btn_broadcast": "📢 پیام همگانی",
        "btn_settings": "⚙️ تنظیمات",
        "btn_files": "☁️ فایل‌های Pixeldrain",
        "btn_back": "🔙 بازگشت",
        "stats_full": (
            "📊 <b>آمار کامل</b>\n\n"
            "👥 کل کاربران: <b>{total_users}</b>\n"
            "⭐ پرو فعال: <b>{pro_users}</b>\n"
            "📥 دانلود امروز: <b>{dl_today}</b>\n"
            "📦 حجم امروز: <b>{bytes_today}</b>\n"
            "📦 حجم کل: <b>{bytes_total}</b>\n"
            "☁️ فایل فعال Pixeldrain: <b>{active_uploads}</b>"
        ),
        "user_page": "👥 کاربران (صفحه {page} از {pages})",
        "user_card": (
            "👤 <b>{name}</b> (<code>{uid}</code>)\n"
            "یوزرنیم: {username}\n"
            "⭐ {status}\n"
            "📥 امروز: {used_today} | کل: {bytes_total}\n"
            "👥 رفرال: {refs} ({available} قابل استفاده)"
        ),
        "btn_ban": "🔨 بن",
        "btn_unban": "✅ آنبن",
        "btn_give_pro": "⭐ پرو ۳۰ روزه",
        "btn_remove_pro": "❌ حذف پرو",
        "btn_reset_quota": "🔄 ریست سهمیه",
        "done": "✅ انجام شد.",
        "broadcast_prompt": "📢 پیام یا مدیای خود را بفرست تا برای همه کاربران ارسال شود.\nبرای لغو /cancel را بزن.",
        "broadcast_done": "✅ پیام برای <b>{sent}</b> کاربر ارسال شد ({failed} ناموفق).",
        "broadcast_cancelled": "❌ لغو شد.",
        "settings_header": "⚙️ <b>تنظیمات</b> (برای ویرایش روی مقدار بزن)",
        "settings_hint": "مقدار جدید را بفرست (فقط عدد). برای لغو /cancel را بزن.",
        "settings_saved": "✅ ذخیره شد: {key} = {value}",
        "files_list": "☁️ <b>فایل‌های فعال Pixeldrain</b>",
        "no_files": "فایل فعالی وجود ندارد.",
        "btn_delete": "🗑 حذف",
        "file_deleted": "🗑 فایل حذف شد.",
    },
    "en": {
        "start": (
            "👋 Hi <b>{name}</b>!\n\n"
            "🎬 Just send me a Pornhub video link and I'll show all available qualities with sizes. "
            "Pick one and I'll download it for you.\n\n"
            "📦 Daily quota: <b>{quota}</b>\n"
            "⭐ Status: <b>{status}</b>\n\n"
            "👥 With your referral link, every <b>{ref_needed}</b> new users grant you <b>{pro_days}</b> days of Pro "
            "(unlimited speed + 2 GB quota):\n"
            "<code>{ref_link}</code>"
        ),
        "help": (
            "ℹ️ <b>Help</b>\n\n"
            "• Send a video link to see qualities and sizes.\n"
            "• Tap a quality button to start the download.\n"
            "• Files up to 50 MB are sent directly in Telegram; larger files get a Pixeldrain link.\n"
            "• Pixeldrain links are auto-deleted after 30 minutes.\n\n"
            "Commands:\n"
            "/start - start & referral link\n"
            "/lang - switch language\n"
            "/status - today's quota status"
        ),
        "status": (
            "📊 <b>Your status</b>\n\n"
            "⭐ Plan: <b>{status}</b>\n"
            "📥 Used today: <b>{used}</b> / <b>{quota}</b>\n"
            "👥 Verified referrals: <b>{refs}</b>\n"
            "🎬 Total downloads: <b>{dls}</b>"
        ),
        "lang_set": "🌐 Language switched to English.",
        "send_link": "Please send a Pornhub video link.",
        "invalid_link": "❌ Invalid link. Please send a Pornhub video link only.",
        "fetching": "🔎 Fetching video info...",
        "expired": "⌛ This message expired. Please send the link again.",
        "banned": "⛔ You are banned.",
        "already_processing": "⏳ Another download is already in progress. Please wait.",
        "quota_exceeded": (
            "❌ Your daily quota is used up or not enough for this quality.\n"
            "📥 Used today: <b>{used}</b> / <b>{quota}</b>\n"
            "⭐ Pro gives you 2 GB/day and unlimited speed."
        ),
        "downloading": "⏳ Downloading... {pct}%",
        "uploading": "☁️ Uploading to Pixeldrain... {pct}%",
        "sending": "📤 Sending file to Telegram...",
        "done_direct": "✅ Done!\n🎬 Quality: <b>{quality}p</b>\n📦 Size: <b>{size}</b>",
        "done_link": (
            "✅ Ready!\n🎬 Quality: <b>{quality}p</b>\n📦 Size: <b>{size}</b>\n\n"
            "⬇️ <b>Download link:</b>\n{link}\n\n"
            "🗑 This link will be auto-deleted in <b>{minutes}</b> minutes."
        ),
        "direct_btn": "⬇️ Direct download",
        "failed": "❌ Download failed. Please try again later.",
        "too_big": "❌ This video exceeds the maximum allowed size ({max}).",
        "views": "👀 {n} views",
        "sizes_header": "📦 Available sizes:",
        "pro_active": "⭐ Pro until {date}",
        "free_user": "Free user",
        "pro_granted": (
            "🎉 <b>Congrats!</b> Your Pro subscription is active (until {date}).\n"
            "✅ Unlimited download speed\n✅ 2 GB daily quota"
        ),
        "ref_ok": "✅ Referral registered! {refs} more new users = Pro subscription.",
        "not_new": "⚠️ Only brand-new users can be referred.",
        "sizes_unknown": "unknown",
        "admin_denied": "⛔ Admins only.",
        "admin_panel": "🛠 <b>Admin Panel</b>\n\n👥 Users: <b>{users}</b>\n⭐ Pro: <b>{pro}</b>\n📥 Downloads today: <b>{dls}</b>\n☁️ Active Pixeldrain files: <b>{files}</b>",
        "btn_stats": "📊 Stats",
        "btn_users": "👥 Users",
        "btn_broadcast": "📢 Broadcast",
        "btn_settings": "⚙️ Settings",
        "btn_files": "☁️ Pixeldrain files",
        "btn_back": "🔙 Back",
        "stats_full": (
            "📊 <b>Full stats</b>\n\n"
            "👥 Total users: <b>{total_users}</b>\n"
            "⭐ Pro: <b>{pro_users}</b>\n"
            "📥 Downloads today: <b>{dl_today}</b>\n"
            "📦 Bytes today: <b>{bytes_today}</b>\n"
            "📦 Bytes total: <b>{bytes_total}</b>\n"
            "☁️ Active Pixeldrain files: <b>{active_uploads}</b>"
        ),
        "user_page": "👥 Users (page {page}/{pages})",
        "user_card": (
            "👤 <b>{name}</b> (<code>{uid}</code>)\n"
            "Username: {username}\n"
            "⭐ {status}\n"
            "📥 Today: {used_today} | Total: {bytes_total}\n"
            "👥 Referrals: {refs} ({available} available)"
        ),
        "btn_ban": "🔨 Ban",
        "btn_unban": "✅ Unban",
        "btn_give_pro": "⭐ Grant Pro 30d",
        "btn_remove_pro": "❌ Remove Pro",
        "btn_reset_quota": "🔄 Reset quota",
        "done": "✅ Done.",
        "broadcast_prompt": "📢 Send the message/media to broadcast to all users.\nSend /cancel to abort.",
        "broadcast_done": "✅ Sent to <b>{sent}</b> users ({failed} failed).",
        "broadcast_cancelled": "❌ Cancelled.",
        "settings_header": "⚙️ <b>Settings</b> (tap a value to edit)",
        "settings_hint": "Send the new numeric value. Send /cancel to abort.",
        "settings_saved": "✅ Saved: {key} = {value}",
        "files_list": "☁️ <b>Active Pixeldrain files</b>",
        "no_files": "No active files.",
        "btn_delete": "🗑 Delete",
        "file_deleted": "🗑 File deleted.",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    lang = lang if lang in STRINGS else "en"
    text = STRINGS[lang].get(key) or STRINGS["en"].get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text
