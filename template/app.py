from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/")
def read_root(request: Request):
    modules = request.app.state.mounted_modules
    if modules:
        return RedirectResponse(url=f"/{modules[0]['id']}")
    return {"mounted_modules": modules}
