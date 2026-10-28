import os, glob, json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
c = Credentials(None, refresh_token=os.environ["YT_REFRESH"], token_uri="https://oauth2.googleapis.com/token",
    client_id=os.environ["YT_ID"], client_secret=os.environ["YT_SECRET"])
yt = build("youtube", "v3", credentials=c)
v = sorted(glob.glob("out/short_*.mp4"), key=os.path.getmtime)[-1]
m = json.load(open("out/meta.json"))
r = yt.videos().insert(part="snippet,status",
    body={"snippet": {"title": m["title"], "description": m["description"], "tags": m["tags"], "categoryId": "27",
                      "defaultLanguage": m.get("language", "en")},
          "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}},
    media_body=MediaFileUpload(v)).execute()
print("uploaded", r["id"], r.get("status", {}).get("privacyStatus"))
