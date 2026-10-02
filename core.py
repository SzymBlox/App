"""RobloxTools core (no UI): storage paths, Supabase client, downloads, export.

Everything the phone app does with files and the network lives here, so it can
be tested on a PC without Kivy.
"""

import io
import os
import re
import sys
import json
import time
import socket
import shutil
import hashlib
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import concurrent.futures

try:  # Android has no system certificate store Python can use
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
except Exception:
    pass

try:
    from PIL import Image, ImageOps
except Exception:  # Pillow is only needed for the admin image upload
    Image = ImageOps = None

APP_NAME = "RobloxTools"
APP_VERSION = "1.0"

SUPABASE_URL = "https://xzmdqclpqkxcurqsbifx.supabase.co"
SUPABASE_KEY = "sb_publishable_uFuSKjXu7RMFim86ZupaAQ_6b9W1JdF"  # publishable: safe to ship
IMAGE_BUCKET = "plugin-images"

USER_AGENT = f"Mozilla/5.0 (Linux; Android 14) {APP_NAME}/{APP_VERSION}"
PLUGIN_EXTENSIONS = (".rbxm", ".rbxmx", ".lua", ".luau")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
PLUGIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SCAN_DEPTH = 8

# ---------------------------------------------------------------------------
# Storage layout - everything lives in internal storage (/storage/emulated/0)
#
#   /storage/emulated/0/RobloxTools/plugins           <- plugins from the store
#   /storage/emulated/0/RobloxTools/exported plugins  <- result of an export
# ---------------------------------------------------------------------------

ON_ANDROID = "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")

STORAGE_ROOT = "/storage/emulated/0" if ON_ANDROID else os.path.join(os.path.expanduser("~"), "RobloxToolsStorage")
BASE_DIR = os.path.join(STORAGE_ROOT, "RobloxTools")
PLUGINS_DIR = os.path.join(BASE_DIR, "plugins")
EXPORT_DIR = os.path.join(BASE_DIR, "exported plugins")

# Private app data (settings, cache). Set by init_data_dir() once the app knows it.
DATA_DIR = os.path.join(os.path.expanduser("~"), ".robloxtools")


def init_data_dir(path):
    global DATA_DIR
    DATA_DIR = path
    os.makedirs(cache_dir(), exist_ok=True)


def settings_path():
    return os.path.join(DATA_DIR, "settings.json")


def cache_dir():
    return os.path.join(DATA_DIR, "cache")


def catalog_cache_path():
    return os.path.join(cache_dir(), "catalog.json")


def installed_registry_path():
    # Kept inside the plugins folder so it survives reinstalling the app.
    return os.path.join(PLUGINS_DIR, ".installed.json")


def export_index_path():
    return os.path.join(EXPORT_DIR, ".plugin_index.json")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def log_exception(exc_type, exc, tb):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n"))
            f.write("".join(traceback.format_exception(exc_type, exc, tb)) + "\n")
    except Exception:
        pass


def format_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_timestamp(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts))) if ts else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def format_date(text):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(text or ""))
    if not m:
        return ""
    year, month, day = (int(g) for g in m.groups())
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{months[month - 1]} {day}, {year}" if 1 <= month <= 12 else ""


def ellipsize(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: max(limit - 1, 1)].rstrip() + "…"


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:64].strip("-")


def sanitize_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name or "")).strip(" .")
    return (cleaned or "unnamed_plugin")[:200]


# ---------------------------------------------------------------------------
# HTTP + Supabase
# ---------------------------------------------------------------------------

class CatalogError(Exception):
    pass


class DownloadError(Exception):
    pass


class ApiError(Exception):
    pass


def http_get(url, timeout=15):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def has_internet_connection(host="economy.roblox.com", port=443, timeout=3):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _api_error_message(error):
    try:
        body = json.loads(error.read().decode("utf-8", "replace"))
        if isinstance(body, dict):
            for key in ("msg", "message", "error_description", "error"):
                if body.get(key):
                    return str(body[key])
    except Exception:
        pass
    return f"The server answered with HTTP {error.code}."


class SupabaseClient:
    """Tiny REST client for the catalog. Reading is public; every write needs
    the token of a signed-in admin and the *server* enforces that rule."""

    def __init__(self, url=SUPABASE_URL, key=SUPABASE_KEY):
        self.url = re.sub(r"/(rest|auth|storage)/v1/?$", "", (url or "").strip().rstrip("/"))
        self.key = (key or "").strip()
        self.session = None
        self._lock = threading.Lock()

    @property
    def configured(self):
        return bool(self.url and self.key)

    @property
    def signed_in(self):
        return self.session is not None

    @property
    def email(self):
        return (self.session or {}).get("email", "")

    def _headers(self, auth, extra):
        headers = {"apikey": self.key, "User-Agent": USER_AGENT}
        if auth and self.session:
            headers["Authorization"] = "Bearer " + self.session["access_token"]
        elif self.key.startswith("eyJ"):
            headers["Authorization"] = "Bearer " + self.key
        headers.update(extra or {})
        return headers

    def _request(self, method, path, body=None, auth=False, headers=None, raw=None, timeout=20):
        if not self.configured:
            raise ApiError("No catalog server is configured in this build.")
        if auth:
            self._ensure_fresh_token()
        extra = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            extra.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers=self._headers(auth, extra))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as e:
            raise ApiError(_api_error_message(e))
        except (urllib.error.URLError, socket.timeout, OSError):
            raise ApiError("Can't reach the catalog server - check your internet connection.")
        if not payload:
            return None
        try:
            return json.loads(payload.decode("utf-8"))
        except ValueError:
            return None

    def _store_session(self, data, email=""):
        if not isinstance(data, dict) or not data.get("access_token"):
            raise ApiError("Sign-in failed.")
        self.session = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": time.time() + int(data.get("expires_in") or 3600),
            "email": (data.get("user") or {}).get("email") or email,
        }

    def _ensure_fresh_token(self):
        with self._lock:
            session = self.session
            if not session:
                raise ApiError("You are signed out. Sign in again in Settings.")
            if session["expires_at"] - time.time() > 90:
                return
            try:
                data = self._request("POST", "/auth/v1/token?grant_type=refresh_token",
                                     body={"refresh_token": session["refresh_token"]})
                self._store_session(data, session["email"])
            except ApiError:
                self.session = None
                raise ApiError("Your admin session expired. Sign in again in Settings.")

    def sign_in(self, email, password):
        data = self._request("POST", "/auth/v1/token?grant_type=password",
                             body={"email": email, "password": password})
        self._store_session(data, email)
        try:
            is_admin = self._request("POST", "/rest/v1/rpc/is_admin", body={}, auth=True)
        except ApiError:
            self.session = None
            raise
        if is_admin is not True:
            self.sign_out()
            raise ApiError("This account is not a catalog admin.")
        return self.email

    def sign_out(self):
        if self.session:
            try:
                self._request("POST", "/auth/v1/logout", body={}, auth=True, timeout=8)
            except ApiError:
                pass
        self.session = None

    def fetch_plugins(self, include_hidden=False):
        path = "/rest/v1/plugins?select=*&order=name.asc"
        if not include_hidden:
            path += "&published=eq.true"
        rows = self._request("GET", path, auth=include_hidden)
        return rows if isinstance(rows, list) else []

    def save_plugin(self, row):
        rows = self._request(
            "POST", "/rest/v1/plugins?on_conflict=id", body=[row], auth=True,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        if not rows:
            raise ApiError("The server didn't accept the change (is this account still an admin?).")
        return rows[0]

    def delete_plugin(self, plugin_id):
        rows = self._request(
            "DELETE", "/rest/v1/plugins?id=eq." + urllib.parse.quote(plugin_id, safe=""),
            auth=True, headers={"Prefer": "return=representation"},
        )
        if not rows:
            raise ApiError("The server didn't delete the plugin (is this account still an admin?).")

    def public_url(self, object_path):
        return f"{self.url}/storage/v1/object/public/{IMAGE_BUCKET}/{object_path}"

    def object_path_from_url(self, url):
        prefix = f"{self.url}/storage/v1/object/public/{IMAGE_BUCKET}/"
        return url[len(prefix):] if url.startswith(prefix) else None

    def upload_image(self, object_path, data, content_type):
        self._request(
            "POST", f"/storage/v1/object/{IMAGE_BUCKET}/{urllib.parse.quote(object_path)}",
            raw=data, auth=True, timeout=60,
            headers={"Content-Type": content_type, "x-upsert": "true", "cache-control": "max-age=31536000"},
        )
        return self.public_url(object_path)

    def delete_images(self, urls):
        paths = [p for p in (self.object_path_from_url(u) for u in urls) if p]
        if paths:
            self._request("DELETE", f"/storage/v1/object/{IMAGE_BUCKET}", body={"prefixes": paths}, auth=True)


def parse_catalog(rows):
    plugins, seen = [], set()
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        url = str(item.get("download_url") or "").strip()
        if not pid or not name or not url or pid in seen:
            continue
        seen.add(pid)
        images = [str(p) for p in (item.get("images") or []) if isinstance(p, str) and p.strip()]
        try:
            revision = max(int(item.get("revision") or 1), 1)
        except (TypeError, ValueError):
            revision = 1
        plugins.append({
            "id": pid,
            "name": name,
            "short_description": str(item.get("short_description") or "").strip(),
            "description": str(item.get("description") or "").strip(),
            "download_url": url,
            "filename": str(item.get("filename") or "").strip(),
            "images": images,
            "thumb": images[0] if images else "",
            "revision": revision,
            "updated": str(item.get("updated_at") or item.get("updated") or ""),
            "published": item.get("published", True) is not False,
        })
    return plugins


def load_cached_catalog():
    data = load_json(catalog_cache_path(), {})
    if not isinstance(data, dict):
        return [], 0
    return parse_catalog(data.get("plugins")), float(data.get("fetched") or 0)


def save_cached_catalog(rows):
    save_json(catalog_cache_path(), {"fetched": time.time(), "plugins": rows})


def clear_cache():
    shutil.rmtree(cache_dir(), ignore_errors=True)
    os.makedirs(cache_dir(), exist_ok=True)


def cache_size_bytes():
    total = 0
    for root, _dirs, files in os.walk(cache_dir()):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def prepare_image_for_upload(path, max_side=1920):
    """Shrinks big pictures. Returns (bytes, extension, content type)."""
    if Image is None:
        raise ApiError("Image support (Pillow) is missing in this build.")
    img = Image.open(path)
    img.load()
    img = ImageOps.exif_transpose(img)
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    out = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.convert("RGBA").save(out, format="PNG", optimize=True)
        return out.getvalue(), "png", "image/png"
    img.convert("RGB").save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue(), "jpg", "image/jpeg"


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

def normalize_download_url(url):
    url = (url or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    query = urllib.parse.parse_qs(parsed.query)
    if host.endswith("dropbox.com"):
        query["dl"] = ["1"]
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query, doseq=True)))
    if host.endswith("drive.google.com"):
        m = re.search(r"/file/d/([A-Za-z0-9_-]+)", parsed.path)
        file_id = m.group(1) if m else (query.get("id") or [""])[0]
        if file_id:
            return f"https://drive.google.com/uc?export=download&id={file_id}"
    if host == "github.com" and "/blob/" in parsed.path:
        return urllib.parse.urlunparse(parsed._replace(
            netloc="raw.githubusercontent.com", path=parsed.path.replace("/blob/", "/", 1), query=""))
    return url


def find_direct_link(page_url, html):
    if "mediafire.com" in urllib.parse.urlparse(page_url).netloc.lower():
        m = re.search(r'href="(https?://download\d*\.mediafire\.com/[^"]+)"', html)
        if not m:
            m = re.search(r"(https?://download\d*\.mediafire\.com/[^\s\"'<>]+)", html)
        if m:
            return m.group(1).replace("&amp;", "&")
    return None


def open_download(url, timeout=20):
    url = normalize_download_url(url)
    if not url.lower().startswith(("http://", "https://")):
        raise DownloadError("The download link must start with http:// or https://")
    for attempt in range(2):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        response = urllib.request.urlopen(request, timeout=timeout)
        if "text/html" not in (response.headers.get("Content-Type") or "").lower():
            return response
        html = response.read(3_000_000).decode("utf-8", "replace")
        response.close()
        direct = find_direct_link(url, html)
        if not direct or attempt == 1:
            break
        url = direct
    raise DownloadError("This link opens a web page instead of the plugin file. "
                        "The plugin needs a direct download link.")


def download_file(url, dest_path, progress=None):
    """Downloads to a temp file, checks it is a Roblox model, then moves it into place."""
    try:
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    except OSError:
        raise DownloadError(f"Can't create the folder {os.path.dirname(dest_path)}. "
                            "Allow storage access in Settings.")
    part_path = dest_path + ".part"
    try:
        response = open_download(url)
    except DownloadError:
        raise
    except urllib.error.HTTPError as e:
        raise DownloadError(f"The download server answered with HTTP {e.code}.")
    except (urllib.error.URLError, socket.timeout, OSError):
        raise DownloadError("Can't reach the download server - check your internet connection.")

    total = int(response.headers.get("Content-Length") or 0)
    done, ok = 0, False
    try:
        with response, open(part_path, "wb") as out:
            first = True
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                if first:
                    first = False
                    if b"<roblox" not in chunk[:1024]:
                        raise DownloadError("The downloaded file is not a Roblox plugin (.rbxm).")
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if done == 0:
            raise DownloadError("The server returned an empty file.")
        os.replace(part_path, dest_path)
        ok = True
    except DownloadError:
        raise
    except (OSError, socket.timeout) as e:
        raise DownloadError(f"Download failed: {e}")
    finally:
        if not ok:
            try:
                os.remove(part_path)
            except OSError:
                pass
    return dest_path


def check_download_link(url):
    """Admin helper: does this link serve a Roblox plugin? Returns (ok, message)."""
    try:
        response = open_download(url)
    except DownloadError as e:
        return False, str(e)
    except urllib.error.HTTPError as e:
        return False, f"The server answered with HTTP {e.code}."
    except (urllib.error.URLError, socket.timeout, OSError):
        return False, "Can't reach that server."
    try:
        with response:
            size = int(response.headers.get("Content-Length") or 0)
            head = response.read(1024)
    except (OSError, socket.timeout) as e:
        return False, f"Reading the file failed: {e}"
    if b"<roblox" not in head:
        return False, "The link works, but the file is not a Roblox plugin (.rbxm)."
    return True, "Valid Roblox plugin" + (f" · {format_size(size)}" if size else "")


def plugin_filename(plugin):
    name = (plugin.get("filename") or "").strip()
    if not name:
        path = urllib.parse.urlparse(plugin.get("download_url", "")).path
        name = urllib.parse.unquote(os.path.basename(path))
    stem, ext = os.path.splitext(name)
    if ext.lower() not in (".rbxm", ".rbxmx"):
        stem, ext = (name if name else plugin.get("name", "plugin")), ".rbxm"
    return sanitize_filename(stem) + ext.lower()


# ---------------------------------------------------------------------------
# Plugins folder + registry of what the app installed
# ---------------------------------------------------------------------------

def load_installed():
    data = load_json(installed_registry_path(), {})
    return data if isinstance(data, dict) else {}


def save_installed(data):
    return save_json(installed_registry_path(), data)


def scan_plugins_folder():
    items = []
    try:
        with os.scandir(PLUGINS_DIR) as it:
            for entry in it:
                try:
                    if not entry.is_file() or not entry.name.lower().endswith(PLUGIN_EXTENSIONS):
                        continue
                    stat = entry.stat()
                except OSError:
                    continue
                items.append({"filename": entry.name, "path": entry.path,
                              "size": stat.st_size, "mtime": stat.st_mtime})
    except OSError:
        pass
    items.sort(key=lambda item: item["filename"].lower())
    return items


# ---------------------------------------------------------------------------
# Export (custom source folders -> "exported plugins")
# ---------------------------------------------------------------------------

def calculate_file_hash(file_path, chunk_size=1024 * 1024):
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_index():
    data = load_json(export_index_path(), {})
    return data if isinstance(data, dict) else {}


def save_index(index):
    return save_json(export_index_path(), index)


def exported_entries(index=None):
    index = load_index() if index is None else index
    return {k: v for k, v in index.items() if isinstance(v, dict) and v.get("filename")}


def resolve_unique_filename(base_name, plugin_id, index):
    candidate = f"{base_name}.rbxm"
    for other_id, meta in exported_entries(index).items():
        if other_id != plugin_id and meta.get("filename") == candidate:
            return f"{base_name} ({plugin_id}).rbxm"
    return candidate


def get_plugin_name(plugin_id, max_retries=3):
    """Real title of a Roblox plugin id; non-numeric ids (file names) keep their name."""
    if not str(plugin_id).isdigit():
        return sanitize_filename(plugin_id)
    url = f"https://economy.roblox.com/v2/assets/{plugin_id}/details"
    for attempt in range(max_retries):
        try:
            data = json.loads(http_get(url, timeout=10).decode("utf-8"))
            name = data.get("Name")
            return sanitize_filename(name) if name else plugin_id
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < max_retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return plugin_id
        except Exception:
            return plugin_id
    return plugin_id


def get_plugin_names(plugin_ids, max_workers=4):
    ids_list = list(plugin_ids)
    names = {}
    if not ids_list:
        return names
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_id = {executor.submit(get_plugin_name, pid): pid for pid in ids_list}
        for future in concurrent.futures.as_completed(future_to_id):
            pid = future_to_id[future]
            try:
                names[pid] = future.result()
            except Exception:
                names[pid] = pid
    return names


def find_rbxm_in_folder(entry_path):
    for root, _dirs, files in os.walk(entry_path):
        for file in files:
            if file.lower().endswith(".rbxm"):
                return os.path.join(root, file)
    return None


def scan_source_folder(source_folder, log=lambda msg: None, depth=0):
    """Yields (plugin_id, rbxm_path). A folder named with a number is a plugin id,
    a .rbxm file uses its file name as the id, other folders are searched."""
    if depth > MAX_SCAN_DEPTH:
        log(f"  (stopped: '{source_folder}' is nested too deep)")
        return
    try:
        with os.scandir(source_folder) as it:
            entries = list(it)
    except OSError as e:
        log(f"  (couldn't read '{source_folder}': {e})")
        return
    for entry in entries:
        name = entry.name
        if name == "0" or name.startswith("."):
            continue
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        if is_dir:
            if name.isdigit():
                rbxm_path = find_rbxm_in_folder(entry.path)
                if rbxm_path:
                    yield name, rbxm_path
            else:
                yield from scan_source_folder(entry.path, log=log, depth=depth + 1)
        elif name.lower().endswith(".rbxm"):
            yield os.path.splitext(name)[0], entry.path


def export_plugins(source_folders, log=lambda msg: None, progress_callback=None):
    """Copies plugins from the given custom folders into EXPORT_DIR.
    Returns (copied, updated), or None if nothing could be exported."""
    if isinstance(source_folders, str):
        source_folders = [source_folders]

    existing = [f for f in source_folders if os.path.isdir(f)]
    for f in source_folders:
        if f not in existing:
            log(f"Error: source folder does not exist: {f}")
    if not existing:
        return None

    try:
        os.makedirs(EXPORT_DIR, exist_ok=True)  # created on first export
    except OSError as e:
        log(f"Can't create {EXPORT_DIR}: {e}\nAllow storage access in Settings.")
        return None

    index = load_index()
    copied_count = updated_count = 0
    try:
        plugin_files, ids_needing_names, seen_ids = {}, [], set()

        for folder in existing:
            log(f"Scanning: {folder}")
            count_here = 0
            for plugin_id, rbxm_path in scan_source_folder(folder, log=log):
                count_here += 1
                if plugin_id in seen_ids:
                    continue
                seen_ids.add(plugin_id)
                try:
                    st = os.stat(rbxm_path)
                except OSError:
                    continue
                size, mtime = st.st_size, int(st.st_mtime)
                previous = index.get(plugin_id)
                unchanged = previous and previous.get("size") == size and previous.get("mtime") == mtime
                still_there = (previous and previous.get("filename")
                               and os.path.exists(os.path.join(EXPORT_DIR, previous["filename"])))
                if unchanged and still_there:
                    continue
                plugin_files[plugin_id] = (rbxm_path, calculate_file_hash(rbxm_path), size, mtime)
                if not (previous and previous.get("filename")):
                    ids_needing_names.append(plugin_id)
            log(f"  found {count_here} plugin(s) here")

        log(f"\n{len(seen_ids)} unique plugin(s) found, {len(plugin_files)} to copy or update.\n")

        resolved = {}
        if ids_needing_names:
            if has_internet_connection():
                log(f"Looking up {len(ids_needing_names)} plugin name(s) on Roblox...")
                resolved = get_plugin_names(ids_needing_names)
            else:
                log("No internet - files will be named by their ID this run.")

        total, processed = len(plugin_files), 0
        for plugin_id, (src, src_hash, size, mtime) in plugin_files.items():
            previous = index.get(plugin_id)
            if previous and previous.get("filename"):
                filename = previous["filename"]
            else:
                filename = resolve_unique_filename(resolved.get(plugin_id, plugin_id), plugin_id, index)
            processed += 1
            try:
                shutil.copy2(src, os.path.join(EXPORT_DIR, filename))
            except OSError as e:
                log(f"Skipped {plugin_id} ({filename!r}): copy failed - {e}")
                if progress_callback:
                    progress_callback(processed, total)
                continue
            if previous is None:
                log(f"Copied new: {filename}")
                copied_count += 1
            else:
                log(f"Updated: {filename}")
                updated_count += 1
            index[plugin_id] = {"filename": filename, "hash": src_hash, "size": size, "mtime": mtime}
            if progress_callback:
                progress_callback(processed, total)

        if copied_count or updated_count:
            log(f"\nDone! Added {copied_count} new, updated {updated_count}.")
        else:
            log("\nNo new or changed files. Everything is already exported.")
        return copied_count, updated_count
    except Exception as e:
        log(f"An error occurred: {e}")
        return None
    finally:
        history = index.get("_history", [])
        if not isinstance(history, list):
            history = []
        history.append({"timestamp": time.time(), "copied": copied_count, "updated": updated_count})
        index["_history"] = history[-10:]
        index["_last_run"] = time.time()
        save_index(index)
