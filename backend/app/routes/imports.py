from fastapi import APIRouter, HTTPException
from app.config import settings
from app.db import db
from app.services.import_jobs import get_job, start_job

router=APIRouter(prefix='/api/imports',tags=['imports'])

@router.post('/demo',status_code=202)
def demo():
    try: return {'job_id':start_job('demo')}
    except RuntimeError: raise HTTPException(409,detail={'error':{'code':'IMPORT_BUSY','message':'Ein Import läuft bereits'}})

@router.post('/local',status_code=202)
def local():
    if not settings.local_import_dir.is_dir(): raise HTTPException(503,detail={'error':{'code':'LOCAL_IMPORT_NOT_CONFIGURED','message':'Lokaler Importordner ist nicht eingerichtet'}})
    try: return {'job_id':start_job('local')}
    except RuntimeError: raise HTTPException(409,detail={'error':{'code':'IMPORT_BUSY','message':'Ein Import läuft bereits'}})

@router.get('/{job_id}')
def job(job_id:str):
    result=get_job(job_id)
    if result is None: raise HTTPException(404,detail={'error':{'code':'NOT_FOUND','message':'Import nicht gefunden'}})
    import json
    result['errors']=json.loads(result.pop('errors_json'))
    return result
