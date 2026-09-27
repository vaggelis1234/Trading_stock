from fastapi import FastAPI
from fastapi.responses import JSONResponse
import os
import glob
import pandas as pd

app = FastAPI()
ARTIFACT_DIR = os.getenv("ARTIFACT_DIR", "artifacts")


def latest_scores_csv():
    files = glob.glob(os.path.join(ARTIFACT_DIR, "scores_*.csv"))
    if not files:
        return None
    files.sort()
    return files[-1]


@app.get("/scores")
def get_scores():
    path = latest_scores_csv()
    if not path:
        return JSONResponse(content={"error": "no scores yet"}, status_code=404)
    df = pd.read_csv(path)
    return JSONResponse(content=df.to_dict(orient="records"))

