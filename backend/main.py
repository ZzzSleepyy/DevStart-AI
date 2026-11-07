from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings
from routes import analysis_routes, chat_routes, github_routes

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=(
        "AI-Powered Developer Assistant backend for the DevStart AI Chrome Extension. "
        "Provides GitHub repository understanding, Semgrep static analysis, "
        "Gemini AI code suggestions, and user-approved Pull Request creation."
    ),
)

# Keep your CORS setup for the frontend connection
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Attach the routers to your app
app.include_router(github_routes.router)
app.include_router(analysis_routes.router)
app.include_router(chat_routes.router)


@app.get("/")
def health_check():
    """Root health check endpoint."""
    return {
        "status": "FastAPI backend is live",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


@app.get("/health")
def extension_health_check():
    """Health check endpoint polled by the DevStart AI Chrome Extension."""
    return {
        "status": "ok",
        "gemini_configured": bool(
            settings.GEMINI_API_KEY and settings.GEMINI_API_KEY != "your_gemini_api_key"
        ),
        "github_token_configured": bool(
            settings.GITHUB_TOKEN and settings.GITHUB_TOKEN != "your_github_token"
        ),
    }
