"""HTTP entry points for the TF Tool generate pipeline.

Sibling to `cli.py` (CLI front) and `app.py` (Streamlit front). All three
drive `core.service.generate()` — change the generator once, every entry
point picks up the fix.

`index.py` is the Vercel Python serverless entrypoint that registers all
routes (`/api/health`, `/api/generate`, `/api/push`). Slack and JIRA
handlers register their own routes onto the same FastAPI app from
`api/slack.py` and `api/jira.py` so a single deploy serves every surface.
"""
