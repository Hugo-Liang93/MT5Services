import uvicorn
from src.api import app  # noqa: F401


if __name__ == "__main__":
    uvicorn.run("src.api:app", host="0.0.0.0", port=8808, reload=False)
