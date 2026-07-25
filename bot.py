import os
import requests
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from flask import Flask

# ---------- Environment ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))

# ---------- Monitor API Keys ----------
UPTIMEROBOT_API_KEY = os.getenv("UPTIMEROBOT_API_KEY")
CRONJOB_API_KEY = os.getenv("CRONJOB_API_KEY")
CRONJOB_API_USER = os.getenv("CRONJOB_API_USER")

# ---------- Flask for health check ----------
app_flask = Flask(__name__)

@app_flask.route('/health')
def health():
    return "OK", 200

def run_flask():
    app_flask.run(host='0.0.0.0', port=PORT)

# ---------- UptimeRobot Helper (5 minutes) ----------
async def add_to_uptimerobot(url):
    api_url = "https://api.uptimerobot.com/v2/newMonitor"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "api_key": UPTIMEROBOT_API_KEY,
        "format": "json",
        "type": "1",
        "url": url,
        "friendly_name": url.replace("https://", "").replace("http://", "").split("/")[0],
        "interval": "300"  # ✅ 5 minutes (300 seconds)
    }
    try:
        response = requests.post(api_url, headers=headers, data=data)
        result = response.json()
        if result.get("stat") == "ok":
            return "✅ Added (5 min interval)"
        return f"❌ {result.get('error', {}).get('message', 'Unknown error')}"
    except Exception as e:
        return f"⚠️ Error: {str(e)[:50]}"

# ---------- Cron-job Helper (1 minute) ----------
async def add_to_cronjob(url):
    api_url = "https://cron-job.org/api/v1/jobs"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CRONJOB_API_KEY}"
    }
    payload = {
        "title": url.replace("https://", "").replace("http://", "").split("/")[0],
        "url": url,
        "schedule": {
            "type": "interval",
            "interval": 1,  # ✅ 1 minute
            "timezone": "UTC"
        },
        "enabled": True,
        "saveResponses": True,
        "emailAddress": CRONJOB_API_USER,
        "requestMethod": "GET"
    }
    try:
        response = requests.post(api_url, headers=headers, json=payload)
        if response.status_code != 200:
            return f"❌ HTTP {response.status_code}: {response.text[:100]}"
        result = response.json()
        if result.get("success"):
            return "✅ Added (1 min interval)"
        return f"❌ {result.get('message', 'Unknown error')}"
    except Exception as e:
        return f"⚠️ Error: {str(e)[:50]}"

# ---------- COMMAND: /monitor ----------
async def monitor_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ URL दें: `/monitor https://your-app.onrender.com`")
        return
    
    url = context.args[0].strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url

    await update.message.reply_text(f"⏳ `{url}` को मॉनिटर करने के लिए जोड़ रहा हूँ...")
    
    results = []
    
    if UPTIMEROBOT_API_KEY:
        results.append(f"🟢 UptimeRobot: {await add_to_uptimerobot(url)}")
    else:
        results.append("🟢 UptimeRobot: API Key missing")
    
    if CRONJOB_API_KEY and CRONJOB_API_USER:
        results.append(f"🔵 Cron-job: {await add_to_cronjob(url)}")
    else:
        results.append("🔵 Cron-job: API Key or User missing")
    
    await update.message.reply_text("📊 **Monitor Status:**\n" + "\n".join(results))

# ---------- COMMAND: /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **URL Monitor Bot**\n\n"
        "📊 `/monitor https://your-app.onrender.com` – URL को मॉनिटर करें\n"
        "🟢 UptimeRobot → हर 5 मिनट\n"
        "🔵 Cron-job → हर 1 मिनट\n"
        "ℹ️ `/start` – यह मैसेज"
    )

# ---------- Bot Run ----------
async def run_bot():
    app = Application.builder().token(TOKEN).build()
    
    # Webhook हटाओ
    await app.bot.delete_webhook(drop_pending_updates=True)
    
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("monitor", monitor_url))
    
    print("🤖 Bot चल रहा है...")
    await app.run_polling(drop_pending_updates=True)

# ---------- Main ----------
if __name__ == "__main__":
    import threading
    
    # Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Run bot
    asyncio.run(run_bot())
