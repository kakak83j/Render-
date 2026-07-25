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

# UptimeRobot API Keys (Main Key is required for creating monitors)
UPTIMEROBOT_MAIN_API_KEY = "u3653759-82ca8e1254d565d3aaf5b0a0"
UPTIMEROBOT_READ_API_KEY = "ur3653759-cbc6d8bc5a4d10bdd4f17825" # Future use for /status command

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
app_flask = Flask(__name__)

@app_flask.route("/health")
def health():
    return jsonify({"status": "ok", "bot": "running"}), 200

@app_flask.route("/")
def home():
    return jsonify({
        "bot": "URL Monitor Bot",
        "endpoints": {"/health": "Health check", "/": "This info"},
        "commands": {"/start": "Show help", "/monitor <url>": "Add URL to monitoring"}
    }), 200

def run_flask():
    app_flask.run(host="0.0.0.0", port=PORT, debug=False)

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
    result = {"success": False, "message": "", "data": None}

    if not UPTIMEROBOT_MAIN_API_KEY:
        result["message"] = "❌ UptimeRobot: API key missing"
        return result

    friendly_name = extract_domain(url)

    # Use Dictionary instead of string to auto-handle URL encoding correctly
    payload = {
        "api_key": UPTIMEROBOT_MAIN_API_KEY,
        "format": "json",
        "type": 1,
        "url": url,
        "friendly_name": friendly_name
    }

    try:
        resp = requests.post(
            "https://api.uptimerobot.com/v2/newMonitor",
            data=payload,
            timeout=30
        )

        if resp.status_code == 200:
            data = resp.json()
            if data.get("stat") == "ok":
                monitor_id = data.get("monitor", {}).get("id", "N/A")
                result["success"] = True
                result["message"] = f"✅ UptimeRobot Added (ID: {monitor_id}, 5 min)"
                result["data"] = {"monitorId": monitor_id}
            else:
                err_msg = data.get("error", {}).get("message", "Unknown error")
                if "already exists" in err_msg.lower():
                    result["success"] = True
                    result["message"] = "⚠️ UptimeRobot: Already monitored"
                else:
                    result["message"] = f"❌ UptimeRobot: {err_msg}"
        else:
            result["message"] = f"❌ UptimeRobot: HTTP {resp.status_code} — API Error."

    except Exception as e:
        result["message"] = f"❌ UptimeRobot: {str(e)}"

    return result

# ──── Cron-job.org Integration (1-MINUTE INTERVAL) ────
def add_cronjob(url: str) -> dict:
    result = {"success": False, "message": "", "data": None}

    if not CRONJOB_API_KEY or not CRONJOB_API_USER:
        result["message"] = "❌ Cron-job: API key or email missing"
        return result

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CRONJOB_API_KEY}",
    }

    friendly_name = extract_domain(url)

    payload = {
        "job": {
            "enabled": True,
            "saveResponses": True,
            "title": f"Monitor - {friendly_name}",
            "url": url,
            "requestMethod": 1,
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
                "hours": [-1],
                "minutes": list(range(0, 60)),  # HAR 1 MINUTE!
                "mdays": [-1],
                "months": [-1],
                "wdays": [-1]
            }
        }
    }

    try:
        resp = requests.put("https://api.cron-job.org/jobs", headers=headers, json=payload, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            job_id = data.get("jobId", "N/A")
            result["success"] = True
            result["message"] = f"✅ Cron-job Added (ID: {job_id}, 1 min interval)"
            result["data"] = {"jobId": job_id}
        elif resp.status_code == 403:
            result["message"] = "❌ Cron-job: API key invalid or IP not allowed"
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
        await update.message.reply_text("❌ *Usage:* `/monitor <url>`\n\nExample:\n`/monitor https://myapp.onrender.com`", parse_mode="Markdown")
        return

    raw_url = " ".join(context.args)
    url = normalize_url(raw_url)
    domain = extract_domain(url)

    if not re.match(r'^https?://[^\s/$.?#].[^\s]*$', url):
        await update.message.reply_text(f"❌ Invalid URL: `{raw_url}`", parse_mode="Markdown")
        return

    processing_msg = await update.message.reply_text(f"⏳ `{url}` को monitor करने के लिए जोड़ रहा हूँ...", parse_mode="Markdown")
    await update.message.chat.send_action("typing")

    ur_result = add_uptimerobot(url)
    cj_result = add_cronjob(url)

    msg_parts = [
        f"📊 *Monitor Status — `{domain}`*",
        f"🔗 `{url}`\n",
        f"{'🟢' if ur_result['success'] else '🔴'} *UptimeRobot:* {ur_result['message']}",
        f"{'🟢' if cj_result['success'] else '🔴'} *Cron-job:* {cj_result['message']}",
        f"\n{'✅' if ur_result['success'] and cj_result['success'] else '⚠️'} *Result:* {sum([ur_result['success'], cj_result['success']])}/2 services configured",
        "\n💡 *Tips:*",
        "• UptimeRobot → 5 min interval (free plan fixed)",
        "• Cron-job → **1 min interval** 🔥",
        "• Dashboards check करे for detailed stats"
    ]
    msg = "\n".join(msg_parts)

    keyboard = [
        [InlineKeyboardButton("📊 UptimeRobot Dashboard", url="https://dashboard.uptimerobot.com/")],
        [InlineKeyboardButton("🔄 Cron-job Dashboard", url="https://console.cron-job.org/")],
    ]

    await processing_msg.edit_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    logger.info(f"Monitor added: {url} | UR: {ur_result['success']} | CJ: {cj_result['success']}")

# ──── Start Command ────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config_status = [
        "✅ TELEGRAM_BOT_TOKEN" if TELEGRAM_BOT_TOKEN else "❌ TELEGRAM_BOT_TOKEN",
        "✅ UPTIMEROBOT_MAIN_API_KEY" if UPTIMEROBOT_MAIN_API_KEY else "❌ UPTIMEROBOT_MAIN_API_KEY",
        "✅ CRONJOB_API_KEY" if CRONJOB_API_KEY else "❌ CRONJOB_API_KEY",
        "✅ CRONJOB_API_USER" if CRONJOB_API_USER else "❌ CRONJOB_API_USER",
    ]
    msg = (
        "🤖 *URL Monitor Bot*\n\n"
        "एक ही command से किसी भी URL को दो monitoring services पर add करें।\n\n"
        "*/monitor <url>* — URL को monitor करें\n\n"
        "*Examples:*\n"
        "`/monitor https://myapp.onrender.com`\n"
        "`/monitor google.com`\n\n"
        "*Config Status:*\n" + "\n".join(config_status) +
        "\n\n*Cron-job:* 🔥 **1 Minute interval**\n"
        "*UptimeRobot:* ⏱️ **5 Minute interval** (free plan)\n\n"
        "_Powered by HackerAI_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Unknown command\n\n*/start* — Help\n*/monitor <url>* — URL monitor करें", parse_mode="Markdown")

def main():
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set!")
        return

    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"✅ Flask health server running on port {PORT}")

    # Build bot application
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("monitor", monitor_url))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    
    logger.info("🤖 URL Monitor Bot started!")

    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook")
    except:
        pass

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
    
