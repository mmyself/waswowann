from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass

import httpx
from app.config import Settings, settings
from app.db import db, now_ms

SCOPE='https://www.googleapis.com/auth/photospicker.mediaitems.readonly'
AUTH_URL='https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL='https://oauth2.googleapis.com/token'

@dataclass
class OAuthAttempt:
    state: str
    verifier: str
    expires_at_ms: int

_attempts: dict[str, OAuthAttempt]={}
_tokens: dict[str, dict]={}

# Picker sessions must not be claimed by another browser after an OAuth restart.
def session_owner_hash(app_session: str) -> str:
    return hashlib.sha256(app_session.encode()).hexdigest()

def _b64(data:bytes)->str: return base64.urlsafe_b64encode(data).rstrip(b'=').decode()

def start_oauth(app_session:str,config:Settings=settings)->str:
    if not config.google_enabled: raise RuntimeError('GOOGLE_NOT_CONFIGURED')
    state=secrets.token_urlsafe(32); verifier=_b64(secrets.token_bytes(48))
    _attempts[app_session]=OAuthAttempt(state,verifier,now_ms()+600_000)
    challenge=_b64(hashlib.sha256(verifier.encode()).digest())
    params={'client_id':config.google_client_id,'redirect_uri':config.google_redirect_uri,'response_type':'code','scope':SCOPE,'state':state,'code_challenge':challenge,'code_challenge_method':'S256','access_type':'online'}
    return AUTH_URL+'?'+str(httpx.QueryParams(params))

async def finish_oauth(app_session:str,state:str,code:str,config:Settings=settings):
    attempt=_attempts.pop(app_session,None)
    if not attempt or attempt.expires_at_ms<now_ms() or not secrets.compare_digest(attempt.state,state):
        raise ValueError('OAUTH_STATE_INVALID')
    async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
        response=await client.post(TOKEN_URL,data={'code':code,'client_id':config.google_client_id,'client_secret':config.google_client_secret,'redirect_uri':config.google_redirect_uri,'grant_type':'authorization_code','code_verifier':attempt.verifier})
    if response.status_code!=200: raise ValueError('OAUTH_EXCHANGE_FAILED')
    token=response.json()
    _tokens[app_session]={'access_token':token['access_token'],'expires_at_ms':now_ms()+int(token.get('expires_in',3600))*1000}

def access_token(app_session:str)->str:
    token=_tokens.get(app_session)
    if not token or token['expires_at_ms']<=now_ms():
        _tokens.pop(app_session,None)
        raise PermissionError('GOOGLE_AUTH_REQUIRED')
    return token['access_token']

def authenticated(app_session:str)->bool:
    return bool(_tokens.get(app_session) and _tokens[app_session]['expires_at_ms']>now_ms())
