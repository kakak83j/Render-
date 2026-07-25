import os
import re
import logging
import requests
import json
from urllib.parse import urlparse
from flask import Flask, jsonify
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ──── CONFIG ────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8649183907:AAHLQZ2F-gW5yqn2cn19nB3HrdhEueNLA5U")
UPTIMEROBOT_API_KEY = "u3653759-476d6400f73a515c9c5bd7fd"   # 👈 Hardcoded
CRONJOB_API_KEY = os.environ.get("CRONJOB_API_KEY", "")
CRONJOB_API_USER = os.environ.get("CRONJOB_API_USER", "")
PORT = int(os.environ.get("PORT", 8080))
# ───────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ──── Flask Health Check ────
app = Flask(__name__)

@app.route("/health")
def health():
    return jsonify({"status": "ok", "bot": "running"}), 200

@app.route("/")
def home():
    return jsonify({
        "bot": "URL Monitor Bot",
        "endpoints": {
            "/health": "Health check",
            "/": "This info"
        },
        "commands": {
            "/start": "Show help",
            "/monitor <url>": "Add URL to monitoring"
        }
    }), 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False)

# ──── Helper Functions ────
def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url

def extract_domain(url: str) -> str:
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    domain = domain.replace("www.", "")
    return domain[:30]

# ──── UptimeRobot Integration ────
def add_uptimerobot(url: str) -> dict:
    """
    UptimeRobot API v2 - newMonitor
    Free plan: 5-min interval fixed
    """
    result = {
        "success": False,
        "message": "",
        "data": None
    }

    if not UPTIMEROBOT_API_KEY:
        result["message"] = "❌ UptimeRobot: API key missing"
        return result

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Cache-Control": "no-cache"
    }

    friendly_name = extract_domain(url)

    payload = {
        "api_key": UPTIMEROBOT_API_KEY,
        "format": "json",
        "type": 1,           # 1 = HTTP(s) monitor
        "url": url,
        "friendly_name": friendly_name,
        "alert_contacts": "",
    }

    try:
        resp = requests.post(
            "https://api.uptimerobot.com/v2/newMonitor",
            headers=headers,
            data=payload,
            timeout=30
        )

        if resp.status_code == 200:
            data = resp.json()
            stat = data.get("stat", "fail")

            if stat == "ok":
                monitor_id = data.get("monitor", {}).get("id", "N/A")
                result["success"] = True
                result["message"] = f"✅ Added (ID: {monitor_id}, 5 min interval - free plan)"
                result["data"] = {"monitorId": monitor_id}
            else:
                err_msg = data.get("error", {}).get("message", "Unknown error")
                result["message"] = f"❌ UptimeRobot: {err_msg}"

                # Handle duplicate
                if "already exists" in err_msg.lower():
                    result["success"] = True
                    result["message"] = "⚠️ UptimeRobot: Already monitored (duplicate)"
        else:
            result["message"] = f"❌ UptimeRobot: HTTP {resp.status_code}"

    except requests.exceptions.Timeout:
        result["message"] = "❌ UptimeRobot: Request timed out"
    except Exception as e:
        result["message"] = f"❌ UptimeRobot: {str(e)}"

    return result

# ──── Cron-job.org Integration (1-MINUTE INTERVAL) ────
def add_cronjob(url: str) -> dict:
    """
    cron-job.org REST API
    Schedule set to EVERY 1 MINUTE
    """
    result = {
        "success": False,
        "message": "",
        "data": None
    }

    if not CRONJOB_API_KEY or not CRONJOB_API_USER:
        result["message"] = "❌ Cron-job: API key or email missing"
        return result

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CRONJOB_API_KEY}",
        "Cache-Control": "no-cache"
    }

    friendly_name = extract_domain(url)

    payload = {
        "job": {
            "enabled": True,
            "saveResponses": True,
            "title": f"Monitor - {friendly_name}",
            "url": url,
            "requestMethod": 1,   # 1 = GET
            "notification": {
                "email": True,
                "emailAddress": CRONJOB_API_USER,
                "onFailure": True,
                "onSuccess": False,
                "onDisabled": True
            },
            "schedule": {
                "timezone": "Asia/Kolkata",
                "expiresAt": 0,
                "hours": [-1],                  # Har ghante
                "minutes": list(range(0, 60)),  # HAR 1 MINUTE!
                "mdays": [-1],                  # Har din
                "months": [-1],                 # Har mahina
                "wdays": [-1]                   # Har din hafta
            }
        }
    }

    try:
        # PUT method
        resp = requests.put(
            "https://api.cron-job.org/jobs",
            headers=headers,
            json=payload,
            timeout=30
        )

        if resp.status_code == 200:
            data = resp.json()
            job_id = data.get("jobId", "N/A")
            result["success"] = True
            result["message"] = f"✅ Added (ID: {job_id}, 1 min interval)"
            result["data"] = {"jobId": job_id}
        elif resp.status_code == 400:
            result["message"] = "❌ Cron-job: Invalid data — check format"
        elif resp.status_code == 403:
            result["message"] = "❌ Cron-job: API key invalid or IP not allowed"
        elif resp.status_code == 404:
            result["message"] = "❌ Cron-job: API endpoint not found"
        else:
            try:
                err = resp.json()
                result["message"] = f"❌ Cron-job: {json.dumps(err)}"
            except:
                result["message"] = f"❌ Cron-job: HTTP {resp.status_code}"

    except Exception as e:
        result["message"] = f"❌ Cron-job: {str(e)}"

    return result

# ──── Monitor Command ────
async def monitor_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❌ *Usage:* `/monitor <url>`\n\n"
            "Example:\n"
            "`/monitor https://myapp.onrender.com`\n"
            "`/monitor google.com`",
            parse_mode="Markdown"
        )
        return

    raw_url = " ".join(context.args)
    url = normalize_url(raw_url)
    domain = extract_domain(url)

    if not re.match(r'^https?://[^\s/$.?#].[^\s]*$', url):
        await update.message.reply_text(
            f"❌ Invalid URL: `{raw_url}`\nकृपया सही URL भेजें।",
            parse_mode="Markdown"
        )
        return

    processing_msg = await update.message.reply_text(
        f"⏳ `{url}` को monitor करने के लिए जोड़ रहा हूँ...",
        parse_mode="Markdown"
    )

    await update.message.chat.send_action("typing")

    ur_result = add_uptimerobot(url)
    cj_result = add_cronjob(url)

    msg_parts = []

    msg_parts.append(f"📊 *Monitor Status — `{domain}`*")
    msg_parts.append(f"🔗 `{url}`\n")

    ur_emoji = "🟢" if ur_result["success"] else "🔴"
    msg_parts.append(f"{ur_emoji} *UptimeRobot:* {ur_result['message']}")

    cj_emoji = "🟢" if cj_result["success"] else "🔴"
    msg_parts.append(f"{cj_emoji} *Cron-job:* {cj_result['message']}")

    success_count = sum([ur_result["success"], cj_result["success"]])
    msg_parts.append(f"\n{'✅' if success_count == 2 else '⚠️'} *Result:* {success_count}/2 services configured")

    msg_parts.append("\n💡 *Tips:*")
    msg_parts.append("• UptimeRobot → 5 min interval (free plan fixed)")
    msg_parts.append("• Cron-job → **1 min interval** 🔥")
    msg_parts.append("• Dashboards check karein for detailed stats")

    msg = "\n".join(msg_parts)

    keyboard = [
        [InlineKeyboardButton("📊 UptimeRobot Dashboard", url="https://uptimerobot.com/dashboard")],
        [InlineKeyboardButton("🔄 Cron-job Dashboard", url="https://cron-job.org/en/dashboard/")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await processing_msg.edit_text(msg, parse_mode="Markdown", reply_markup=reply_markup)
    logger.info(f"Monitor added: {url} | UR: {ur_result['success']} | CJ: {cj_result['success']}")

# ──── Start Command ────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config_status = []
    config_status.append("✅ TELEGRAM_BOT_TOKEN" if TELEGRAM_BOT_TOKEN else "❌ TELEGRAM_BOT_TOKEN")
    config_status.append("✅ UPTIMEROBOT_API_KEY" if UPTIMEROBOT_API_KEY else "❌ UPTIMEROBOT_API_KEY")
    config_status.append("✅ CRONJOB_API_KEY" if CRONJOB_API_KEY else "❌ CRONJOB_API_KEY")
    config_status.append("✅ CRONJOB_API_USER" if CRONJOB_API_USER else "❌ CRONJOB_API_USER")

    msg = (
        "🤖 *URL Monitor Bot*\n\n"
        "एक ही command से किसी भी URL को दो monitoring services "
        "(UptimeRobot + Cron-job.org) पर add करें।\n\n"
        "*/monitor <url>* — URL को monitor करें\n\n"
        "*Examples:*\n"
        "`/monitor https://myapp.onrender.com`\n"
        "`/monitor google.com`\n\n"
        "*Config Status:*\n"
        + "\n".join(config_status) +
        "\n\n*Cron-job Interval:* 🔥 **1 Minute**\n\n"
        "_Powered by HackerAI_"
    )

    await update.message.reply_text(msg, parse_mode="Markdown")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Unknown command\n\n"
        "*/start* — Help\n"
        "*/monitor <url>* — URL monitor करें",
        parse_mode="Markdown"
    )

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set!")
        return

    if not UPTIMEROBOT_API_KEY:
        logger.warning("⚠️ UPTIMEROBOT_API_KEY not set — UptimeRobot won't work")
    if not CRONJOB_API_KEY or not CRONJOB_API_USER:
        logger.warning("⚠️ CRONJOB_API_KEY/USER not set — Cron-job won't work")

    # Start Flask health server
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"✅ Flask health server running on port {PORT}")

    # Build Telegram bot
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("monitor", monitor_url))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("🤖 URL Monitor Bot started! (Cron-job: 1 min interval)")

    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook")
        logger.info("✅ Webhook deleted (polling mode)")
    except:
        pass

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
