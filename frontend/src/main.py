import os
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

app = FastAPI()

# Point to your templates folder
templates = Jinja2Templates(directory="src/templates")


@app.get("/")
async def serve_frontend(request: Request):
    # Grab the backend URL from the environment (defaulting to localhost for local testing)
    backend_url = os.getenv("BACKEND_URL", "http://localhost:8001")

    # We are rendering chat_page.html since that's what is in your folder
    return templates.TemplateResponse(
        request=request, name="chat_page.html", context={"backend_url": backend_url}
    )
