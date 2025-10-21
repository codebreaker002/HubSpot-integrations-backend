# slack.py

import json
import secrets
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
import httpx 
from datetime import datetime

from backend.redis_client import add_key_value_redis, delete_key_redis, get_value_redis
from integrations.integration_item import IntegrationItem

CLIENT_ID = 'pat-na2-7658ae34-a96a-4c7e-b8ee-5b66c55987ba'
CLIENT_SECRET = 'bcaec9c1-9c49-48e4-9f67-2f1c6d775d47'
REDIRECT_URI = 'http://localhost:8000/integrations/hubspot/oauth2callback'
SCOPES = 'crm.objects.contacts.read', 'crm.schemas.contacts.read'
AUTH_BASE = 'https://app.hubspot.com/oauth/authorize'
TOKEN_URL = 'https://api.hubapi.com/oauth/v1/token'



async def authorize_hubspot(user_id, org_id):
    state_data = {
        'state': secrets.token_urlsafe(32),
        'user_id': user_id,
        'org_id': org_id
    }
    encoded_state = json.dumps(state_data)
    await add_key_value_redis(f'hubspot_state:{org_id}:{user_id}', encoded_state, expire=600)

    return f'{AUTH_BASE}?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&scope={SCOPES}&state={encoded_state}'

async def oauth2callback_hubspot(request: Request):
    if request.query_params.get('error'):
        raise HTTPException(status_code=400, detail=request.query_params.get('error'))
    code = request.query_params.get('code')
    encoded_state = request.query_params.get('state')
    state_data = json.loads(encoded_state)

    original_state = state_data.get('state')
    user_id = state_data.get('user_id')
    org_id = state_data.get('org_id')
    saved_state = await get_value_redis(f'hubspot_state:{org_id}:{user_id}')
    
    if not saved_state or original_state != json.loads(saved_state).get('state'):
        raise HTTPException(status_code=400, detail='State does not match.')
    
    # Exchange code for access token
    
    async with httpx.AsyncClient() as client:
        response = await client.post(
            TOKEN_URL,
            data={
                'grant_type': 'authorization_code',
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'redirect_uri': REDIRECT_URI,
                'code': code
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        delete_key_redis(f'hubspot_state:{org_id}:{user_id}')
    if response.status_code != 200:
        raise HTTPException(status_code=400, detail='Failed to obtain access token from HubSpot.')
    await add_key_value_redis(f'hubspot_credentials:{org_id}:{user_id}', json.dumps(response.json()), expire=600)
    
    close_window_script = """
    <html>
        <script>
            window.close();
        </script>
    </html>
    """
    return HTMLResponse(content=close_window_script)
    

async def get_hubspot_credentials(user_id, org_id):
    credentials = await get_value_redis(f'hubspot_credentials:{org_id}:{user_id}')
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    credentials = json.loads(credentials)
    if not credentials:
        raise HTTPException(status_code=400, detail='No credentials found.')
    await delete_key_redis(f'hubspot_credentials:{org_id}:{user_id}')

    return credentials

async def create_integration_item_metadata_object(response_json):
    
    props = response_json.get("properties", {}) or {}

    first = props.get("firstname") or ""
    last = props.get("lastname") or ""
    email = props.get("email")
    # Derive a human‑readable name
    name_parts = [p for p in [first.strip(), last.strip()] if p]
    if name_parts:
        name = " ".join(name_parts)
    elif email:
        name = email
    else:
        name = f'HubSpot Object {response_json.get("id")}'

    # Prefer top-level timestamps; fall back to property timestamps.
    created_raw = response_json.get("createdAt") or props.get("createdate")
    updated_raw = response_json.get("updatedAt") or props.get("lastmodifieddate")

    def _parse_dt(value):
        if not value:
            return None
        # Normalize Z suffix for fromisoformat
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None

    created_dt = _parse_dt(created_raw)
    updated_dt = _parse_dt(updated_raw)

    integration_item = IntegrationItem(
        id=response_json.get("id"),
        type=response_json.get("objectType") or response_json.get("type") or "Contact",
        name=name,
        creation_time=created_dt,
        last_modified_time=updated_dt,
        # HubSpot contacts have no simple parent container analogous to bases/folders here
        parent_id=None,
        parent_path_or_name=None,
        url=None,  # Could be constructed if portal/portalId is available
        visibility=not response_json.get("archived", False),
    )
    return integration_item

async def get_items_hubspot(credentials):
    """
    Fetch HubSpot contact objects and convert them into IntegrationItem instances.
    Expects credentials (JSON string) containing at least an access_token.
    """
    if isinstance(credentials, str):
        credentials = json.loads(credentials)
    access_token = credentials.get("access_token")
    if not access_token:
        raise HTTPException(status_code=400, detail="Missing access token.")

    base_url = "https://api.hubapi.com/crm/v3/objects/contacts"
    params = {
        "limit": 100,
        "archived": "false",
        "properties": "firstname,lastname,email,createdate,lastmodifieddate"
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    items: list[IntegrationItem] = []
    after = None

    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            if after:
                params["after"] = after
            response = await client.get(base_url, params=params, headers=headers)
            if response.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"HubSpot contacts fetch failed: {response.text}"
                )
            payload = response.json()
            for obj in payload.get("results", []):
                item = await create_integration_item_metadata_object(obj)
                items.append(item)
            paging = payload.get("paging", {})
            next_info = paging.get("next", {})
            after = next_info.get("after")
            if not after:
                break

    return items