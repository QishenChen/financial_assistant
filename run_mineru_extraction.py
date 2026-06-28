#!/usr/bin/env python3
"""
MinerU Batch Extraction Script (v4)
- Saves batch_ids to state for crash recovery
- Falls back to single-file submission when batch quota exhausted
- Preserves images alongside markdown output
- Splits PDFs > 200 pages into chunks and integrates after extraction
- Merges chunk _middle.json files with corrected page offsets
"""

import os, sys, json, time, requests, shutil, zipfile, traceback, subprocess
from pathlib import Path

# Force all print() calls to flush immediately so logs are visible in real time
# when the script is run as a background subprocess.
_original_print = print

def _flush_print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    return _original_print(*args, **kwargs)

print = _flush_print

TOKEN= "eyJ0eXBlIjoiSldUIiwiYWxnIjoiSFM1MTIifQ.eyJqdGkiOiIzMjUwMDc4MiIsInJvbCI6IlJPTEVfUkVHSVNURVIiLCJpc3MiOiJPcGVuWExhYiIsImlhdCI6MTc4MTQyMzQ5MiwiY2xpZW50SWQiOiJsa3pkeDU3bnZ5MjJqa3BxOXgydyIsInBob25lIjoiIiwib3BlbklkIjpudWxsLCJ1dWlkIjoiYjBlNjIyMjQtOTMyMS00M2Q2LWEwOWMtNDgzZjg4ZDVkYzAyIiwiZW1haWwiOiIiLCJleHAiOjE3ODkxOTk0OTJ9.WXbXiLZbqqeFZnkQORL24AsBelPluoc7YZLUAGL51Ep93KrzLGVWRvAJYYftl4ux7QQiFZxmZnOfX5t6cO3zaw"
BASE_URL = "https://mineru.net/api/v4"
HEADERS = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
SOURCE_DIR = "public_dataset_upload/raw"
OUTPUT_DIR = "public_dataset_upload/extracted"
STATE_FILE = "mineru_batch_state.json"
BATCH_SIZE = 20
POLL_INTERVAL = 10
MAX_WAIT = 900
OVER_PAGE_LIMIT = set()
PAGE_LIMIT = 200
SPLIT_TMP_DIR = "/tmp/mineru_splits"

def get_page_count(pdf_path):
    try:
        result = subprocess.run(['pdfinfo', pdf_path], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            for line in result.stdout.split('\n'):
                if 'Pages' in line:
                    return int(line.split(':')[1].strip())
    except: pass
    try:
        from PyPDF2 import PdfReader
        return len(PdfReader(pdf_path).pages)
    except: pass
    try:
        from pypdf import PdfReader
        return len(PdfReader(pdf_path).pages)
    except: pass
    return None

def split_pdf(pdf_path, max_pages=PAGE_LIMIT, tmp_dir=SPLIT_TMP_DIR):
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        try:
            from PyPDF2 import PdfReader, PdfWriter
        except ImportError:
            print(f"    ✗ Cannot split PDF: pypdf/PyPDF2 not available")
            return None
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        reader = PdfReader(pdf_path)
    except Exception as e:
        print(f"    ✗ Cannot read PDF for splitting: {pdf_path} — {e}")
        return None
    total_pages = len(reader.pages)
    if total_pages <= max_pages:
        return None
    chunks = []
    base_name = os.path.splitext(os.path.basename(pdf_path))[0]
    for start in range(0, total_pages, max_pages):
        end = min(start + max_pages, total_pages)
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        chunk_name = f"{base_name}_p{start+1}-{end}"
        chunk_path = os.path.join(tmp_dir, f"{chunk_name}.pdf")
        with open(chunk_path, 'wb') as f:
            writer.write(f)
        chunks.append({'abs_path': chunk_path, 'name': f"{chunk_name}.pdf", 'start_page': start + 1, 'end_page': end})
    print(f"  → Split {os.path.basename(pdf_path)} ({total_pages} pages) into {len(chunks)} chunks")
    return chunks

def collect_files():
    files = []
    split_map = {}
    for root, dirs, filenames in os.walk(SOURCE_DIR):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ('.pdf', '.html'): continue
            abs_path = os.path.join(root, fname)
            rel_path = os.path.relpath(abs_path, SOURCE_DIR)
            ftype = ext.lstrip('.')
            if ftype == 'pdf':
                pages = get_page_count(abs_path)
                if pages and pages > PAGE_LIMIT:
                    orig_base = rel_path.rsplit('.', 1)[0]
                    if os.path.exists(os.path.join(OUTPUT_DIR, orig_base + '.md')): continue
                    chunks = split_pdf(abs_path, PAGE_LIMIT)
                    if chunks:
                        chunk_rel_paths = []
                        for i, chunk in enumerate(chunks):
                            orig_base = rel_path.rsplit('.', 1)[0]
                            chunk_rel = f"{orig_base}_chunk_{chunk['start_page']}-{chunk['end_page']}.pdf"
                            chunk['rel_path'] = chunk_rel
                            chunk['type'] = 'pdf'
                            files.append(chunk)
                            chunk_rel_paths.append(chunk_rel)
                        split_map[rel_path] = chunk_rel_paths
                        continue
                    else:
                        OVER_PAGE_LIMIT.add(rel_path)
                        print(f"  ⚠ Skipping {rel_path} ({pages} pages) — split failed")
                        continue
            files.append({'abs_path': abs_path, 'rel_path': rel_path, 'name': fname, 'type': ftype})
    files.sort(key=lambda x: x['rel_path'])
    return files, split_map

def integrate_split_chunks(original_rel_path, chunk_rel_paths):
    original_base = original_rel_path.rsplit('.', 1)[0]
    merged_md_path = os.path.join(OUTPUT_DIR, original_base + '.md')
    merged_img_dir = os.path.join(OUTPUT_DIR, original_base, 'images')
    combined_md = []
    any_success = False

    for chunk_rp in chunk_rel_paths:
        chunk_base = chunk_rp.rsplit('.', 1)[0]
        chunk_md = os.path.join(OUTPUT_DIR, chunk_base + '.md')
        if os.path.exists(chunk_md):
            with open(chunk_md, 'r', encoding='utf-8') as f:
                content = f.read()
            chunk_label = os.path.splitext(os.path.basename(chunk_rp))[0]
            combined_md.append(f"<!-- Chunk: {chunk_label} -->\n\n{content}")
            any_success = True
        chunk_img_dir = os.path.join(OUTPUT_DIR, chunk_base, 'images')
        if os.path.exists(chunk_img_dir) and os.path.isdir(chunk_img_dir):
            os.makedirs(merged_img_dir, exist_ok=True)
            for img_name in os.listdir(chunk_img_dir):
                src = os.path.join(chunk_img_dir, img_name)
                dst = os.path.join(merged_img_dir, img_name)
                if os.path.isfile(src) and not os.path.exists(dst):
                    shutil.copy2(src, dst)

    if any_success:
        os.makedirs(os.path.dirname(merged_md_path), exist_ok=True)
        with open(merged_md_path, 'w', encoding='utf-8') as f:
            f.write('\n\n'.join(combined_md))
        img_count = sum(1 for _ in Path(merged_img_dir).rglob('*')) if os.path.exists(merged_img_dir) else 0
        print(f"  ✓ Integrated: {original_base}.md from {len(chunk_rel_paths)} chunks ({img_count} images)")

        # Merge _middle.json files with corrected page offsets
        merged_middle_path = os.path.join(OUTPUT_DIR, original_base + '_middle.json')
        combined_middle_pages = []
        global_page_offset = 0
        for chunk_rp in chunk_rel_paths:
            chunk_base = chunk_rp.rsplit('.', 1)[0]
            chunk_middle = os.path.join(OUTPUT_DIR, chunk_base + '_middle.json')
            if os.path.exists(chunk_middle):
                try:
                    with open(chunk_middle, 'r', encoding='utf-8') as f:
                        middle_data = json.load(f)
                    chunk_pages = middle_data.get("pdf_info", [])
                    for page in chunk_pages:
                        page["page_idx"] = page.get("page_idx", 0) + global_page_offset
                        combined_middle_pages.append(page)
                    if chunk_pages:
                        global_page_offset += len(chunk_pages)
                except Exception as e:
                    print(f"    ⚠ Failed to merge middle.json for {chunk_rp}: {e}")
        if combined_middle_pages:
            merged_middle = {"pdf_info": combined_middle_pages, "_backend": "pipeline", "_version_name": "merged"}
            os.makedirs(os.path.dirname(merged_middle_path), exist_ok=True)
            with open(merged_middle_path, 'w', encoding='utf-8') as f:
                json.dump(merged_middle, f, ensure_ascii=False)
            print(f"    ✓ Integrated: {original_base}_middle.json ({len(combined_middle_pages)} pages)")
    else:
        print(f"  ⚠ Integration: no successful chunks for {original_rel_path}")

    # Clean up
    for chunk_rp in chunk_rel_paths:
        chunk_base = chunk_rp.rsplit('.', 1)[0]
        for p in [os.path.join(OUTPUT_DIR, chunk_base + '.md'), os.path.join(OUTPUT_DIR, chunk_base),
                  os.path.join(OUTPUT_DIR, chunk_base + '_middle.json'), os.path.join(OUTPUT_DIR, chunk_base + '_model.json'),
                  os.path.join(OUTPUT_DIR, chunk_base + '_layout.json')]:
            if os.path.isfile(p): os.remove(p)
            elif os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
    for chunk_rp in chunk_rel_paths:
        tmp_pdf = os.path.join(SPLIT_TMP_DIR, os.path.basename(chunk_rp))
        if os.path.exists(tmp_pdf): os.remove(tmp_pdf)
    return any_success

def submit_batch(files_batch, model_version):
    if not files_batch: return None, None
    payload = {"files": [{"name": f['name'], "data_id": f['rel_path']} for f in files_batch], "model_version": model_version, "enable_table": True, "enable_formula": True, "language": "ch"}
    print(f"  Submitting batch for: {[f['rel_path'] for f in files_batch]}")
    try:
        resp = requests.post(f"{BASE_URL}/file-urls/batch", headers=HEADERS, json=payload, timeout=(10, 30))
        if resp.status_code == 429:
            print("    ⚠ Submit rate-limited (429)")
            return None, None
        resp.raise_for_status()
        result = resp.json()
    except requests.exceptions.Timeout as e:
        print(f"    ✗ Submit timeout: {e}")
        return None, None
    except requests.exceptions.RequestException as e:
        print(f"    ✗ Submit request failed: {e}")
        return None, None
    if result.get("code") != 0:
        print(f"    ✗ Submit API error: {result.get('msg', result)}")
        return None, None
    print(f"    ✓ Got batch_id: {result['data']['batch_id']}")
    return result["data"]["batch_id"], result["data"]["file_urls"]

def upload_files(batch_id, file_urls, files_batch, max_retries=2):
    print(f"  Batch {batch_id}: uploading {len(file_urls)} files...")
    failed = []
    for i, f in enumerate(files_batch):
        success = False
        last_err = None
        for attempt in range(1, max_retries + 2):
            try:
                with open(f['abs_path'], 'rb') as fh:
                    print(f"    Uploading {f['rel_path']} (attempt {attempt}) ...")
                    put_resp = requests.put(file_urls[i], data=fh, timeout=(10, 60))
                    if put_resp.ok:
                        print(f"    ✓ Uploaded {f['rel_path']}")
                        success = True
                        break
                    else:
                        last_err = f"HTTP {put_resp.status_code}"
                        print(f"    ✗ Upload HTTP {put_resp.status_code} for {f['rel_path']} (attempt {attempt})")
            except requests.exceptions.Timeout as e:
                last_err = f"timeout ({e})"
                print(f"    ✗ Upload timeout for {f['rel_path']} (attempt {attempt})")
            except Exception as e:
                last_err = str(e)
                print(f"    ✗ Upload failed for {f['rel_path']} (attempt {attempt}): {e}")
            if attempt <= max_retries:
                time.sleep(5 * attempt)
        if not success:
            print(f"    ✗ Gave up uploading {f['rel_path']} after {max_retries + 1} attempts: {last_err}")
            failed.append(f)
    return failed

def poll_batch(batch_id):
    start = time.time()
    print(f"  Polling batch {batch_id} ...")
    while True:
        if time.time() - start > MAX_WAIT:
            print(f"  Batch {batch_id}: TIMEOUT after {MAX_WAIT}s")
            return None
        try:
            resp = requests.get(f"{BASE_URL}/extract-results/batch/{batch_id}", headers=HEADERS, timeout=(10, 30))
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.Timeout as e:
            print(f"  Batch {batch_id}: poll timeout ({e})")
            time.sleep(POLL_INTERVAL)
            continue
        except Exception as e:
            print(f"  Batch {batch_id}: poll error ({e})")
            time.sleep(POLL_INTERVAL)
            continue
        if result.get("code") != 0:
            print(f"  Batch {batch_id}: API error — {result.get('msg', result)}")
            time.sleep(POLL_INTERVAL)
            continue
        extract_results = result["data"].get("extract_result", [])
        if not extract_results: time.sleep(POLL_INTERVAL); continue
        items = []
        for er in extract_results:
            if isinstance(er, str): continue
            items.append({"rel_path": er.get("data_id", er.get("file_name", "unknown")), "state": er.get("state", "unknown"), "zip_url": er.get("full_zip_url", ""), "err_msg": er.get("err_msg", "")})
        states = {}
        for item in items: states[item["state"]] = states.get(item["state"], 0) + 1
        done, failed, pending = states.get("done", 0), states.get("failed", 0), sum(v for k, v in states.items() if k not in ("done", "failed"))
        print(f"  Batch {batch_id}: {done}✓ {failed}✗ {pending}… ({time.time()-start:.0f}s)")
        if pending == 0: return items
        time.sleep(POLL_INTERVAL)

def save_file(zip_url, rel_path):
    base = rel_path.rsplit('.', 1)[0]
    md_out = os.path.join(OUTPUT_DIR, base + '.md')
    if os.path.exists(md_out):
        print(f"    ✓ {base}.md already exists")
        return True
    print(f"    Downloading result for {rel_path} ...")
    try:
        resp = requests.get(zip_url, timeout=(10, 60))
        if resp.status_code == 403:
            print(f"    ⚠ Result URL expired for {rel_path}")
            return 'expired'
        if resp.status_code != 200:
            print(f"    ✗ Download HTTP {resp.status_code} for {rel_path}")
            return False
    except requests.exceptions.Timeout as e:
        print(f"    ✗ Download timeout for {rel_path}: {e}")
        return False
    except Exception as e:
        print(f"    ✗ Download failed for {rel_path}: {e}")
        return False
    tmp_zip = f"/tmp/mineru_{os.path.basename(rel_path)}.zip"
    tmp_dir = f"/tmp/mineru_ext_{os.path.basename(rel_path)}"
    try:
        with open(tmp_zip, 'wb') as f: f.write(resp.content)
        os.makedirs(tmp_dir, exist_ok=True)
        with zipfile.ZipFile(tmp_zip, 'r') as zf: zf.extractall(tmp_dir)
        md_files = list(Path(tmp_dir).rglob("full.md"))
        md_out = os.path.join(OUTPUT_DIR, base + '.md')
        os.makedirs(os.path.dirname(md_out), exist_ok=True)
        if md_files:
            shutil.copy(str(md_files[0]), md_out)
            print(f"    ✓ {base}.md")
        else:
            any_md = list(Path(tmp_dir).rglob("*.md"))
            if any_md:
                combined = []
                for mdf in any_md:
                    with open(mdf, 'r', encoding='utf-8') as f:
                        combined.append(f.read())
                with open(md_out, 'w', encoding='utf-8') as f:
                    f.write('\n\n'.join(combined))
                print(f"    ✓ {base}.md (combined)")
        # Save layout.json as _middle.json
        layout_files = [f for f in Path(tmp_dir).rglob("layout.json") if f.name == "layout.json"]
        if not layout_files:
            layout_files = [f for f in Path(tmp_dir).rglob("middle.json") if f.name == "middle.json"]
            layout_files += [f for f in Path(tmp_dir).rglob("*_middle.json") if "_middle.json" in f.name and f.suffix == ".json"]
            layout_files += [f for f in Path(tmp_dir).rglob("*.json") if "middle" in f.name.lower()]
        if layout_files:
            middle_out = os.path.join(OUTPUT_DIR, base + '_middle.json')
            os.makedirs(os.path.dirname(middle_out), exist_ok=True)
            shutil.copy(str(layout_files[0]), middle_out)
            print(f"    ✓ {base}_middle.json (from {os.path.basename(layout_files[0])})")
        else:
            all_json = list(Path(tmp_dir).rglob("*.json"))
            if all_json:
                names = [str(f.relative_to(tmp_dir)) for f in all_json[:5]]
                print(f"    ⚠ No layout/middle.json, available JSON: {names}")
        # Save model.json
        model_files = [f for f in Path(tmp_dir).rglob("model.json") if f.name == "model.json"]
        model_files += [f for f in Path(tmp_dir).rglob("*_model.json") if "_model.json" in f.name]
        if model_files:
            model_out = os.path.join(OUTPUT_DIR, base + '_model.json')
            os.makedirs(os.path.dirname(model_out), exist_ok=True)
            shutil.copy(str(model_files[0]), model_out)
            print(f"    ✓ {base}_model.json")
        # Save images
        for img_dir in [d for d in Path(tmp_dir).rglob("images") if d.is_dir()]:
            dest = os.path.join(OUTPUT_DIR, base, "images")
            os.makedirs(dest, exist_ok=True)
            for f in img_dir.iterdir():
                if f.is_file(): shutil.copy2(str(f), os.path.join(dest, f.name))
            n = sum(1 for _ in img_dir.iterdir() if _.is_file())
            if n: print(f"    ✓ {n} images → {base}/images/")
        return True
    except Exception as e:
        print(f"    ✗ Extract: {rel_path} — {e}")
        return False
    finally:
        for p in [tmp_zip, tmp_dir]:
            if os.path.exists(p): (os.remove if os.path.isfile(p) else shutil.rmtree)(p)

def process_item(item, completed, failed, pending_batches, batch_id):
    rp = item["rel_path"]
    if item["state"] == "done":
        if item["zip_url"]:
            result = save_file(item["zip_url"], rp)
            if result == 'expired': return 'expired'
            elif result: completed.add(rp)
            else: failed.add(rp)
        else: failed.add(rp)
        return True
    elif item["state"] == "failed":
        err = item.get("err_msg", "")
        if rp.endswith('.pdf'):
            pages = get_page_count(os.path.join(SOURCE_DIR, rp))
            if pages and pages > PAGE_LIMIT:
                print(f"    ✗ FAILED: {rp} — {pages} pages (exceeds {PAGE_LIMIT}-page limit)")
            else: print(f"    ✗ FAILED: {rp} — {err}")
        else: print(f"    ✗ FAILED: {rp} — {err}")
        if "retry limit" in err.lower() or "pages exceeds" in err.lower(): OVER_PAGE_LIMIT.add(rp)
        failed.add(rp)
        return True

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: return json.load(f)
    return {"completed": [], "failed": [], "pending_batches": {}, "split_map": {}}

def save_state(completed, failed, pending_batches, split_map=None):
    state = {"completed": sorted(completed), "failed": sorted(failed | OVER_PAGE_LIMIT), "pending_batches": pending_batches}
    if split_map: state["split_map"] = split_map
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=2)

def process_single_files(files_batch, model_version, completed, failed, pending_batches):
    for f in files_batch:
        rp = f['rel_path']
        if rp in completed or rp in failed or rp in OVER_PAGE_LIMIT: continue
        print(f"  Single: {rp}")
        bid, urls = submit_batch([f], model_version)
        if bid:
            failed_uploads = upload_files(bid, urls, [f])
            if failed_uploads:
                print(f"    ✗ Upload failed for {rp}; will retry later")
                failed.add(rp)
                continue
            pending_batches[bid] = [f['rel_path']]
            save_state(completed, failed, pending_batches)
        else:
            print(f"    ✗ Submit failed")
            failed.add(rp)
            continue
        time.sleep(2)
        items = poll_batch(bid)
        if items:
            for item in items: process_item(item, completed, failed, pending_batches, bid)
            if bid in pending_batches: del pending_batches[bid]
        else:
            print(f"    ⚠ Batch {bid} timed out; abandoning to retry later")
            if bid in pending_batches: del pending_batches[bid]
        save_state(completed, failed, pending_batches)
    return None

def check_and_integrate_splits(completed, failed, split_map):
    integrated = set()
    for orig_rel_path, chunk_rel_paths in list(split_map.items()):
        all_chunks_done = all(crp in completed or crp in failed for crp in chunk_rel_paths)
        if all_chunks_done:
            successful_chunks = [crp for crp in chunk_rel_paths if crp in completed]
            if successful_chunks:
                integrate_split_chunks(orig_rel_path, successful_chunks)
                completed.add(orig_rel_path)
                for crp in chunk_rel_paths: completed.discard(crp); failed.discard(crp)
                integrated.add(orig_rel_path)
            else:
                failed.add(orig_rel_path)
                for crp in chunk_rel_paths: failed.discard(crp)
                integrated.add(orig_rel_path)
    for k in integrated: del split_map[k]
    return integrated

def main():
    all_files, split_map = collect_files()
    print(f"Found {len(all_files)} files: {sum(1 for f in all_files if f['type']=='pdf')} PDFs + {sum(1 for f in all_files if f['type']=='html')} HTMLs")
    if split_map: print(f"  📄 {len(split_map)} oversized PDF(s) split into chunks for processing")
    state = load_state()
    completed = set(state.get("completed", []))
    failed = set(state.get("failed", []))
    pending_batches = state.get("pending_batches", {})
    saved_split_map = state.get("split_map", {})
    for k, v in split_map.items():
        if k not in saved_split_map: saved_split_map[k] = v
    split_map = saved_split_map
    all_chunk_set = set()
    for chunk_list in split_map.values(): all_chunk_set.update(chunk_list)
    for f in all_files:
        if f['type'] == 'pdf' and f['rel_path'] not in all_chunk_set:
            pages = get_page_count(f['abs_path'])
            if pages and pages > PAGE_LIMIT and f['rel_path'] not in split_map:
                OVER_PAGE_LIMIT.add(f['rel_path']); failed.add(f['rel_path'])
    if pending_batches:
        print(f"\nRecovering {len(pending_batches)} pending batches...")
        for bid, fpaths in list(pending_batches.items()):
            print(f"  Polling batch {bid}...")
            items = poll_batch(bid)
            if items is None:
                print(f"  Batch {bid}: poll timed out; abandoning batch to retry later")
                del pending_batches[bid]
                save_state(completed, failed, pending_batches, split_map)
                continue
            for item in items: process_item(item, completed, failed, pending_batches, bid)
            del pending_batches[bid]
            save_state(completed, failed, pending_batches, split_map)
        check_and_integrate_splits(completed, failed, split_map)
    remaining = [f for f in all_files if f['rel_path'] not in completed and f['rel_path'] not in failed and f['rel_path'] not in OVER_PAGE_LIMIT]
    print(f"\nCompleted: {len(completed)}, Failed: {len(failed)}, Remaining: {len(remaining)}")
    if not remaining:
        check_and_integrate_splits(completed, failed, split_map)
        save_state(completed, failed, pending_batches, split_map)
        remaining = [f for f in all_files if f['rel_path'] not in completed and f['rel_path'] not in failed and f['rel_path'] not in OVER_PAGE_LIMIT]
        if not remaining: print("All done!"); return
    vlm = [f for f in remaining if f['type'] == 'pdf']
    html = [f for f in remaining if f['type'] == 'html']
    print(f"PDFs: {len(vlm)}, HTMLs: {len(html)}")
    for model, files in [("vlm", vlm), ("MinerU-HTML", html)]:
        if not files: continue
        for bi in range(0, len(files), BATCH_SIZE):
            batch = files[bi:bi + BATCH_SIZE]
            batch_files = [f for f in batch if f['rel_path'] not in completed and f['rel_path'] not in failed]
            if not batch_files: continue
            print(f"\nBatch {bi//BATCH_SIZE + 1} ({len(batch_files)} files, {model})")
            bid, urls = None, None
            for attempt in range(1, 4):
                bid, urls = submit_batch(batch_files, model)
                if bid: break
                if attempt < 3: time.sleep(5 * attempt)
            if bid:
                failed_uploads = upload_files(bid, urls, batch_files)
                if failed_uploads:
                    print(f"  Batch {bid}: {len(failed_uploads)} upload(s) failed; abandoning batch and retrying those files later")
                    for f in failed_uploads:
                        failed.add(f['rel_path'])
                else:
                    pending_batches[bid] = [f['rel_path'] for f in batch_files]
                    save_state(completed, failed, pending_batches, split_map)
                    items = poll_batch(bid)
                    if items:
                        expired = []
                        for item in items:
                            result = process_item(item, completed, failed, pending_batches, bid)
                            if result == 'expired': expired.append(item)
                        for repoll_count in range(1, 4):
                            if not expired: break
                            time.sleep(2)
                            fresh_items = poll_batch(bid)
                            if fresh_items:
                                fresh_map = {f['rel_path']: f for f in fresh_items}
                                still_expired = []
                                for ei in expired:
                                    fi = fresh_map.get(ei['rel_path'])
                                    if fi and fi.get('zip_url'):
                                        ei['zip_url'] = fi['zip_url']
                                        if process_item(ei, completed, failed, pending_batches, bid) == 'expired': still_expired.append(ei)
                                    else: failed.add(ei['rel_path'])
                                expired = still_expired
                        if bid in pending_batches: del pending_batches[bid]
                    else:
                        print(f"  ⚠ Batch {bid} timed out; abandoning batch to retry later")
                        if bid in pending_batches: del pending_batches[bid]
            else:
                process_single_files(batch_files, model, completed, failed, pending_batches)
            check_and_integrate_splits(completed, failed, split_map)
            save_state(completed, failed, pending_batches, split_map)
            time.sleep(3)
    check_and_integrate_splits(completed, failed, split_map)
    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    print(f"  Completed: {len(completed)}")
    print(f"  Failed permanently: {len(failed)}")
    pending_count = sum(len(v) for v in pending_batches.values())
    print(f"  Pending (orphaned): {pending_count}")
    md = sum(1 for _ in Path(OUTPUT_DIR).rglob("*.md"))
    print(f"  Output: {md} .md")
    if split_map: print(f"  ⚠ {len(split_map)} split PDF(s) still pending integration")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MinerU batch extraction")
    parser.add_argument("--source", default=SOURCE_DIR, help="Source directory with PDFs/HTMLs")
    parser.add_argument("--output", default=OUTPUT_DIR, help="Output directory for extracted markdown")
    parser.add_argument("--state", default=STATE_FILE, help="State file for batch recovery")
    args = parser.parse_args()

    SOURCE_DIR = args.source
    OUTPUT_DIR = args.output
    STATE_FILE = args.state

    main()