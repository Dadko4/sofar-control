# Agent notes for sofar-control

## Deploy
- Do NOT deploy unless the user explicitly asks for it in the current request.
- Pushing to `main` triggers `.github/workflows/deploy.yml` (tests, then deploy on the self-hosted runner `sofar-host`). Treat a push to `main` as a production deploy.
- Production runs on this host via `docker compose`. `scripts/deploy.sh` refuses to deploy while the scheduler is `starting`, `charging`, `transitioning` or `returning_to_self_use`.

## Tests
- Run the suite in Docker: `docker run --rm -u "$(id -u):$(id -g)" -v "$PWD":/app -w /app --entrypoint python sofar-control-sofar-opti -m unittest discover -p 'test_*.py'`
- Host Python has no pip or socketio; use Docker.

## Forecast model
- `SOFAR_FORECAST_MODEL_MODE`: `off` | `active` (default; planner uses the learned model, falls back to the formula if the model is missing) | `shadow` (learned predictions stored in each plan under `forecast_model`, planner uses the formula).
- The app retrains at startup and then every 7 days (`forecast_service.py`), saving `DATA_DIR/forecast_model.json`; status at `GET /api/forecast-model`.
- Offline training/evaluation: `python train_forecast.py --location Jacovce --cache /tmp/cache.json` (run in Docker with `--env-file .env`).
