from fastapi import FastAPI
from core.domain.objects import init_db
from core.api.objects import router as object_router

app = FastAPI(title="Atlas Core")

@app.on_event("startup")
def startup():
    init_db()

app.include_router(object_router)

@app.get("/health")
def health():
    return {"status": "ok"}
