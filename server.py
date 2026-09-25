import os
import sys
import json
import sqlite3
import hashlib
import time
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Header, Depends, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp
import requests
from bs4 import BeautifulSoup

app = FastAPI(title="Ultimate Downloader API & Admin System", version="11.0")

# CORS setup for mobile app & web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Use persistent disk on Render.com (/app/data) if available, else local
DATA_DIR = "/app/data" if os.path.isdir("/app/data") else BASE_DIR
DB_PATH = os.path.join(DATA_DIR, "app_data.db")
DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ----------------- DATABASE INITIALIZATION -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Users table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'customer',
        notes TEXT DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        download_limit INTEGER DEFAULT -1,
        downloads_count INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)
    
    # System settings table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS system_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)
    
    # Guest / Device download tracking
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS guest_usage (
        device_id TEXT PRIMARY KEY,
        downloads_count INTEGER DEFAULT 0,
        last_download_at TEXT
    )
    """)
    
    # Downloads history table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS download_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        video_url TEXT,
        title TEXT,
        format_picked TEXT,
        downloaded_at TEXT
    )
    """)
    
    # Default settings: free limit 5
    cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('free_download_limit', '5')")
    cursor.execute("INSERT OR IGNORE INTO system_settings (key, value) VALUES ('app_title', 'Ultimate Social Media Downloader PRO')")
    
    # Default Admin account (admin / admin123)
    admin_pass = "admin123"
    admin_hash = hashlib.sha256(admin_pass.encode("utf-8")).hexdigest()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
    INSERT OR IGNORE INTO users (username, password_hash, role, notes, status, download_limit, downloads_count, created_at)
    VALUES ('admin', ?, 'admin', 'Super Administrator', 'active', -1, 0, ?)
    """, (admin_hash, now_str))
    
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(pwd: str) -> str:
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()

def get_free_download_limit():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT value FROM system_settings WHERE key = 'free_download_limit'")
    row = c.fetchone()
    conn.close()
    return int(row["value"]) if row else 5

# ----------------- PYDANTIC SCHEMAS -----------------
class LoginRequest(BaseModel):
    username: str
    password: str
    device_id: Optional[str] = "default_device"

class CreateUserRequest(BaseModel):
    username: str
    password: str
    notes: Optional[str] = ""
    download_limit: Optional[int] = -1

class ResetPasswordRequest(BaseModel):
    username: str
    new_password: str

class UpdateUserStatusRequest(BaseModel):
    username: str
    status: str # 'active' or 'disabled'

class SettingsUpdateRequest(BaseModel):
    free_download_limit: int

class VideoInfoRequest(BaseModel):
    url: str

class VideoDownloadRequest(BaseModel):
    url: str
    quality: Optional[str] = "best"
    format_label: Optional[str] = "Best Available"
    username: Optional[str] = None
    device_id: Optional[str] = "guest_device"

class ProfileScrapeRequest(BaseModel):
    url: str
    target_limit: Optional[int] = 50

class WebScrapeRequest(BaseModel):
    url: str
    scrape_all_pages: Optional[bool] = False

# ----------------- AUTH & USER MANAGEMENT -----------------
@app.post("/api/login")
def login(data: LoginRequest):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (data.username,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
        
    hashed = hash_password(data.password)
    if user["password_hash"] != hashed:
        raise HTTPException(status_code=401, detail="Invalid username or password")
        
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail="Your account has been deactivated by administrator")
        
    free_limit = get_free_download_limit()
    
    return {
        "success": True,
        "username": user["username"],
        "role": user["role"],
        "download_limit": user["download_limit"],
        "downloads_count": user["downloads_count"],
        "free_limit": free_limit
    }

@app.get("/api/user-status")
def get_user_status(username: Optional[str] = None, device_id: Optional[str] = "default_device"):
    free_limit = get_free_download_limit()
    
    if not username:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT downloads_count FROM guest_usage WHERE device_id = ?", (device_id,))
        row = c.fetchone()
        conn.close()
        guest_count = row["downloads_count"] if row else 0
        remaining = max(0, free_limit - guest_count)
        return {
            "role": "guest",
            "is_logged_in": False,
            "downloads_count": guest_count,
            "free_limit": free_limit,
            "remaining_free": remaining,
            "can_download": remaining > 0
        }
        
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = c.fetchone()
    conn.close()
    
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    can_download = (user["download_limit"] == -1) or (user["downloads_count"] < user["download_limit"])
    
    return {
        "role": user["role"],
        "is_logged_in": True,
        "username": user["username"],
        "status": user["status"],
        "downloads_count": user["downloads_count"],
        "download_limit": user["download_limit"],
        "can_download": can_download,
        "free_limit": free_limit
    }

# ----------------- ADMIN PANEL ENDPOINTS -----------------
@app.get("/api/admin/users")
def list_users(admin_user: str = Query(...)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE username = ?", (admin_user,))
    row = c.fetchone()
    if not row or row["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Unauthorized: Admin privileges required")
        
    c.execute("SELECT id, username, role, notes, status, download_limit, downloads_count, created_at FROM users ORDER BY id DESC")
    users = [dict(u) for u in c.fetchall()]
    conn.close()
    return {"users": users}

@app.post("/api/admin/users")
def create_customer(data: CreateUserRequest, admin_user: str = Query(...)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE username = ?", (admin_user,))
    row = c.fetchone()
    if not row or row["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Unauthorized: Admin privileges required")
        
    c.execute("SELECT id FROM users WHERE username = ?", (data.username,))
    if c.fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail="Username already exists")
        
    hashed = hash_password(data.password)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("""
    INSERT INTO users (username, password_hash, role, notes, status, download_limit, downloads_count, created_at)
    VALUES (?, ?, 'customer', ?, 'active', ?, 0, ?)
    """, (data.username, hashed, data.notes, data.download_limit, now_str))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Customer '{data.username}' created successfully!"}

@app.put("/api/admin/reset-password")
def reset_password(data: ResetPasswordRequest, admin_user: str = Query(...)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE username = ?", (admin_user,))
    row = c.fetchone()
    if not row or row["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Unauthorized: Admin privileges required")
        
    c.execute("SELECT id FROM users WHERE username = ?", (data.username,))
    if not c.fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Target user does not exist")
        
    hashed = hash_password(data.new_password)
    c.execute("UPDATE users SET password_hash = ? WHERE username = ?", (hashed, data.username))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Password for '{data.username}' successfully reset!"}

@app.put("/api/admin/users/status")
def update_user_status(data: UpdateUserStatusRequest, admin_user: str = Query(...)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE username = ?", (admin_user,))
    row = c.fetchone()
    if not row or row["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Unauthorized: Admin privileges required")
        
    if data.username == "admin" and data.status == "disabled":
        conn.close()
        raise HTTPException(status_code=400, detail="Cannot disable primary admin account")
        
    c.execute("UPDATE users SET status = ? WHERE username = ?", (data.status, data.username))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"User status updated to '{data.status}'"}

@app.delete("/api/admin/users/{username}")
def delete_user(username: str, admin_user: str = Query(...)):
    if username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete default admin account")
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE username = ?", (admin_user,))
    row = c.fetchone()
    if not row or row["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    c.execute("DELETE FROM users WHERE username = ?", (username,))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"User '{username}' deleted successfully"}

@app.get("/api/admin/stats")
def get_system_stats(admin_user: str = Query(...)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE username = ?", (admin_user,))
    row = c.fetchone()
    if not row or row["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    c.execute("SELECT COUNT(*) as total_users FROM users WHERE role = 'customer'")
    total_customers = c.fetchone()["total_users"]
    
    c.execute("SELECT COUNT(*) as active_users FROM users WHERE role = 'customer' AND status = 'active'")
    active_customers = c.fetchone()["active_users"]
    
    c.execute("SELECT COUNT(*) as total_downloads FROM download_logs")
    total_downloads = c.fetchone()["total_downloads"]
    
    c.execute("SELECT COUNT(*) as total_guests FROM guest_usage")
    total_guests = c.fetchone()["total_guests"]
    
    c.execute("SELECT value FROM system_settings WHERE key = 'free_download_limit'")
    free_limit = int(c.fetchone()["value"])
    
    c.execute("SELECT * FROM download_logs ORDER BY id DESC LIMIT 15")
    recent_logs = [dict(x) for x in c.fetchall()]
    
    conn.close()
    return {
        "total_customers": total_customers,
        "active_customers": active_customers,
        "total_downloads": total_downloads,
        "total_guests": total_guests,
        "free_download_limit": free_limit,
        "recent_downloads": recent_logs
    }

@app.post("/api/admin/settings")
def update_settings(data: SettingsUpdateRequest, admin_user: str = Query(...)):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT role FROM users WHERE username = ?", (admin_user,))
    row = c.fetchone()
    if not row or row["role"] != "admin":
        conn.close()
        raise HTTPException(status_code=403, detail="Unauthorized")
        
    c.execute("UPDATE system_settings SET value = ? WHERE key = 'free_download_limit'", (str(data.free_download_limit),))
    conn.commit()
    conn.close()
    return {"success": True, "message": f"Free tier limit set to {data.free_download_limit} videos"}

# ----------------- DOWNLOAD ENGINE & QUOTA VERIFICATION -----------------
def verify_and_increment_download_quota(username: Optional[str], device_id: str):
    free_limit = get_free_download_limit()
    conn = get_db()
    c = conn.cursor()
    
    if not username:
        # Guest Mode
        c.execute("SELECT downloads_count FROM guest_usage WHERE device_id = ?", (device_id,))
        row = c.fetchone()
        count = row["downloads_count"] if row else 0
        if count >= free_limit:
            conn.close()
            raise HTTPException(
                status_code=403,
                detail=f"Free limit reached ({free_limit}/{free_limit} videos)! Please contact the Admin to get customer login credentials for unlimited downloads."
            )
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("""
        INSERT INTO guest_usage (device_id, downloads_count, last_download_at)
        VALUES (?, 1, ?)
        ON CONFLICT(device_id) DO UPDATE SET downloads_count = downloads_count + 1, last_download_at = ?
        """, (device_id, now_str, now_str))
        conn.commit()
        conn.close()
        return {"role": "guest", "downloads_used": count + 1, "remaining": free_limit - (count + 1)}
    else:
        # Customer / Admin Mode
        c.execute("SELECT * FROM users WHERE username = ?", (username,))
        user = c.fetchone()
        if not user:
            conn.close()
            raise HTTPException(status_code=404, detail="User account not found")
        if user["status"] != "active":
            conn.close()
            raise HTTPException(status_code=403, detail="Customer account is inactive")
        if user["download_limit"] != -1 and user["downloads_count"] >= user["download_limit"]:
            conn.close()
            raise HTTPException(status_code=403, detail="Download quota reached for this account")
            
        c.execute("UPDATE users SET downloads_count = downloads_count + 1 WHERE username = ?", (username,))
        conn.commit()
        conn.close()
        return {"role": user["role"], "downloads_used": user["downloads_count"] + 1, "remaining": "Unlimited"}

# ----------------- VIDEO EXTRACTION & MEDIA API -----------------
@app.post("/api/video/info")
def get_video_info(data: VideoInfoRequest):
    url = data.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="Please enter a valid URL")
        
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            
            title = info.get("title", "Unknown Title")
            duration = info.get("duration", 0)
            view_count = info.get("view_count", 0)
            thumbnail = info.get("thumbnail", "")
            uploader = info.get("uploader", info.get("channel", "Unknown Channel"))
            description = info.get("description", "")
            
            formats_list = []
            if "formats" in info:
                seen_res = set()
                for f in info["formats"]:
                    height = f.get("height")
                    vcodec = f.get("vcodec", "none")
                    ext = f.get("ext", "mp4")
                    if height and height not in seen_res:
                        seen_res.add(height)
                        formats_list.append({
                            "format_id": f.get("format_id"),
                            "resolution": f"{height}p",
                            "ext": ext,
                            "filesize": f.get("filesize") or f.get("filesize_approx") or 0
                        })
            formats_list.sort(key=lambda x: int(x["resolution"].replace("p", "")), reverse=True)
            
            return {
                "success": True,
                "url": url,
                "title": title,
                "duration": duration,
                "duration_formatted": f"{duration//60:02d}:{duration%60:02d}" if duration else "00:00",
                "view_count": view_count,
                "thumbnail": thumbnail,
                "uploader": uploader,
                "description": description[:300] if description else "",
                "formats": formats_list[:6]
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch video: {str(e)}")

@app.post("/api/video/download")
def download_video(data: VideoDownloadRequest):
    # Check quota first
    quota_res = verify_and_increment_download_quota(data.username, data.device_id or "default_device")
    
    url = data.url.strip()
    quality_map = {
        "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best",
        "1080p": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
        "720p": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
        "480p": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
        "audio": "bestaudio/best",
    }
    
    fmt = quality_map.get(data.quality, "best")
    
    ydl_opts = {
        'format': fmt,
        'outtmpl': os.path.join(DOWNLOAD_DIR, '%(title).100s-%(id)s.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
    }
    
    if data.quality == "audio":
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if data.quality == "audio":
                filename = os.path.splitext(filename)[0] + ".mp3"
                
            # Log download
            conn = get_db()
            c = conn.cursor()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            c.execute("""
            INSERT INTO download_logs (username, video_url, title, format_picked, downloaded_at)
            VALUES (?, ?, ?, ?, ?)
            """, (data.username or "Guest", url, info.get("title", "Video"), data.format_label, now_str))
            conn.commit()
            conn.close()
            
            return {
                "success": True,
                "title": info.get("title"),
                "filename": os.path.basename(filename),
                "download_url": f"/api/files/{os.path.basename(filename)}",
                "quota": quota_res
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download error: {str(e)}")

@app.get("/api/files/{filename}")
def serve_downloaded_file(filename: str):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path, filename=filename)

@app.get("/api/library")
def get_library():
    files = []
    for fname in os.listdir(DOWNLOAD_DIR):
        fpath = os.path.join(DOWNLOAD_DIR, fname)
        if os.path.isfile(fpath):
            stat = os.stat(fpath)
            files.append({
                "name": fname,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "url": f"/api/files/{fname}"
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"files": files}

@app.delete("/api/library/{filename}")
def delete_file(filename: str):
    file_path = os.path.join(DOWNLOAD_DIR, filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        return {"success": True}
    raise HTTPException(status_code=404, detail="File not found")

# ----------------- PROFILE SCRAPER & BULK -----------------
@app.post("/api/scrape/profile")
def scrape_profile(data: ProfileScrapeRequest):
    url = data.url.strip()
    target_limit = data.target_limit or 50
    
    ydl_opts = {
        'extract_flat': 'in_playlist',
        'quiet': True,
        'no_warnings': True,
        'playlistend': target_limit,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            entries = info.get("entries", [])
            videos = []
            for e in entries:
                if not e:
                    continue
                videos.append({
                    "id": e.get("id"),
                    "title": e.get("title", "Untitled"),
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                    "duration": e.get("duration", 0),
                    "duration_str": f"{e.get('duration', 0)//60:02d}:{e.get('duration', 0)%60:02d}" if e.get("duration") else "00:00",
                    "views": e.get("view_count", 0),
                    "thumbnail": e.get("thumbnail") or (e.get("thumbnails", [{}])[0].get("url") if e.get("thumbnails") else ""),
                    "uploader": e.get("uploader", info.get("title", "Channel"))
                })
                
            return {
                "success": True,
                "channel_title": info.get("title", "Channel"),
                "total_found": len(videos),
                "videos": videos
            }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Scraping error: {str(e)}")

# ----------------- WEBSITE VIDEO SCRAPER -----------------
@app.post("/api/scrape/website")
def scrape_website(data: WebScrapeRequest):
    url = data.url.strip()
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        resp = requests.get(url, headers=headers, timeout=12)
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        found = []
        for v in soup.find_all('video'):
            src = v.get('src')
            if src:
                found.append(src if src.startswith("http") else requests.compat.urljoin(url, src))
            for s in v.find_all('source'):
                ssrc = s.get('src')
                if ssrc:
                    found.append(ssrc if ssrc.startswith("http") else requests.compat.urljoin(url, ssrc))
                    
        video_exts = ('.mp4', '.mkv', '.webm', '.mov', '.avi', '.mp3')
        for a in soup.find_all('a', href=True):
            href = a['href']
            if any(href.lower().endswith(ext) for ext in video_exts):
                found.append(href if href.startswith("http") else requests.compat.urljoin(url, href))
                
        raw_matches = re.findall(r'(https?://[^\s"\'<>]+\.(?:mp4|webm|m3u8|mp3))', resp.text)
        found.extend(raw_matches)
        
        unique_found = list(dict.fromkeys(found))
        items = []
        for idx, u in enumerate(unique_found):
            items.append({
                "id": idx + 1,
                "url": u,
                "name": os.path.basename(u.split("?")[0]) or f"video_{idx+1}.mp4"
            })
            
        return {
            "success": True,
            "url": url,
            "count": len(items),
            "media": items
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Website scraping failed: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
