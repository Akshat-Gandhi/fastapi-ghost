from fastapi import HTTPException


async def get_report(rid: int):
    raise HTTPException(status_code=404, detail="no such report")
