from __future__ import annotations

import hashlib
import secrets
import uuid
import asyncio
import json
from pathlib import Path
import httpx
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from app.config import settings
from app.db import db,now_ms
from app.services.google_oauth import session_owner_hash
from app.services.photo_ingest import ingest_image
from app.services.google_oauth import authenticated,finish_oauth,start_oauth,access_token
from app.services.google_picker import PickerClient,GoogleAPIError

router=APIRouter(prefix='/api/google',tags=['google'])
COOKIE='wowannwas_session'

def _session(request:Request,create=False):
    value=request.cookies.get(COOKIE)
    if not value and create: value=secrets.token_urlsafe(32)
    return value

def _owner(value): return hashlib.sha256(value.encode()).hexdigest()
def _fail(code,status=503): raise HTTPException(status_code=status,detail={'error':{'code':code,'message':code.replace('_',' ').title()}})
def _duration_ms(text,default):
    if not text:return default
    try:
        from datetime import timedelta
        from isodate import parse_duration
        return int(parse_duration(text).total_seconds()*1000)
    except Exception:
        try:return int(float(text[:-1])*({'s':1000,'m':60000,'h':3600000}[text[-1]]))
        except Exception:return default

@router.get('/auth/status')
def auth_status(request:Request):
    s=_session(request)
    return {'enabled':settings.google_enabled,'authenticated':bool(s and authenticated(s))}

@router.get('/oauth/start')
def oauth_start(request:Request):
    s=_session(request,True)
    try:url=start_oauth(s)
    except RuntimeError:_fail('GOOGLE_NOT_CONFIGURED')
    response=RedirectResponse(url,status_code=302)
    response.set_cookie(COOKIE,s,httponly=True,samesite='lax',secure=settings.google_redirect_uri.startswith('https://'),path='/')
    return response

@router.get('/oauth/callback')
async def oauth_callback(request:Request,code:str|None=None,state:str|None=None,error:str|None=None):
    s=_session(request)
    if not s: _fail('OAUTH_STATE_INVALID',400)
    if error: return RedirectResponse(settings.frontend_origin+'/?google=cancelled',status_code=303)
    if not code or not state: _fail('OAUTH_STATE_INVALID',400)
    try: await finish_oauth(s,state,code)
    except ValueError as exc: _fail(str(exc),400)
    return RedirectResponse(settings.frontend_origin+'/?google=connected',status_code=303)

@router.post('/picker/sessions',status_code=201)
async def create_picker(request:Request,response:Response):
    if not settings.google_enabled:_fail('GOOGLE_NOT_CONFIGURED')
    s=_session(request)
    if not s or not authenticated(s):_fail('GOOGLE_AUTH_REQUIRED',401)
    try:
        remote=await PickerClient(access_token(s)).create_session()
        now=now_ms(); sid=str(uuid.uuid4())
        poll=_duration_ms(remote.get('pollingConfig',{}).get('pollInterval'),5000)
        timeout=_duration_ms(remote.get('pollingConfig',{}).get('timeoutIn'),600000)
        expires=remote.get('expireTime')
        expiry=int(datetime.fromisoformat(expires.replace('Z','+00:00')).timestamp()*1000) if expires else now+86400000
        with db() as conn:
            conn.execute("INSERT INTO picker_sessions(id,google_session_id,owner_session_hash,picker_uri,status,poll_interval_ms,polling_deadline_ms,expires_at_ms,next_poll_at_ms,created_at_ms,updated_at_ms) VALUES(?,?,?,?,'waiting',?,?,?,?,?,?)",(sid,remote['id'],_owner(s),remote['pickerUri'],poll,now+timeout,expiry,now+poll,now,now))
        return {'id':sid,'status':'waiting','picker_url':remote['pickerUri'],'poll_interval_ms':poll,'polling_deadline':datetime.fromtimestamp((now+timeout)/1000).isoformat()+'Z','expires_at':datetime.fromtimestamp(expiry/1000).isoformat()+'Z'}
    except GoogleAPIError as exc:_fail(exc.code,401 if exc.code=='GOOGLE_AUTH_REQUIRED' else 502)

@router.get('/picker/sessions/{session_id}')
async def get_picker(session_id:str,request:Request):
    s=_session(request)
    if not s or not authenticated(s):_fail('GOOGLE_AUTH_REQUIRED',401)
    with db() as conn: row=conn.execute('SELECT * FROM picker_sessions WHERE id=? AND owner_session_hash=?',(session_id,_owner(s))).fetchone()
    if not row: raise HTTPException(404,detail={'error':{'code':'NOT_FOUND','message':'Picker-Sitzung nicht gefunden'}})
    now=now_ms(); status=row['status']
    if status in ('waiting','ready') and now>=row['expires_at_ms']:
        status='expired'
        with db() as conn:conn.execute("UPDATE picker_sessions SET status='expired',updated_at_ms=? WHERE id=?",(now,session_id))
    elif status=='waiting' and row['next_poll_at_ms'] is not None and now>=row['next_poll_at_ms'] and authenticated(s):
        try:
            remote=await PickerClient(access_token(s)).get_session(row['google_session_id'])
            status='ready' if remote.get('mediaItemsSet') else 'waiting'
            with db() as conn:conn.execute('UPDATE picker_sessions SET status=?,next_poll_at_ms=?,updated_at_ms=? WHERE id=?',(status,now+row['poll_interval_ms'],now,session_id))
        except GoogleAPIError as exc:
            if exc.code=='GOOGLE_AUTH_REQUIRED':_fail(exc.code,401)
            _fail(exc.code,502)
    return {'id':session_id,'status':status,'poll_interval_ms':row['poll_interval_ms'],'polling_deadline':datetime.fromtimestamp(row['polling_deadline_ms']/1000).isoformat()+'Z','expires_at':datetime.fromtimestamp(row['expires_at_ms']/1000).isoformat()+'Z','polling_timed_out':now>=row['polling_deadline_ms'] and status=='waiting'}

@router.post('/picker/sessions/{session_id}/import',status_code=202)
async def import_picker(session_id:str,request:Request):
    s=_session(request)
    if not s or not authenticated(s):_fail('GOOGLE_AUTH_REQUIRED',401)
    owner=_owner(s)
    with db() as conn:
        row=conn.execute('SELECT * FROM picker_sessions WHERE id=? AND owner_session_hash=?',(session_id,owner)).fetchone()
        if not row: raise HTTPException(404,detail={'error':{'code':'NOT_FOUND','message':'Picker-Sitzung nicht gefunden'}})
        existing=conn.execute('SELECT id FROM import_jobs WHERE picker_session_id=?',(session_id,)).fetchone()
        if existing:return {'job_id':existing['id']}
        if row['status']!='ready':_fail('PICKER_NOT_READY',409)
        job_id=str(uuid.uuid4()); now=now_ms()
        conn.execute("INSERT INTO import_jobs(id,source,picker_session_id,status,created_at_ms) VALUES(?, 'google_photos',?,'queued',?)",(job_id,session_id,now))
        conn.execute("UPDATE picker_sessions SET status='importing',updated_at_ms=? WHERE id=?",(now,session_id))
    asyncio.create_task(_run_google_import(job_id,session_id,row['google_session_id'],s))
    return {'job_id':job_id}

async def _run_google_import(job_id:str,local_id:str,google_id:str,session:str):
    counts={'imported':0,'skipped':0,'failed':0}; errors=[]; fatal=False
    try:
        with db() as conn:conn.execute("UPDATE import_jobs SET status='running' WHERE id=?",(job_id,))
        client=PickerClient(access_token(session))
        async for item in client.list_media(google_id):
            try:
                file=item.get('mediaFile',{})
                mime=file.get('mimeType','')
                if not mime.startswith('image/'):
                    counts['skipped']+=1
                else:
                    url=file.get('baseUrl','')
                    if not url.startswith('https://'):
                        raise ValueError('Invalid Google media URL')
                    # Download authenticated media without following redirects or exposing the URL.
                    async with httpx.AsyncClient(timeout=45,follow_redirects=False) as download:
                        async with download.stream('GET',url+'=d',headers={'Authorization':f'Bearer {access_token(session)}'}) as response:
                            if response.status_code!=200:raise ValueError('GOOGLE_DOWNLOAD_FAILED')
                            parts=[]; size=0
                            async for chunk in response.aiter_bytes():
                                size+=len(chunk)
                                if size>25*1024*1024:raise ValueError('IMAGE_TOO_LARGE')
                                parts.append(chunk)
                            payload=b''.join(parts)
                    created=item.get('createTime')
                    created_ms=int(datetime.fromisoformat(created.replace('Z','+00:00')).timestamp()*1000) if created else None
                    media_id,result=await asyncio.to_thread(ingest_image,source='google_photos',external_id=item.get('id',''),filename=file.get('filename','photo.jpg'),data=payload,google_create_time=created_ms)
                    counts[result]+=1
            except Exception:
                counts['failed']+=1
                if len(errors)<100:errors.append('MEDIA_IMPORT_FAILED')
            with db() as conn:
                conn.execute('UPDATE import_jobs SET processed=?,total=COALESCE(total,?),imported=?,skipped=?,failed=?,errors_json=? WHERE id=?',(sum(counts.values()),sum(counts.values()),counts['imported'],counts['skipped'],counts['failed'],json.dumps(errors),job_id))
        try:await client.delete_session(google_id)
        except Exception:
            if len(errors)<100:errors.append('GOOGLE_CLEANUP_FAILED')
        status='partial' if counts['failed'] or errors else 'completed'
    except Exception:
        fatal=True; errors.append('GOOGLE_IMPORT_FAILED'); status='failed'
    with db() as conn:
        conn.execute('UPDATE import_jobs SET status=?,finished_at_ms=?,errors_json=? WHERE id=?',(status,now_ms(),json.dumps(errors[:100]),job_id))
        conn.execute('UPDATE picker_sessions SET status=?,last_error_code=?,updated_at_ms=? WHERE id=?',('failed' if fatal else 'completed',errors[-1] if errors else None,now_ms(),local_id))

@router.delete('/picker/sessions/{session_id}',status_code=204)
async def delete_picker(session_id:str,request:Request):
    s=_session(request)
    if not s or not authenticated(s):_fail('GOOGLE_AUTH_REQUIRED',401)
    with db() as conn: row=conn.execute('SELECT * FROM picker_sessions WHERE id=? AND owner_session_hash=?',(session_id,_owner(s))).fetchone()
    if not row: raise HTTPException(404,detail={'error':{'code':'NOT_FOUND','message':'Picker-Sitzung nicht gefunden'}})
    if row['status']=='importing':_fail('IMPORT_IN_PROGRESS',409)
    if authenticated(s):
        try:await PickerClient(access_token(s)).delete_session(row['google_session_id'])
        except GoogleAPIError:pass
    with db() as conn:conn.execute("UPDATE picker_sessions SET status='cancelled',updated_at_ms=? WHERE id=?",(now_ms(),session_id))
    return Response(status_code=204)
