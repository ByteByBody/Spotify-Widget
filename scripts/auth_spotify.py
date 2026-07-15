import json, os
import spotipy
from spotipy.oauth2 import SpotifyOAuth

CONFIG_PATH = os.path.expanduser("~/.config/music-mode/config.json")

def main():
    if not os.path.exists(CONFIG_PATH):
        print(f"Error: {CONFIG_PATH} not found.")
        return
        
    with open(CONFIG_PATH, "r") as f:
        cfg = json.load(f)
        
    cid = cfg.get("spotify_client_id", "")
    secret = cfg.get("spotify_client_secret", "")
    
    if not cid or not secret:
        print("Please follow these steps to enable the Live Queue feature:")
        print("1. Go to https://developer.spotify.com/dashboard")
        print("2. Log in and click 'Create app'")
        print("3. Set App Name to 'Music Mode Widget' and App Description to anything.")
        print("4. Set Redirect URI to 'http://localhost:8080/callback'")
        print("5. Under 'APIs used', select 'Web API'")
        print("6. Click Save.")
        print("7. Go to Settings, copy the 'Client ID' and 'Client secret'")
        print(f"8. Paste them into {CONFIG_PATH}")
        print("9. Run this script again.")
        return
        
    print("Authenticating with Spotify...")
    # Scopes needed for reading queue and modifying playback
    scope = "user-read-currently-playing user-read-playback-state user-modify-playback-state"
    
    cache_path = os.path.expanduser("~/.config/music-mode/.spotify_cache")
    
    sp_oauth = SpotifyOAuth(client_id=cid,
                            client_secret=secret,
                            redirect_uri="http://localhost:8080/callback",
                            scope=scope,
                            cache_path=cache_path)
                            
    # This will prompt the user to open a browser and authorize
    token_info = sp_oauth.get_cached_token()
    if not token_info:
        auth_url = sp_oauth.get_authorize_url()
        print(f"Please navigate here in your browser:\n{auth_url}")
        response = input("Enter the URL you were redirected to: ")
        code = sp_oauth.parse_response_code(response)
        token_info = sp_oauth.get_access_token(code)
        
    if token_info:
        print("Authentication successful! You can now use the Live Queue explorer.")
    else:
        print("Authentication failed.")

if __name__ == "__main__":
    main()
