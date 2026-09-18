import os
import json
import requests
import time
from pathlib import Path
from typing import List, Dict, Set

# Load API key from environment variable ONLY
API_KEY = os.getenv('API_KEY')
if not API_KEY:
    print("❌ ERROR: API_KEY environment variable not set!")
    print("Set it with: export API_KEY='sk_cdn_...'")
    exit(1)

LOCAL_DIR = "./assets"
API_URL = "https://cdn.hackclub.com/api/v4"
MANIFEST_FILE = "cdn-manifest.json"
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

def get_files_to_upload(directory: str) -> Set[str]:
    """
    Collect files from Screenshots/ and Videos/ directories only,
    excluding any old/ subdirectories. Returns relative paths.
    """
    files = set()
    target_dirs = ['Screenshots', 'Videos']
    
    for root, dirs, filenames in os.walk(directory):
        # Skip if we're in an 'old' directory
        if 'old' in root.split(os.sep):
            continue
        
        # Only process Screenshots and Videos directories
        rel_path = os.path.relpath(root, directory)
        if rel_path == '.' or rel_path.split(os.sep)[0] in target_dirs:
            for filename in filenames:
                full_path = os.path.join(root, filename)
                relative = os.path.relpath(full_path, directory)
                files.add(relative)
    
    return files

def delete_file_from_cdn(file_id: str) -> bool:
    """Delete a single file from CDN by ID with retry logic."""
    headers = {
        'Authorization': f'Bearer {API_KEY}'
    }
    
    for attempt in range(MAX_RETRIES):
        try:
            response = requests.delete(
                f"{API_URL}/upload/{file_id}", 
                headers=headers,
                timeout=30
            )
            if response.status_code in [200, 204]:
                return True
            elif response.status_code == 404:
                # File already deleted or doesn't exist
                return True
            else:
                print(f"    ⚠️  Delete returned {response.status_code}: {response.text[:100]}")
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                print(f"    ⚠️  Retry {attempt + 1}/{MAX_RETRIES} (error: {str(e)[:50]})")
                time.sleep(RETRY_DELAY)
                continue
            else:
                print(f"    ❌ Failed to delete {file_id}: {e}")
                return False
    
    return False

def upload_batch(file_paths: List[str]) -> Dict:
    """Upload a batch of files (max 40 per API limit) with retry logic."""
    files = []
    file_handles = []
    
    for file_path in file_paths:
        try:
            f = open(file_path, 'rb')
            files.append(('files[]', f))
            file_handles.append(f)
        except Exception as e:
            print(f"  ⚠️  Failed to open {file_path}: {e}")
            continue

    headers = {
        'Authorization': f'Bearer {API_KEY}'
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(
                f"{API_URL}/uploads",
                files=files,
                headers=headers,
                timeout=60
            )
            result = response.json()
            
            # Close all file handles
            for f in file_handles:
                f.close()
            
            return result
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                print(f"  ⚠️  Retry {attempt + 1}/{MAX_RETRIES} (error: {str(e)[:50]})")
                time.sleep(RETRY_DELAY)
                # Recreate file handles for retry
                files = []
                file_handles = []
                for file_path in file_paths:
                    try:
                        f = open(file_path, 'rb')
                        files.append(('files[]', f))
                        file_handles.append(f)
                    except Exception as e:
                        print(f"  ⚠️  Failed to open {file_path}: {e}")
                        continue
            else:
                print(f"  ❌ Upload failed after {MAX_RETRIES} attempts: {e}")
                for f in file_handles:
                    f.close()
                return {}
    
    # Close all file handles
    for f in file_handles:
        f.close()
    
    return {}

def migrate_files():
    """Main migration function with cleanup."""
    print("🚀 Starting Hack Club CDN migration...\n")
    
    # Get current files to upload
    current_files = get_files_to_upload(LOCAL_DIR)
    total_files = len(current_files)
    
    if not current_files:
        print(f"❌ No files found in {LOCAL_DIR}/Screenshots/ or {LOCAL_DIR}/Videos/")
        return
    
    print(f"📂 Found {total_files} files to process\n")
    
    # Load existing manifest
    manifest = {}
    if os.path.exists(MANIFEST_FILE):
        try:
            with open(MANIFEST_FILE, 'r') as f:
                manifest = json.load(f)
            print(f"📄 Loaded manifest with {len(manifest)} existing entries\n")
        except Exception as e:
            print(f"⚠️  Failed to load manifest: {e}. Starting fresh.\n")
            manifest = {}
    
    # Find files to delete (in manifest but not in current_files)
    files_to_delete = set(manifest.keys()) - current_files
    deleted_count = 0
    
    if files_to_delete:
        print(f"🗑️  Cleaning up {len(files_to_delete)} removed/moved files...\n")
        
        for file_path in sorted(files_to_delete):
            file_id = manifest[file_path].get('id', 'unknown')
            if delete_file_from_cdn(file_id):
                print(f"  ✅ Deleted: {file_path}")
                del manifest[file_path]
                deleted_count += 1
            else:
                print(f"  ❌ Failed to delete: {file_path}")
        
        print(f"\n📊 Cleanup: {deleted_count}/{len(files_to_delete)} deleted\n")
    
    # Find new files to upload
    files_to_upload = current_files - set(manifest.keys())
    
    if not files_to_upload:
        print("✅ All files already uploaded. No new uploads needed.\n")
        with open(MANIFEST_FILE, 'w') as f:
            json.dump(manifest, f, indent=2)
        print("✨ Manifest saved.\n")
        return
    
    print(f"📤 Uploading {len(files_to_upload)} new files...\n")
    
    # Convert to list and sort for consistent batching
    files_to_upload_list = sorted(list(files_to_upload))
    
    # Upload in batches of 40
    batch_size = 40
    uploaded_count = 0
    failed_count = 0
    
    for i in range(0, len(files_to_upload_list), batch_size):
        batch = files_to_upload_list[i:i + batch_size]
        batch_num = (i // batch_size) + 1
        total_batches = (len(files_to_upload_list) + batch_size - 1) // batch_size
        
        print(f"📦 Batch {batch_num}/{total_batches}: Uploading {len(batch)} files...")
        
        # Convert relative paths to full paths for upload
        full_paths = [os.path.join(LOCAL_DIR, f) for f in batch]
        result = upload_batch(full_paths)
        
        if 'uploads' in result and result['uploads']:
            for upload in result['uploads']:
                relative_path = upload['filename']
                manifest[relative_path] = {
                    'id': upload['id'],
                    'url': upload['url'],
                    'size': upload['size'],
                    'content_type': upload['content_type'],
                    'created_at': upload['created_at'],
                }
                uploaded_count += 1
                print(f"  ✅ {relative_path}")
        
        if 'failed' in result and result['failed']:
            failed_count += len(result['failed'])
            for failed in result['failed']:
                filename = failed.get('filename', 'unknown')
                error = failed.get('error', 'unknown error')
                print(f"  ❌ {filename}: {error}")
        
        print()
    
    # Save updated manifest
    with open(MANIFEST_FILE, 'w') as f:
        json.dump(manifest, f, indent=2)
    
    print(f"\n{'='*60}")
    print(f"✨ Migration complete!")
    print(f"{'='*60}")
    print(f"📤 Uploaded: {uploaded_count}")
    print(f"🗑️  Deleted:  {deleted_count}")
    print(f"❌ Failed:   {failed_count}")
    print(f"📄 Manifest: {MANIFEST_FILE}")
    print(f"{'='*60}\n")

if __name__ == "__main__":
    migrate_files()

