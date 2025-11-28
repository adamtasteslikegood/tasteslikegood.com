import os
from flask import Blueprint, redirect, url_for, session, request, abort
from google_auth_oauthlib.flow import Flow
import google.oauth2.credentials
import googleapiclient.discovery
from dotenv import load_dotenv

load_dotenv()

auth_bp = Blueprint('auth', __name__)

SCOPES = ['openid', 'https://www.googleapis.com/auth/userinfo.email', 'https://www.googleapis.com/auth/userinfo.profile']

@auth_bp.route('/login')
def login():
    client_config = {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for('auth.callback', _external=True)],
        }
    }
    flow = Flow.from_client_config(
        client_config, scopes=SCOPES)
    flow.redirect_uri = url_for('auth.callback', _external=True)
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true')
    session['state'] = state
    return redirect(authorization_url)

@auth_bp.route('/callback')
def callback():
    state = session.get('state')
    if not state:
        abort(400, description="State missing from session.")

    client_config = {
        "web": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID"),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [url_for('auth.callback', _external=True)],
        }
    }
    
    try:
        flow = Flow.from_client_config(
            client_config, scopes=SCOPES, state=state)
        flow.redirect_uri = url_for('auth.callback', _external=True)
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)
        credentials = flow.credentials
        session['credentials'] = credentials_to_dict(credentials)

        # Get user info
        userinfo_service = googleapiclient.discovery.build('oauth2', 'v2', credentials=credentials)
        user_info = userinfo_service.userinfo().get().execute()
        session['user_info'] = user_info
        
    except Exception as e:
        abort(500, description=f"Authentication failed: {str(e)}")

    return redirect(url_for('index'))



@auth_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

def credentials_to_dict(credentials):
    return {'token': credentials.token,
            'refresh_token': credentials.refresh_token,
            'token_uri': credentials.token_uri,
            'client_id': credentials.client_id,
            'client_secret': credentials.client_secret,
            'scopes': credentials.scopes}
