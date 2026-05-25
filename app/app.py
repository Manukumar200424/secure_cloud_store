import os
import base64
import io
from datetime import timedelta, datetime
from bson import ObjectId
from flask import (
    Flask, render_template, request, redirect, url_for, session,
    jsonify, send_file
)
from pymongo import MongoClient
import gridfs
import bcrypt
from dotenv import load_dotenv

load_dotenv()

# Configure Flask to use correct template and static folders
template_dir = os.path.join(os.path.dirname(__file__), '..', 'templates')
static_dir = os.path.join(os.path.dirname(__file__), '..', 'static')

app = Flask(__name__, template_folder=template_dir, static_folder=static_dir)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret")
app.permanent_session_lifetime = timedelta(hours=6)
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2GB max upload size

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "privacy_store")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
users_col = db["users"]
fs = gridfs.GridFS(db, collection="files_fs")

# Helpers
def get_current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = users_col.find_one({"_id": ObjectId(user_id)})
    if user:
        if "username" not in session:
            session["username"] = user["username"]
        if "is_admin" not in session:
            session["is_admin"] = user.get("is_admin", False)
    return user

@app.route("/")
def index():
    if session.get("user_id"):
        return redirect(url_for("home"))
    return render_template("index.html")

# Pages
@app.route("/home")
def home():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("home.html", username=user["username"], is_admin=user.get("is_admin"))

@app.route("/profile")
def profile():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("profile.html",
        username=user["username"],
        fullname=user.get("fullname", ""),
        email=user.get("email", ""),
        phone=user.get("phone", ""),
        gender=user.get("gender", ""),
        dob=user.get("dob", ""),
        joined=user.get("joined", "")
    )

@app.route("/support")
def support():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("support.html", username=user["username"])

@app.route("/register")
def register():
    return render_template("register.html")

@app.route("/login")
def login():
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("dashboard.html", username=user["username"])

@app.route("/upload")
def upload():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    return render_template("upload.html", username=user["username"])

@app.route("/admin")
def admin():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))
    
    if not user.get("is_admin"):
        return redirect(url_for("home"))

    # Create support threads collection if it doesn't exist
    if "support_threads" not in db.list_collection_names():
        # Create initial threads from existing users
        existing_users = list(users_col.find({}))
        for u in existing_users:
            thread = {
                "_id": u["_id"],
                "created_at": datetime.utcnow(),
                "timestamp": datetime.utcnow()
            }
            db["support_threads"].insert_one(thread)

    # 1. Usage Stats (Total Size & File Count per User)
    pipeline_usage = [
        {"$group": {
            "_id": "$metadata.owner_id",
            "totalSize": {"$sum": "$length"},
            "fileCount": {"$sum": 1},
            "lastUpload": {"$max": "$uploadDate"}
        }}
    ]
    usage_stats = list(db["files_fs.files"].aggregate(pipeline_usage))
    stats_map = {str(stat["_id"]): stat for stat in usage_stats}
    
    # 2. Global File Type Distribution
    # Fetch all filenames to determine type
    all_files_cursor = db["files_fs.files"].find({}, {"filename": 1})
    file_types = {"Images": 0, "Videos": 0, "Audio": 0, "Documents": 0, "Archives": 0, "Others": 0}
    
    for f in all_files_cursor:
        fname = f.get("filename", "").lower()
        if fname.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')):
            file_types["Images"] += 1
        elif fname.endswith(('.mp4', '.webm', '.ogg', '.mov', '.avi')):
            file_types["Videos"] += 1
        elif fname.endswith(('.mp3', '.wav', '.aac', '.flac')):
            file_types["Audio"] += 1
        elif fname.endswith(('.pdf', '.doc', '.docx', '.txt', '.xls', '.xlsx', '.ppt', '.pptx')):
            file_types["Documents"] += 1
        elif fname.endswith(('.zip', '.rar', '.7z', '.tar', '.gz')):
            file_types["Archives"] += 1
        else:
            file_types["Others"] += 1

    # Fetch all users
    all_users = list(users_col.find())
    
    users_data = []
    total_system_storage = 0
    total_system_files = 0

    for u in all_users:
        uid = str(u["_id"])
        stat = stats_map.get(uid, {"totalSize": 0, "fileCount": 0, "lastUpload": None})
        
        size_bytes = stat["totalSize"]
        file_count = stat["fileCount"]
        last_active = stat.get("lastUpload")
        
        total_system_storage += size_bytes
        total_system_files += file_count
        
        # Format size
        if size_bytes < 1024:
            size_fmt = f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            size_fmt = f"{size_bytes / 1024:.2f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            size_fmt = f"{size_bytes / (1024 * 1024):.2f} MB"
        else:
            size_fmt = f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

        # Format last active
        last_active_str = last_active.strftime("%Y-%m-%d %H:%M") if last_active else "Never"
        
        # Calculate percentage (max 2GB reference)
        storage_percent = min((size_bytes / (2 * 1024 * 1024 * 1024)) * 100, 100)

        users_data.append({
            "username": u["username"],
            "_id": uid,
            "is_admin": u.get("is_admin", False),
            "file_count": file_count,
            "storage_bytes": size_bytes,
            "storage_formatted": size_fmt,
            "last_active": last_active_str,
            "storage_percent": storage_percent
        })

    # Format total system storage
    if total_system_storage < 1024:
        total_str = f"{total_system_storage} B"
    elif total_system_storage < 1024 * 1024:
        total_str = f"{total_system_storage / 1024:.2f} KB"
    elif total_system_storage < 1024 * 1024 * 1024:
        total_str = f"{total_system_storage / (1024 * 1024):.2f} MB"
    else:
        total_str = f"{total_system_storage / (1024 * 1024 * 1024):.2f} GB"

    return render_template(
        "admin.html", 
        username=user["username"],
        users=users_data, 
        total_users=len(all_users), 
        total_storage_formatted=total_str,
        total_files=total_system_files,
        file_types=file_types
    )

# API: Register
@app.route("/api/register", methods=["POST"])
def api_register():
    from datetime import datetime
    payload = request.json
    username = payload.get("username", "").strip()
    password = payload.get("password", "")
    email    = payload.get("email", "").strip()
    fullname = payload.get("fullname", "").strip()
    phone    = payload.get("phone", "").strip()
    gender   = payload.get("gender", "").strip()
    dob      = payload.get("dob", "").strip()

    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    if not email or not fullname:
        return jsonify({"error": "name and email are required"}), 400
    if users_col.find_one({"username": username}):
        return jsonify({"error": "username taken"}), 400
    if users_col.find_one({"email": email}):
        return jsonify({"error": "email already registered"}), 400

    # Store bcrypt hash
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
    # Generate an encryption salt for PBKDF2 (client-side key derivation)
    enc_salt = base64.b64encode(os.urandom(16)).decode()

    user = {
        "username": username,
        "password_hash": pw_hash,
        "enc_salt": enc_salt,
        "email": email,
        "fullname": fullname,
        "phone": phone,
        "gender": gender,
        "dob": dob,
        "joined": datetime.utcnow().strftime("%B %Y"),
        "is_admin": False
    }
    users_col.insert_one(user)
    return jsonify({"ok": True})

# API: Login
@app.route("/api/login", methods=["POST"])
def api_login():
    payload = request.json
    username = payload.get("username", "").strip()
    password = payload.get("password", "")
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    user = users_col.find_one({"username": username})
    if not user:
        return jsonify({"error": "invalid credentials"}), 401
    if not bcrypt.checkpw(password.encode(), user["password_hash"]):
        return jsonify({"error": "invalid credentials"}), 401

    # set session
    session.permanent = True
    session["user_id"] = str(user["_id"])
    session["username"] = user["username"]
    session["is_admin"] = user.get("is_admin", False)
    # Return encryption salt so the client can derive the key
    return jsonify({"ok": True, "enc_salt": user["enc_salt"], "username": user["username"]})

# Logout
@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

# API: Logout
@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("user_id", None)
    return jsonify({"ok": True})

# API: Upload (encrypted file)
@app.route("/api/upload", methods=["POST"])
def api_upload():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401

    # Expecting multipart/form-data: 'file' (encrypted Blob), 'orig_filename', 'iv' (base64)
    if "file" not in request.files:
        return jsonify({"error": "no file uploaded"}), 400
    f = request.files["file"]
    orig_filename = request.form.get("orig_filename", f.filename)
    iv_b64 = request.form.get("iv")
    if not iv_b64:
        return jsonify({"error": "iv missing"}), 400

    # Save encrypted blob to GridFS with metadata
    metadata = {
        "owner_id": ObjectId(user["_id"]),
        "orig_filename": orig_filename,
        "iv": iv_b64,
        "uploader": user["username"]
    }
    # Use f directly to stream potential large files
    file_id = fs.put(f, filename=orig_filename, metadata=metadata)
    return jsonify({"ok": True, "file_id": str(file_id)})

# API: List files for user
@app.route("/api/files", methods=["GET"])
def api_files():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401
    files = []
    for doc in db["files_fs.files"].find({"metadata.owner_id": ObjectId(user["_id"])}):
        files.append({
            "file_id": str(doc["_id"]),
            "filename": doc.get("filename"),
            "upload_date": doc.get("uploadDate").isoformat(),
            "length": doc.get("length"),
            "iv": doc.get("metadata", {}).get("iv"),
        })
    return jsonify({"files": files})

# API: Download encrypted file (server does NOT decrypt)
@app.route("/api/download/<file_id>", methods=["GET"])
def api_download(file_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401
    try:
        gid = ObjectId(file_id)
    except Exception:
        return jsonify({"error": "invalid file id"}), 400
    # Ensure owner
    filedoc = db["files_fs.files"].find_one({"_id": gid, "metadata.owner_id": ObjectId(user["_id"])})
    if not filedoc:
        return jsonify({"error": "file not found"}), 404
    
    # Stream file back using GridOut as file-like object
    grid_out = fs.get(gid)
    iv_b64 = filedoc.get("metadata", {}).get("iv", "")
    orig_fn = filedoc.get("filename", "encrypted.bin")
    
    response = send_file(
        grid_out,
        as_attachment=True,
        download_name=orig_fn,
        mimetype="application/octet-stream"
    )
    response.headers["X-File-IV"] = iv_b64
    response.headers["X-Orig-Filename"] = orig_fn
    return response

# API: Delete file
@app.route("/api/delete/<file_id>", methods=["POST"])
def api_delete(file_id):
    user = get_current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401
    try:
        gid = ObjectId(file_id)
    except Exception:
        return jsonify({"error": "invalid file id"}), 400
    # Ensure owner
    filedoc = db["files_fs.files"].find_one({"_id": gid, "metadata.owner_id": ObjectId(user["_id"])})
    if not filedoc:
        return jsonify({"error": "file not found"}), 404
    # Delete file and chunks
    fs.delete(gid)
    return jsonify({"ok": True})

# API: Get enc_salt without login (optional) - requires login in this demo
@app.route("/api/enc_salt", methods=["GET"])
def api_enc_salt():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({"enc_salt": user["enc_salt"]})

# --- Support Chat System ---

# API: Send message (Used by both admin and user)
@app.route("/api/chat/send", methods=["POST"])
def api_chat_send():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401
    
    payload = request.json
    message = payload.get("message", "").strip()
    # If admin is replying to a specific user
    target_user_id = payload.get("target_user_id") 
    
    if not message:
        return jsonify({"error": "message is empty"}), 400

    chat_msg = {
        "sender_id": ObjectId(user["_id"]),
        "sender_name": user["username"],
        "message": message,
        "timestamp": db.command("serverStatus")["localTime"], # sync with DB time
        "is_admin": user.get("is_admin", False)
    }

    if user.get("is_admin") and target_user_id:
        chat_msg["thread_id"] = ObjectId(target_user_id)
    else:
        chat_msg["thread_id"] = ObjectId(user["_id"])

    db["support_chats"].insert_one(chat_msg)
    return jsonify({"ok": True})

# API: Get chat history
@app.route("/api/chat/history", methods=["GET"])
def api_chat_history():
    user = get_current_user()
    if not user:
        return jsonify({"error": "not authenticated"}), 401
    
    # If admin is viewing a specific thread
    target_user_id = request.args.get("target_user_id")
    
    if user.get("is_admin") and target_user_id:
        thread_id = ObjectId(target_user_id)
    else:
        thread_id = ObjectId(user["_id"])

    messages = list(db["support_chats"].find({"thread_id": thread_id}).sort("timestamp", 1))
    
    serialized = []
    for m in messages:
        serialized.append({
            "sender_name": m["sender_name"],
            "message": m["message"],
            "timestamp": m["timestamp"].isoformat(),
            "is_admin": m.get("is_admin", False)
        })
    
    return jsonify({"messages": serialized})

# API: Get all active chat threads (Admin only)
@app.route("/api/admin/chat/threads", methods=["GET"])
def api_admin_chat_threads():
    user = get_current_user()
    if not user or not user.get("is_admin", False):
        return jsonify({"error": "admin access required"}), 403
    
    threads = []
    # Get all users who have sent messages
    message_senders = db["support_chats"].distinct("thread_id")
    
    for thread_id in message_senders:
        # Get last message for this thread
        last_msg = db["support_chats"].find_one({"thread_id": thread_id}, sort=[("timestamp", -1)])
        u = users_col.find_one({"_id": thread_id})
        
        if u:
            threads.append({
                "user_id": str(thread_id),
                "username": u["username"],
                "last_message": last_msg["message"] if last_msg else "No messages",
                "timestamp": last_msg["timestamp"].isoformat() if last_msg else datetime.utcnow().isoformat()
            })

    return jsonify({"threads": threads})

if __name__ == "__main__":
    app.run(debug=True)