from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.config import settings
from app.db import db, migrate
from app.services.import_jobs import recover_jobs
from app.routes import imports, media, observations, google, uploads, analysis, zukunftsgraph

MUTATING = {'POST','PATCH','PUT','DELETE'}

class BrowserMutationGuard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in MUTATING and request.url.path.startswith('/api/'):
            if request.url.path.startswith('/api/google/oauth/callback'):
                return await call_next(request)
            origin=request.headers.get('origin')
            if origin not in settings.allowed_origins:
                return JSONResponse(status_code=403,content={'error':{'code':'ORIGIN_DENIED','message':'Ursprungsdomain nicht erlaubt'}})
            content_type=request.headers.get('content-type','').split(';',1)[0].strip().lower()
            is_upload=request.method=='POST' and request.url.path=='/api/uploads'
            if is_upload and content_type!='multipart/form-data':
                return JSONResponse(status_code=415,content={'error':{'code':'MULTIPART_REQUIRED','message':'Multipart-Anfrage erforderlich'}})
            if not is_upload and not (request.method=='DELETE' and not content_type) and content_type!='application/json':
                return JSONResponse(status_code=415,content={'error':{'code':'JSON_REQUIRED','message':'JSON-Anfrage erforderlich'}})
        return await call_next(request)

@asynccontextmanager
async def lifespan(app:FastAPI):
    migrate()
    recover_jobs()
    from app.services.processing_jobs import runner
    await runner.start()
    try:
        yield
    finally:
        await runner.stop()

app=FastAPI(title='WoWannWas API',version='0.1.0',lifespan=lifespan)
app.add_middleware(BrowserMutationGuard)
app.add_middleware(CORSMiddleware,allow_origins=list(settings.allowed_origins),allow_credentials=True,allow_methods=['GET','POST','PATCH','DELETE','OPTIONS'],allow_headers=['Content-Type'])
app.include_router(observations.router)
app.include_router(media.router)
app.include_router(imports.router)
app.include_router(google.router)
app.include_router(uploads.router)
app.include_router(analysis.router)
app.include_router(zukunftsgraph.router)

@app.get('/api/health')
def health():
    with db() as conn: conn.execute('SELECT 1').fetchone()
    return {'status':'ok'}

@app.get('/api/config')
def public_config():
    provider = settings.gemini_provider if settings.analysis_enabled else None
    return {'google_photos_enabled':settings.google_enabled,'local_import_enabled':settings.local_import_dir.is_dir(),'demo_enabled':True,'analysis_available':settings.analysis_enabled,'analysis_provider':provider}
