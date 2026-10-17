import os
from typing import Annotated, Optional

from fastapi import Depends, Header, HTTPException


def get_current_user(authorization: Optional[str] = Header(default=None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"name": "u", "admin": authorization == "admin"}


def require_admin(user=Depends(get_current_user)):
    if not user["admin"]:
        raise HTTPException(status_code=403, detail="Admins only")
    return user


def check_maintenance():
    if os.environ.get("MAINTENANCE"):
        raise HTTPException(status_code=503, detail="maintenance")


CurrentUser = Annotated[dict, Depends(get_current_user)]
