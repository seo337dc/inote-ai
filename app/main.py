import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from app.routers import book_info, chat, conversations, sessions, summarize

app = FastAPI(title="inote-ai")

allowed_origins = os.environ.get("ALLOWED_ORIGINS", "http://localhost:3011").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(conversations.router)
app.include_router(sessions.router)
app.include_router(summarize.router)
app.include_router(book_info.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
