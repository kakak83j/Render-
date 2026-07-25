import os
import zipfile
import shutil
import tempfile
import threading
import re
import requests
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from github import Github, GithubException
from flask import Flask

# ---------- Environment ----------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USERNAME")
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

# ---------- Temp ----------
TEMP_DIR = tempfile.mkdtemp(prefix="bot_zip_")
WAITING_FOR_REPO_NAME = 1

# ---------- GitHub Helper ----------
g = Github(GITHUB_TOKEN)
user_obj = g.get_user()

# ---------- Upload Folder ----------
async def upload_folder_to_github(repo, folder_path, branch="main"):
    uploaded_count = 0
    errors = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            file_path = os.path.join(root, file)
            relative_path = os.path.relpath(file_path, folder_path).replace("\\", "/")
            try:
                with open(file_path, 'rb') as f:
                    content = f.read()
                try:
                    repo.create_file(relative_path, f"Upload {relative_path}", content, branch=branch)
                except GithubException as e:
                    if e.status == 422:
                        file_content = repo.get_contents(relative_path, ref=branch)
                        repo.update_file(relative_path, f"Update {relative_path}", content, file_content.sha, branch=branch)
                    else:
                        raise
                uploaded_count += 1
            except Exception as e:
                errors.append(f"{relative_path}: {str(e)}")
    if errors:
        return f"Uploaded {uploaded_count}, errors: " + ", ".join(errors[:3])
    return f"Upload Complete – {uploaded_count} files."

# ---------- Download Single Repo ----------
def download_repo_as_zip(repo_name, download_path):
    try:
        repo = user_obj.get_repo(repo_name)
        zip_url = repo.html_url + "/archive/main.zip"
        response = requests.get(zip_url)
        if response.status_code != 200:
            zip_url = repo.html_url + "/archive/master.zip"
            response = requests.get(zip_url)
            if response.status_code != 200:
                return None, "Cannot download repo"
        with open(download_path, 'wb') as f:
            f.write(response.content)
        return download_path, f"Downloaded {repo_name}"
    except Exception as e:
        return None, str(e)

# ---------- UptimeRobot Helper (FIXED) ----------
async def add_to_uptimerobot(url):
    api_url = "https://api.uptimerobot.com/v2/newMonitor"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "api_key": UPTIMEROBOT_API_KEY,
        "format": "json",
        "url": url,
        "friendly_name": url.replace("https://", "").replace("http://", "").split("/")[0]
        # ✅ Free plan: "type" aur "interval" hata diye - default use hoga
    }
    try:
        response = requests.post(api_url, headers=headers, data=data)
        result = response.json()
        if result.get("stat") == "ok":
            return "✅ Added"
        return f"❌ {result.get('error', {}).get('message', 'Unknown error')}"
    except Exception as e:
        return f"⚠️ Error: {str(e)[:50]}"

# ---------- Cron-job Helper (FIXED) ----------
async def add_to_cronjob(url):
    api_url = "https://cron-job.org/api/jobs"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {CRONJOB_API_KEY}"
    }
    payload = {
        "title": url.replace("https://", "").replace("http://", "").split("/")[0],
        "url": url,
        "schedule": {
            "type": "interval",
            "interval": 5,  # ✅ 5 minutes (integer, not cron string)
            "timezone": "UTC"
        },
        "enabled": True,
        "saveResponses": True,
        "emailAddress": CRONJOB_API_USER,
        "requestMethod": "GET"
    }
    try:
        response = requests.post(api_url, headers=headers, json=payload)
        # ✅ Debug: Check HTTP status
        if response.status_code != 200:
            return f"❌ HTTP {response.status_code}: {response.text[:100]}"
        result = response.json()
        if result.get("success"):
            return "✅ Added"
        return f"❌ {result.get('message', 'Unknown error')}"
    except requests.exceptions.JSONDecodeError:
        return f"⚠️ Invalid API response (check API Key & User)"
    except Exception as e:
        return f"⚠️ Error: {str(e)[:50]}"

# ---------- COMMAND 1: /monitor ----------
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

# ---------- COMMAND 2: /download_all_separate ----------
async def download_all_separate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📦 सभी रिपो को अलग-अलग ZIP में डाउनलोड किया जा रहा है...")
    repos = list(user_obj.get_repos())
    if not repos:
        await update.message.reply_text("❌ कोई रिपो नहीं मिली।")
        return

    all_repos_folder = os.path.join(TEMP_DIR, "all_repos_" + str(update.message.from_user.id))
    os.makedirs(all_repos_folder, exist_ok=True)
    downloaded_count = 0
    failed_repos = []
    
    for repo in repos:
        repo_name = repo.name
        zip_path = os.path.join(all_repos_folder, f"{repo_name}.zip")
        status, msg = download_repo_as_zip(repo_name, zip_path)
        if status:
            downloaded_count += 1
            try:
                await update.message.reply_document(
                    document=open(zip_path, 'rb'),
                    caption=f"📦 `{repo_name}` – Download"
                )
                os.remove(zip_path)
            except Exception as e:
                failed_repos.append(f"{repo_name}: send error")
        else:
            failed_repos.append(f"{repo_name}: {msg}")
        if downloaded_count % 5 == 0:
            await update.message.reply_text(f"⏳ {downloaded_count}/{len(repos)} डाउनलोड हो चुके हैं...")

    shutil.rmtree(all_repos_folder, ignore_errors=True)
    summary = f"✅ {downloaded_count} रिपो डाउनलोड हुईं।"
    if failed_repos:
        summary += f"\n⚠️ फेल: {', '.join(failed_repos[:5])}" + ("..." if len(failed_repos) > 5 else "")
    await update.message.reply_text(summary)

# ---------- COMMAND 3: /privatize ----------
async def privatize_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ नाम दें: `/privatize repo_name`")
        return
    repo_name = context.args[0]
    try:
        repo = user_obj.get_repo(repo_name)
        if not repo.private:
            repo.edit(private=True)
            await update.message.reply_text(f"✅ `{repo_name}` अब **प्राइवेट** है।")
        else:
            await update.message.reply_text(f"ℹ️ `{repo_name}` पहले से ही प्राइवेट है।")
    except GithubException as e:
        await update.message.reply_text(f"❌ Error: {e.data.get('message', str(e))}")

# ---------- COMMAND 4: /publicize ----------
async def publicize_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ नाम दें: `/publicize repo_name`")
        return
    repo_name = context.args[0]
    try:
        repo = user_obj.get_repo(repo_name)
        if repo.private:
            repo.edit(private=False)
            await update.message.reply_text(f"✅ `{repo_name}` अब **पब्लिक** है।")
        else:
            await update.message.reply_text(f"ℹ️ `{repo_name}` पहले से ही पब्लिक है।")
    except GithubException as e:
        await update.message.reply_text(f"❌ Error: {e.data.get('message', str(e))}")

# ---------- COMMAND 5: /delete_repo ----------
async def delete_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ नाम दें: `/delete_repo repo_name`")
        return
    repo_name = context.args[0]
    await update.message.reply_text(f"⚠️ `{repo_name}` को डिलीट करना है? `/confirm_delete {repo_name}` टाइप करो।")
    context.user_data['pending_delete'] = repo_name

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ नाम दें: `/confirm_delete repo_name`")
        return
    repo_name = context.args[0]
    pending = context.user_data.get('pending_delete')
    if pending != repo_name:
        await update.message.reply_text("❌ नाम मेल नहीं खाता।")
        return
    try:
        repo = user_obj.get_repo(repo_name)
        repo.delete()
        await update.message.reply_text(f"🗑️ `{repo_name}` **डिलीट** कर दिया गया।")
        context.user_data.pop('pending_delete', None)
    except GithubException as e:
        await update.message.reply_text(f"❌ Error: {e.data.get('message', str(e))}")

# ---------- COMMAND 6: /start ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **मैं कर सकता हूँ:**\n\n"
        "📦 **ZIP / GitHub URL** → नई रिपो\n"
        "📥 `/download_all_separate` – सभी रिपो की अलग-अलग ZIP\n"
        "🔒 `/privatize repo` – पब्लिक→प्राइवेट\n"
        "🔓 `/publicize repo` – प्राइवेट→पब्लिक\n"
        "🗑️ `/delete_repo repo` – डिलीट (फिर confirm)\n"
        "📊 `/monitor https://url.com` – UptimeRobot + Cron-job पर मॉनिटर"
    )

# ---------- Original: ZIP / GitHub URL Handler ----------
async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text
    document = update.message.document

    if text and "github.com" in text.lower():
        context.user_data['pending_github_url'] = text.strip()
        await update.message.reply_text("📝 इस रिपो के लिए नई रिपॉजिटरी का नाम बताओ:")
        return WAITING_FOR_REPO_NAME
    elif document and document.file_name.endswith('.zip'):
        context.user_data['pending_zip'] = {
            'file_id': document.file_id,
            'file_name': document.file_name,
            'user_id': user.id,
        }
        await update.message.reply_text("📝 इस ZIP के लिए रिपॉजिटरी का नाम बताओ:")
        return WAITING_FOR_REPO_NAME
    else:
        await update.message.reply_text("❌ सिर्फ ZIP या GitHub URL भेजो!")
        return ConversationHandler.END

async def handle_repo_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_name = update.message.text.strip()
    if not repo_name:
        await update.message.reply_text("❌ नाम खाली नहीं हो सकता।")
        return WAITING_FOR_REPO_NAME

    await update.message.reply_text("⏳ प्रोसेस हो रहा है...")
    github_url = context.user_data.get('pending_github_url')
    zip_pending = context.user_data.get('pending_zip')
    extract_path = None
    zip_path = None

    try:
        if github_url:
            zip_path = os.path.join(TEMP_DIR, f"repo_{repo_name}.zip")
            status, msg = download_github_repo_as_zip(github_url, zip_path)
            if not status:
                await update.message.reply_text(f"❌ {msg}")
                context.user_data.pop('pending_github_url', None)
                return ConversationHandler.END
            await update.message.reply_text(f"📥 {msg} – अपलोड कर रहा हूँ...")
            extract_path = os.path.join(TEMP_DIR, "extracted_" + str(update.message.from_user.id))
            os.makedirs(extract_path, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
            await update.message.reply_document(
                document=open(zip_path, 'rb'),
                caption="📦 डाउनलोड किया गया ZIP"
            )
        elif zip_pending:
            file_id = zip_pending['file_id']
            file = await context.bot.get_file(file_id)
            zip_path = os.path.join(TEMP_DIR, zip_pending['file_name'])
            await file.download_to_drive(zip_path)
            extract_path = os.path.join(TEMP_DIR, "extracted_" + str(zip_pending['user_id']))
            os.makedirs(extract_path, exist_ok=True)
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
        else:
            await update.message.reply_text("❌ कोई डेटा नहीं मिला।")
            return ConversationHandler.END

        items = os.listdir(extract_path)
        if len(items) == 1 and os.path.isdir(os.path.join(extract_path, items[0])):
            top_folder = items[0]
            top_path = os.path.join(extract_path, top_folder)
            for item in os.listdir(top_path):
                shutil.move(os.path.join(top_path, item), extract_path)
            os.rmdir(top_path)

        base_name = repo_name
        attempt = 0
        while True:
            try:
                user_obj.get_repo(base_name)
                attempt += 1
                base_name = f"{repo_name}-{attempt}"
            except GithubException as e:
                if e.status == 404:
                    break
                else:
                    raise

        await update.message.reply_text(f"🏗️ '{base_name}' बन रहा है...")
        repo = user_obj.create_repo(base_name, private=False, auto_init=False)
        upload_status = await upload_folder_to_github(repo, extract_path)
        await update.message.reply_text(f"✅ Repo: {repo.html_url}\n📤 {upload_status}")

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        if extract_path and os.path.exists(extract_path):
            shutil.rmtree(extract_path, ignore_errors=True)
        if zip_path and os.path.exists(zip_path):
            os.remove(zip_path)
        context.user_data.pop('pending_zip', None)
        context.user_data.pop('pending_github_url', None)

    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ कैंसल कर दिया गया।")
    context.user_data.clear()
    return ConversationHandler.END

# ---------- Download Helper (for URL) ----------
def download_github_repo_as_zip(repo_url, download_path):
    match = re.search(r"github\.com/([^/]+)/([^/]+)", repo_url)
    if not match:
        return None, "Invalid GitHub URL"
    username, repo = match.groups()
    zip_url = f"https://github.com/{username}/{repo}/archive/main.zip"
    response = requests.get(zip_url)
    if response.status_code != 200:
        zip_url = f"https://github.com/{username}/{repo}/archive/master.zip"
        response = requests.get(zip_url)
        if response.status_code != 200:
            return None, "Repo not accessible"
    with open(download_path, 'wb') as f:
        f.write(response.content)
    return download_path, f"Downloaded {repo}"

# ---------- Bot Run ----------
def run_bot():
    app = Application.builder().token(TOKEN).build()
    
    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("monitor", monitor_url))
    app.add_handler(CommandHandler("download_all_separate", download_all_separate))
    app.add_handler(CommandHandler("privatize", privatize_repo))
    app.add_handler(CommandHandler("publicize", publicize_repo))
    app.add_handler(CommandHandler("delete_repo", delete_repo))
    app.add_handler(CommandHandler("confirm_delete", confirm_delete))
    
    # Conversation for ZIP/URL
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.ALL, handle_input),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)
        ],
        states={
            WAITING_FOR_REPO_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_repo_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)
    
    app.bot.delete_webhook(drop_pending_updates=True)
    print("🤖 Bot चल रहा है...")
    app.run_polling(drop_pending_updates=True)

# ---------- Main ----------
if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    run_bot()
