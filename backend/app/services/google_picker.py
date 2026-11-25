from __future__ import annotations

import asyncio
import httpx
from app.config import Settings,settings

BASE='https://photospicker.googleapis.com/v1'

class GoogleAPIError(RuntimeError):
    def __init__(self, code): self.code=code

class PickerClient:
    def __init__(self,token:str,config:Settings=settings):
        self.token=token; self.config=config

    async def request(self,method,path,**kwargs):
        headers={'Authorization':f'Bearer {self.token}'}
        # Disable redirects: never forward an OAuth bearer token to another host.
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            response=await client.request(method,BASE+path,headers=headers,**kwargs)
        if response.status_code>=400:
            if response.status_code==401: raise GoogleAPIError('GOOGLE_AUTH_REQUIRED')
            if response.status_code==403: raise GoogleAPIError('GOOGLE_PERMISSION_DENIED')
            if response.status_code==429: raise GoogleAPIError('GOOGLE_RATE_LIMITED')
            raise GoogleAPIError('GOOGLE_API_ERROR')
        return response.json() if response.content else {}

    async def create_session(self): return await self.request('POST','/sessions',json={})
    async def get_session(self,google_id): return await self.request('GET',f'/sessions/{google_id}')
    async def delete_session(self,google_id): return await self.request('DELETE',f'/sessions/{google_id}')
    async def list_media(self,google_id):
        token=None
        while True:
            params={'sessionId':google_id}
            if token: params['pageToken']=token
            page=await self.request('GET','/mediaItems',params=params)
            for item in page.get('mediaItems',[]): yield item
            token=page.get('nextPageToken')
            if not token: break
