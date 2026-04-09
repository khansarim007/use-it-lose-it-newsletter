# CullList

Simple MVP SaaS tool for newsletter engagement management.

## Run

1. Install dependencies:
   pip install -r requirements.txt
2. Start app:
   python app.py
3. Open browser at:
   http://127.0.0.1:5000

## Features

- Signup/Login with session auth
- CSV subscriber uploads
- Open and click tracking endpoints
- Tracked email generation (link rewriting + pixel injection)
- Engagement rule engine (Clean List)
- Dashboard metrics
- Subscriber list filters
- Simulated warning emails for inactive subscribers

## 2-Minute Demo Workflow

1. Seed demo data:
   python seed_demo.py
2. Start app:
   python app.py
3. Login with:
   demo@useitloseit.local / demo123
4. Dashboard already shows mixed statuses and engagement metrics.
5. Go to Upload CSV and upload sample_subscribers.csv.
6. Go to Compose and generate a tracked email for any subscriber.

## Deployment

Ready to deploy? See [DEPLOYMENT.md](DEPLOYMENT.md) for:
- Step-by-step GitHub setup
- Render deployment (recommended)
- Railway deployment (alternative)
- Environment variable configuration
- Production troubleshooting
