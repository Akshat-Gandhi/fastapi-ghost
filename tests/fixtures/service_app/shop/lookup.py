import os

from fastapi import HTTPException

if os.environ.get("LEGACY_BACKEND"):
    def find(key):
        raise HTTPException(status_code=502, detail="legacy backend")
else:
    def find(key):
        raise HTTPException(status_code=507, detail="backend full")
