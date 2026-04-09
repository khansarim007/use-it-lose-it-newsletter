# Deployment Guide

Your Flask app is now ready for production deployment. Follow the steps below for your chosen platform.

---

## Prerequisites

- GitHub account (required for both Render and Railway)
- Render or Railway account

---

## Step 1: Push to GitHub

### Create a new GitHub repository

1. Go to https://github.com/new
2. Repository name: `use-it-lose-it-newsletter`
3. Description: "Newsletter engagement tracking SaaS"
4. Choose **Private** or **Public**
5. Click **Create repository**

### Push your code

Run these commands in your project directory:

```bash
git remote add origin https://github.com/YOUR_USERNAME/use-it-lose-it-newsletter.git
git branch -M main
git push -u origin main
```

Replace `YOUR_USERNAME` with your actual GitHub username.

---

## Option A: Deploy on Render

Render is simple, fast, and generous with free tier.

### 1. Connect GitHub to Render

1. Go to https://render.com (sign up with GitHub)
2. Click **New +** → **Web Service**
3. Click **Connect a repository**
4. Search for `use-it-lose-it-newsletter` and connect it

### 2. Configure the Service

- **Name**: `use-it-lose-it-newsletter` (or your preferred name)
- **Environment**: Python 3
- **Region**: Choose closest to you (e.g., us-east)
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn app:app`

### 3. Environment Variables

Add these in the **Environment** section:

```
FLASK_ENV=production
SECRET_KEY=<generate-a-strong-random-key>
```

To generate a secret key, run in terminal:
```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Copy the output and paste as SECRET_KEY value.

### 4. Deploy

- Click **Create Web Service**
- Render will auto-build and deploy
- Wait 2-3 minutes for deployment to complete
- Your URL: `https://use-it-lose-it-newsletter.onrender.com`

### 5. Update Flask Code (One-time)

Modify the line in `app.py`:

```python
app.config["SECRET_KEY"] = "dev-secret-change-in-production"
```

To use environment variables:

```python
import os
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-fallback-key")
```

Commit and push:
```bash
git add app.py
git commit -m "Use environment variable for SECRET_KEY"
git push
```

---

## Option B: Deploy on Railway

Railway is another excellent option with similar ease and free credits.

### 1. Connect GitHub to Railway

1. Go to https://railway.app (sign up with GitHub)
2. Click **+ New Project**
3. Select **Deploy from GitHub repo**
4. Search and select `use-it-lose-it-newsletter`

### 2. Add Environment Variables

In the **Variables** tab, add:

```
FLASK_ENV=production
SECRET_KEY=<your-generated-secret-key>
```

### 3. Configure Start Command

Railway auto-detects Python + Procfile, so it should work out of the box.

If needed, set in **Settings**:
- **Procfile** exists (it does) → Railway will use it automatically

### 4. Deploy

Click **Deploy** button. Railway will:
- Install dependencies from requirements.txt
- Run the Procfile command: `gunicorn app:app`
- Assign a public URL and deploy

Your URL will be shown on the deployment page.

### 5. Update Flask Code (Same as Render)

Same as Option A — update `app.py` to use environment variable for SECRET_KEY.

---

## Database & SQLite in Production

SQLite works perfectly for production apps with moderate traffic.

**How it works:**
- Database file (`database.db`) is created on first run
- Each Render/Railway instance has its own file system
- Manual backups: Download `database.db` from your instance if needed

**To backup manually (optional):**

1. Access your deployment logs/shell
2. Download `database.db` before re-deploying

**Note:** If your app is scaled to multiple dynos/instances, each will have its own database copy. For a production app serving many users, consider PostgreSQL. For this MVP, SQLite is fine.

---

## Verify Production Deployment

Once deployed:

1. Open your public URL
2. Sign up with a test email
3. Log in
4. Upload CSV (use `sample_subscribers.csv`)
5. Compose an email
6. Test open/click tracking

---

## Post-Deployment

### Enable Auto-Deploys (Rendering)

- **Render**: Auto-deploys on every `git push` to main
- **Railway**: Similar — auto-deploys on push

### Monitor Logs

- **Render**: Dashboard → **Logs** tab
- **Railway**: Dashboard → Select service → **Logs** tab

### Custom Domain (Optional)

- **Render**: Settings → Custom Domain
- **Railway**: Settings → Domain

---

## Troubleshooting

### Build fails
- Check that `requirements.txt` includes all dependencies
- Verify `Procfile` syntax: `web: gunicorn app:app`

### App crashes on startup
- Check deployment logs for error messages
- Ensure SECRET_KEY environment variable is set
- Verify PORT environment variable is respected in `app.py` ✓ (already done)

### Database issues
- SQLite stores data in `database.db` in project root
- First request auto-initializes schema
- If tables missing, check logs for init_db() errors

---

## Final Checklist

- [x] Project structure correct (app.py, templates/, static/)
- [x] requirements.txt updated with gunicorn
- [x] app.py modified for production (no debug=True, PORT env var)
- [x] Procfile created
- [x] Git initialized and committed
- [x] .gitignore created
- [x] SQLite ready for production

You're ready to deploy! Choose Render or Railway and follow the steps above.
