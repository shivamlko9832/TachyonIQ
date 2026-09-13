# TachyonIQ Frontend

Next.js (App Router, TypeScript, Tailwind) client for the UADA API. Replaces
the inline HTML page previously served from `uada/api/routes/ui.py`.

## Run

```bash
cd frontend
npm install
cp .env.local.example .env.local   # set BACKEND_URL if the API isn't on localhost:8000
npm run dev                        # http://localhost:3001
```

Start the FastAPI backend separately (`uvicorn uada.api.app:app --reload`, default port 8000).
The browser only talks to this Next.js app — `app/api/backend/[...path]/route.ts` proxies every
request server-side to `BACKEND_URL`, so the FastAPI app needs no CORS configuration, and an
optional `BACKEND_API_KEY` never reaches the browser bundle.

## Layout

- `app/` — routes (`page.tsx` is the single chat screen) and the backend proxy route.
- `components/` — `ChatApp` is the top-level client component; everything else is presentational.
- `lib/types.ts` — TypeScript mirrors of the `UADAResponse` family in `uada/models/result.py`.
- `lib/api.ts` — typed fetch wrappers for `/connections`, `/query`, `/session`.

## Notes

- The right-panel "Data" tab renders the real result table from `UADAResponse.query_result`
  (columns + rows, capped at 50 displayed rows).
- `sql` and `query_result.executed_sql` are redacted server-side (`null`) unless the caller's
  role is `analyst` or `admin` — anonymous/local-dev requests are `viewer` by default, so the
  SQL tab will show "SQL is hidden for your role" once a query has run. Set `UADA_API_KEY` on
  the backend and `BACKEND_API_KEY` here to authenticate as `admin` and see SQL.
- Schema browsing uses the lightweight `GET /connections/{id}/schema` endpoint (table/column
  names and types only, no profiling) rather than the heavier `/refresh` endpoint, since the UI
  doesn't display profiling stats (null %, distinct count, min/max).
