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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
UPTIMEROBOT_API_KEY = os.environ.get("UPTIMEROBOT_API_KEY", "")
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
            "/monitor <URL>": "Add URL to monitoring"
        }
    }), 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False)

# ──── Helper Functions ────

def normalize_url(url: str) -> str:
    """Add https:// if no protocol specified."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url

def extract_domain(url: str) -> str:
    """Extract clean domain name for friendly name."""
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    domain = domain.replace("www.", "")
    return domain[:30]  # Keep it short

# ──── UptimeRobot Integration ────

def add_uptimerobot(url: str) -> dict:
    """
    Add monitor to UptimeRobot via API v2.
    Free plan: 5-minute interval, 50 monitors max.
    """
    result = {"success": False, "message": "", "data": None}
    
    if not UPTIMEROBOT_API_KEY:
        result["message"] = "❌ UPTIMEROBOT_API_KEY not configured"
        return result
    
    friendly_name = extract_domain(url)
    
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache"
    }
    
    payload = {
        "api_key": UPTIMEROBOT_API_KEY,
        "format": "json",
        "type": 1,  # HTTP(s)
        "url": url,
        "friendly_name": friendly_name,
        "interval": 300,  # 5 minutes (free plan)
    }
    
    try:
        resp = requests.post(
            "https://api.uptimerobot.com/v2/newMonitor",
            headers=headers,
            json=payload,
            timeout=30
        )
        data = resp.json()
        
        if data.get("stat") == "ok":
            monitor = data.get("monitor", [{}])[0]
            result["success"] = True
            result["message"] = f"✅ Added (ID: {monitor.get('id', 'N/A')})"
            result["data"] = monitor
        else:
            error_msg = data.get("error", {}).get("message", "Unknown error")
            result["message"] = f"❌ UptimeRobot: {error_msg}"
            
    except requests.exceptions.Timeout:
        result["message"] = "❌ UptimeRobot: Timeout"
    except requests.exceptions.ConnectionError:
        result["message"] = "❌ UptimeRobot: Connection failed"
    except Exception as e:
        result["message"] = f"❌ UptimeRobot: {str(e)}"
    
    return result


# ──── Cron-job.org Integration ────

def add_cronjob(url: str) -> dict:
    """
    Add cron job to Cron-job.org via API v1.
    Free plan: 1-minute interval.
    """
    result = {"success": False, "message": "", "data": None}
    
    if not CRONJOB_API_KEY or not CRONJOB_API_USER:
        result["message"] = "❌ CRONJOB_API_KEY or CRONJOB_API_USER not configured"
        return result
    
    friendly_name = extract_domain(url)
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CRONJOB_API_KEY}",
    }
    
    # Cron-job.org v1 API: POST /jobs
    # Body: { title, url, schedule (cron expression), email, enabled }
    payload = {
        "title": f"Monitor - {friendly_name}",
        "url": url,
        "schedule": {
            "cronExpression": "* * * * *",  # Every 1 minute
            "timezone": "UTC"
        },
        "notification": {
            "email": True,
            "emailAddress": CRONJOB_API_USER,
            "onFailure": True,
            "onSuccess": False,
            "onDisabled": True
        },
        "enabled": True,
        "saveResponses": True,
        "requestMethod": 1  # GET
    }
    
    try:
        resp = requests.post(
            "https://api.cron-job.org/jobs",
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if resp.status_code in [200, 201]:
            data = resp.json()
            job_id = data.get("jobId", "N/A")
            result["success"] = True
            result["message"] = f"✅ Added (ID: {job_id})"
            result["data"] = {"jobId": job_id}
        elif resp.status_code == 409:
            result["message"] = "❌ Cron-job: Duplicate entry (already added)"
        else:
            try:
                err = resp.json().get("error", "Unknown error")
            except:
                err = f"HTTP {resp.status_code}"
            result["message"] = f"❌ Cron-job: {err}"
            
    except requests.exceptions.Timeout:
        result["message"] = "❌ Cron-job: Timeout"
    except requests.exceptions.ConnectionError:
        result["message"] = "❌ Cron-job: Connection failed"
    except Exception as e:
        result["message"] = f"❌ Cron-job: {str(e)}"
    
    return result


# ──── Monitor Command ────

async def monitor_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /monitor <URL>"""
    
    if not context.args:
        await update.message.reply_text(
            "❌ *Usage:* `/monitor <URL>`\n\n"
            "Example:\n"
            "`/monitor https://myapp.onrender.com`\n"
            "`/monitor google.com`",
            parse_mode="Markdown"
        )
        return
    
    raw_url = " ".join(context.args)
    url = normalize_url(raw_url)
    domain = extract_domain(url)
    
    # Validate URL
    if not re.match(r'^https?://[^\s/$.?#].[^\s]*$', url):
        await update.message.reply_text(
            f"❌ Invalid URL: `{raw_url}`\nकृपया सही URL भेजें।",
            parse_mode="Markdown"
        )
        return
    
    # Send processing message
    processing_msg = await update.message.reply_text(
        f"⏳ `{url}` को monitor करने के लिए जोड़ रहा हूँ...",
        parse_mode="Markdown"
    )
    
    # Add to both services (parallel)
    await update.message.chat.send_action("typing")
    
    ur_result = add_uptimerobot(url)
    cj_result = add_cronjob(url)
    
    # ──── Build Response ────
    msg_parts = []
    msg_parts.append(f"📊 *Monitor Status — `{domain}`*")
    msg_parts.append(f"🔗 `{url}`\n")
    
    # UptimeRobot
    ur_emoji = "🟢" if ur_result["success"] else "🔴"
    msg_parts.append(f"{ur_emoji} *UptimeRobot:* {ur_result['message']}")
    
    # Cron-job
    cj_emoji = "🟢" if cj_result["success"] else "🔴"
    msg_parts.append(f"{cj_emoji} *Cron-job:* {cj_result['message']}")
    
    # Summary
    success_count = sum([ur_result["success"], cj_result["success"]])
    msg_parts.append(f"\n{'✅' if success_count == 2 else '⚠️'} *Result:* {success_count}/2 services configured")
    
    # Tips
    msg_parts.append("\n💡 *Tips:*")
    msg_parts.append("• UptimeRobot → 5 min interval (free plan)")
    msg_parts.append("• Cron-job → 1 min interval (free plan)")
    msg_parts.append("• Dashboards check karein for detailed stats")
    
    msg = "\n".join(msg_parts)
    
    # Add dashboard links
    keyboard = [
        [InlineKeyboardButton("📊 UptimeRobot Dashboard", url="https://uptimerobot.com/dashboard")],
        [InlineKeyboardButton("🔄 Cron-job Dashboard", url="https://cron-job.org/en/dashboard/")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await processing_msg.edit_text(msg, parse_mode="Markdown", reply_markup=reply_markup)
    
    # Log
    logger.info(f"Monitor added: {url} | UR: {ur_result['success']} | CJ: {cj_result['success']}")


# ──── Start Command ────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start"""
    
    # Check if all env vars are set
    config_status = []
    config_status.append("✅" if TELEGRAM_BOT_TOKEN else "❌" + " TELEGRAM_BOT_TOKEN")
    config_status.append("✅" if UPTIMEROBOT_API_KEY else "❌" + " UPTIMEROBOT_API_KEY")
    config_status.append("✅" if CRONJOB_API_KEY else "❌" + " CRONJOB_API_KEY")
    config_status.append("✅" if CRONJOB_API_USER else "❌" + " CRONJOB_API_USER")
    
    msg = (
        "🤖 *URL Monitor Bot*\n\n"
        "एक ही command से किसी भी URL को दो monitoring services "
        "(UptimeRobot + Cron-job.org) पर add करें।\n\n"
        "*/monitor <URL>* — URL को monitor करें\n\n"
        "*Examples:*\n"
        "`/monitor https://myapp.onrender.com`\n"
        "`/monitor google.com`\n\n"
        "*Config Status:*\n"
        + "\n".join(config_status) + "\n\n"
        "_Powered by HackerAI_"
    )
    
    await update.message.reply_text(msg, parse_mode="Markdown")


# ──── Unknown Commands ────

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unknown commands."""
    await update.message.reply_text(
        "❌ Unknown command\n\n"
        "*/start* — Help\n"
        "*/monitor <URL>* — URL monitor करें",
        parse_mode="Markdown"
    )


# ──── Main ────

def main():
    # Validate config
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set!")
        return
    
    if not UPTIMEROBOT_API_KEY:
        logger.warning("⚠️ UPTIMEROBOT_API_KEY not set — UptimeRobot won't work")
    
    if not CRONJOB_API_KEY or not CRONJOB_API_USER:
        logger.warning("⚠️ CRONJOB_API_KEY/USER not set — Cron-job won't work")
    
    # Start Flask health server in background
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"✅ Flask health server running on port {PORT}")
    
    # Build Telegram bot
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("monitor", monitor_url))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    
    logger.info("🤖 URL Monitor Bot started!")
    
    # Delete webhook if exists
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook")
        logger.info("✅ Webhook deleted (polling mode)")
    except:
        pass
    
    # Start polling
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
