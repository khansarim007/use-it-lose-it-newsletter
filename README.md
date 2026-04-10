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

## Integrations

Set these environment variables before connecting platforms:

- `SECRET_KEY`
- `MAILCHIMP_CLIENT_ID`
- `MAILCHIMP_CLIENT_SECRET`
- `MAILCHIMP_LIST_ID`
- `MAILCHIMP_REDIRECT_URI` (optional, defaults to the app route)
- `CONVERTKIT_CLIENT_ID`
- `CONVERTKIT_CLIENT_SECRET`
- `CONVERTKIT_REDIRECT_URI` (optional, defaults to the app route)
- `CONVERTKIT_SCOPE` (optional, default: `forms:read subscribers:read`)
- `BEEHIIV_PUBLICATION_ID`
- `BEEHIIV_CLIENT_ID`
- `BEEHIIV_CLIENT_SECRET`
- `BEEHIIV_REDIRECT_URI` (optional, defaults to the app route)
- `BEEHIIV_SCOPE` (optional, default: `publications:read subscriptions:read`)

Optional endpoint overrides (only if provider docs change):

- `CONVERTKIT_AUTHORIZE_URL`
- `CONVERTKIT_TOKEN_URL`
- `BEEHIIV_AUTHORIZE_URL`
- `BEEHIIV_TOKEN_URL`

ConvertKit and Beehiiv now use OAuth in the app, just like Mailchimp.
