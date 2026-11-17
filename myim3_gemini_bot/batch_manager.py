import os
import json
import time
import datetime

BATCH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "saved_batches.json")
EXPIRY_SECONDS = 12 * 3600  # 12 hours TTL

def load_batches_raw() -> list:
    if not os.path.exists(BATCH_FILE):
        return []
    try:
        with open(BATCH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error loading batches: {e}")
        return []

def save_batches_raw(batches: list) -> bool:
    try:
        with open(BATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(batches, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"Error saving batches: {e}")
        return False

def cleanup_expired_batches() -> list:
    """
    Purges any batches older than 12 hours (expires_at <= current time).
    Also deletes any associated batch .txt files that have matured past 12h.
    """
    now = time.time()
    batches = load_batches_raw()
    valid_batches = []
    has_expired = False
    
    for b in batches:
        if b.get("expires_at", 0) > now:
            valid_batches.append(b)
        else:
            has_expired = True

    if has_expired or len(valid_batches) != len(batches):
        save_batches_raw(valid_batches)

    # Clean up old temporary batch .txt files
    dir_path = os.path.dirname(os.path.abspath(__file__))
    try:
        for fname in os.listdir(dir_path):
            if fname.startswith("gemini_batch_") and fname.endswith(".txt"):
                fpath = os.path.join(dir_path, fname)
                try:
                    if os.path.isfile(fpath) and (now - os.path.getmtime(fpath) > EXPIRY_SECONDS):
                        os.remove(fpath)
                except Exception:
                    pass
    except Exception:
        pass

    return valid_batches

def get_active_batches() -> list:
    """Returns active batches sorted newest first."""
    batches = cleanup_expired_batches()
    batches.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return batches

def get_batch_by_id(batch_id: str):
    batches = cleanup_expired_batches()
    for b in batches:
        if b.get("batch_id") == batch_id:
            return b
    return None

def create_batch(links: list, phones: list = None, label: str = "Auto-Claimer") -> dict:
    """
    Saves a new batch with a unique name, ID, and 12-hour expiration window.
    """
    valid_links = [l.strip() for l in links if l and l.strip()]
    if not valid_links:
        return None

    now = time.time()
    created_dt = datetime.datetime.fromtimestamp(now)
    time_code = created_dt.strftime("%H:%M")
    date_code = created_dt.strftime("%d-%b")
    unique_suffix = int(now * 1000) % 10000

    batch_id = f"B_{created_dt.strftime('%Y%m%d_%H%M%S')}_{unique_suffix}"
    count = len(valid_links)
    link_plural = "Link" if count == 1 else "Links"
    batch_name = f"Batch #{date_code} {time_code} ({label} • {count} {link_plural})"

    batch_obj = {
        "batch_id": batch_id,
        "batch_name": batch_name,
        "label": label,
        "links": valid_links,
        "phones": phones or [],
        "count": count,
        "created_at": now,
        "created_time_str": created_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "expires_at": now + EXPIRY_SECONDS,
        "expiry_time_str": datetime.datetime.fromtimestamp(now + EXPIRY_SECONDS).strftime("%Y-%m-%d %H:%M:%S")
    }

    batches = cleanup_expired_batches()
    batches.append(batch_obj)
    save_batches_raw(batches)
    return batch_obj

def get_all_active_links() -> list:
    batches = get_active_batches()
    seen = set()
    result = []
    for b in batches:
        for link in b.get("links", []):
            if link not in seen:
                seen.add(link)
                result.append(link)
    return result

