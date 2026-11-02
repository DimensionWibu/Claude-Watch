"""Impor transkrip dari mesin lain ke Claude Watch: format Codex "rollout" (mis. fork Codex untuk DeepSeek).

Sumber diatur lokal di config.json (tidak masuk repo):
    "importSources": [{"name": "<nama>", "kind": "codex", "home": "<folder CODEX_HOME>"}]
Berkas yang dibaca: <home>/sessions/YYYY/MM/DD/rollout-*.jsonl dan <home>/session_index.jsonl (judul). Read-only.

Dua fungsi:
  * list_items()    -> sesi sumber untuk daftar Claude Watch (dilihat lewat RolloutTracker di server.py).
  * import_session()-> mengubah rollout menjadi transkrip Claude Code di <claudeHome>/projects/<folder>/<sid>.jsonl
                       agar bisa di-Resume/Fork oleh claude maupun clauseek. Konversi sengaja berbasis TEKS:
                       tool call/hasilnya dilipat menjadi ringkasan di pesan asisten (tanpa blok tool_use/thinking),
                       sehingga valid untuk API mana pun, dan hanya giliran terakhir yang dimuat (batas konteks).
Isi transkrip tidak tepercaya: ia hanya menjadi riwayat percakapan, tidak pernah dieksekusi di sini.
"""
import glob
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

import settings

TAIL_UUID = re.compile(r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\.jsonl$")
# pesan "user" yang sebenarnya injeksi sistem/alat, bukan ketikan pengguna
INJECTED = ("# AGENTS.md", "<environment_context>", "# Context", "<user_instructions>", "<INSTRUCTIONS>", "<permissions")
BUDGET_CHARS = 120_000       # total teks giliran yang diimpor (~30-40 ribu token)
TOOL_OUT_MAX = 1200          # batas per keluaran tool
TURN_MAX = 24_000            # batas teks asisten per giliran
VERSION = "2.1.316"
_meta_cache = {}             # path -> (mtime, cwd, model)


# ---------------------------------------------------------------- sumber & daftar
def sources():
    out = []
    for s in settings.load().get("importSources") or []:
        if not (isinstance(s, dict) and s.get("kind") == "codex" and s.get("name") and s.get("home")):
            continue
        home = str(s["home"])
        if os.path.isdir(os.path.join(home, "sessions")):
            name = re.sub(r"[^A-Za-z0-9_.-]", "", str(s["name"]))[:24] or "import"
            out.append({"name": name, "home": home})
    return out


def _index(home):
    names = {}
    try:
        with open(os.path.join(home, "session_index.jsonl"), "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    j = json.loads(ln)
                except ValueError:
                    continue
                if j.get("id") and j.get("thread_name"):
                    names[j["id"]] = str(j["thread_name"])
    except OSError:
        pass
    return names


def _first_meta(path, mtime):
    """(cwd, model) dari baris pertama (session_meta); di-cache per mtime."""
    c = _meta_cache.get(path)
    if c and c[0] == mtime:
        return c[1], c[2]
    cwd = model = None
    try:
        with open(path, "rb") as f:
            line = f.readline(4_000_000)
        p = (json.loads(line).get("payload") or {})
        cwd, model = p.get("cwd"), p.get("model")
    except (OSError, ValueError):
        pass
    _meta_cache[path] = (mtime, cwd, model)
    return cwd, model


def list_items(limit=40):
    items = []
    for src in sources():
        names = _index(src["home"])
        found = []
        for f in glob.glob(os.path.join(src["home"], "sessions", "*", "*", "*", "rollout-*.jsonl")):
            m = TAIL_UUID.search(os.path.basename(f))
            if not m:
                continue
            try:
                st = os.stat(f)
            except OSError:
                continue
            found.append((st.st_mtime, st.st_size, f, m.group(1).lower()))
        found.sort(reverse=True)
        for mtime, size, f, sid in found[:limit]:
            cwd, _ = _first_meta(f, mtime)
            items.append({"sid": sid, "path": f, "project": "import:" + src["name"], "mtime": mtime, "size": size,
                          "title": names.get(sid) or f"({src['name']}, tanpa judul)", "cwd": cwd,
                          "forked_from": None, "forks": 0, "source": src["name"]})
    return items


# ---------------------------------------------------------------- konversi
def _text(content):
    if not isinstance(content, list):
        return ""
    return "".join(c.get("text", "") for c in content
                   if isinstance(c, dict) and c.get("type") in ("input_text", "output_text", "text"))


def _is_injected(t):
    s = t.lstrip()
    return any(s.startswith(p) for p in INJECTED)


def _mid_clip(s, n):
    if len(s) <= n:
        return s
    head = n * 2 // 3
    return s[:head] + f"\n… (+{len(s) - n} karakter dipotong) …\n" + s[-(n - head):]


def _call_line(name, args):
    try:
        a = json.loads(args) if isinstance(args, str) else args
    except ValueError:
        a = None
    if isinstance(a, dict):
        if name == "exec_command":
            cmd = a.get("cmd")
            cmd = " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd or "")
            wd = a.get("workdir")
            return f"▸ exec_command{' @' + str(wd) if wd else ''}: {_mid_clip(cmd, 600)}"
        if name == "write_stdin":
            return f"▸ write_stdin: {_mid_clip(str(a.get('chars', '')), 300)}"
        if name == "view_image":
            return f"▸ view_image: {a.get('path', '')}"
        if name == "update_plan":
            return f"▸ update_plan: {_mid_clip(json.dumps(a.get('plan'), ensure_ascii=False), 500)}"
        return f"▸ {name}: {_mid_clip(json.dumps(a, ensure_ascii=False), 400)}"
    return f"▸ {name}: {_mid_clip(str(args), 400)}"


def _out_text(out):
    if isinstance(out, str):
        i = out.find("\nOutput:\n")          # buang header "Chunk ID / Wall time"
        body = out[i + 9:] if 0 <= i < 400 else out
        return _mid_clip(body.strip(), TOOL_OUT_MAX)
    if isinstance(out, list):
        return "[keluaran non-teks: " + ", ".join(sorted({str(c.get("type")) for c in out if isinstance(c, dict)})) + "]"
    return _mid_clip(json.dumps(out, ensure_ascii=False), TOOL_OUT_MAX)


def convert(path, budget=BUDGET_CHARS):
    """-> dict(cwd, model, id, total, kept, turns=[{ts, user, assistant}])."""
    meta = {"cwd": None, "model": None, "id": None}
    turns, cur = [], None

    def flush():
        nonlocal cur
        if cur:
            cur["assistant"] = _mid_clip("\n\n".join(cur.pop("parts")).strip(), TURN_MAX)
            turns.append(cur)
        cur = None

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            # saring cepat dengan kata kunci saja (tidak bergantung spasi/urutan kunci); tipe sebenarnya dicek setelah parse
            if "response_item" not in ln and "session_meta" not in ln and "turn_context" not in ln:
                continue
            try:
                j = json.loads(ln)
            except ValueError:
                continue
            p = j.get("payload") if isinstance(j.get("payload"), dict) else {}
            jt = j.get("type")
            if jt == "session_meta":
                meta["cwd"], meta["id"] = p.get("cwd"), p.get("id")
                continue
            if jt == "turn_context":
                meta["model"] = p.get("model") or meta["model"]
                continue
            if jt != "response_item":
                continue
            pt, ts = p.get("type"), j.get("timestamp")
            if pt == "message":
                role, txt = p.get("role"), _text(p.get("content"))
                if role == "user" and txt.strip() and not _is_injected(txt):
                    flush()
                    cur = {"ts": ts, "user": txt.strip(), "parts": []}
                elif role == "assistant" and txt.strip() and cur is not None:
                    cur["parts"].append(txt.strip())
            elif pt == "function_call" and cur is not None:
                cur["parts"].append(_call_line(p.get("name", "?"), p.get("arguments")))
            elif pt == "function_call_output" and cur is not None:
                cur["parts"].append("⮑ " + _out_text(p.get("output")))
        flush()
    total, used, kept = len(turns), 0, []
    for t in reversed(turns):
        cost = len(t["user"]) + len(t["assistant"])
        if kept and used + cost > budget:
            break
        kept.append(t)
        used += cost
    kept.reverse()
    return {**meta, "total": total, "kept": len(kept), "turns": kept}


# ---------------------------------------------------------------- tulis transkrip Claude
def _iso(ts, add=0.0):
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        d = datetime.now(timezone.utc)
    return (d + timedelta(seconds=add)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _projects_dir():
    return os.path.join(settings.claude_home(), "projects")


def project_dir_for(cwd):
    return os.path.join(_projects_dir(), re.sub(r"[^A-Za-z0-9]", "-", cwd))


def import_session(item, fork):
    """Ubah rollout `item` jadi sesi Claude Code. -> (sid_baru, cwd, dibuat_baru, info).
    Resume: id deterministik (uuid5 dari id sumber) -> impor SEKALI; panggilan berikutnya melanjutkan hasil impor
    (yang sudah berkembang di Claude/clauseek), tidak ditimpa. Fork: id acak, salinan baru dari kondisi sumber saat ini."""
    cwd = item.get("cwd")
    if not cwd or not os.path.isdir(cwd):
        raise RuntimeError(f"folder kerja sesi tidak ada: {cwd!r} (impor/resume harus dari folder proyek aslinya)")
    new_sid = str(uuid.uuid4()) if fork else str(uuid.uuid5(uuid.NAMESPACE_URL, "claude-watch:import:" + item["sid"]))
    d = project_dir_for(cwd)
    out = os.path.join(d, new_sid + ".jsonl")
    if not fork and os.path.isfile(out):
        return new_sid, cwd, False, {"reused": True}
    conv = convert(item["path"])
    if not conv["turns"]:
        raise RuntimeError("tidak ada giliran percakapan yang bisa diimpor dari sesi ini")
    model = conv.get("model") or "imported"
    note = (f"[Diimpor dari {item.get('source', 'sumber lain')} · sesi {item['sid'][:8]} · folder {cwd} · "
            f"{conv['kept']} dari {conv['total']} giliran terakhir; output tool diringkas]\n\n")
    base = {"isSidechain": False, "userType": "external", "entrypoint": "cli", "cwd": cwd,
            "sessionId": new_sid, "version": VERSION, "gitBranch": ""}
    lines, parent = [], None
    for i, t in enumerate(conv["turns"]):
        for role in ("user", "assistant"):
            u = str(uuid.uuid4())
            if role == "user":
                msg = {"role": "user", "content": (note if i == 0 else "") + t["user"]}
                ent = {"parentUuid": parent, "type": "user", "message": msg, "uuid": u,
                       "timestamp": _iso(t["ts"]), "promptId": str(uuid.uuid4())}
            else:
                text = t["assistant"] or "(tidak ada jawaban tertulis pada giliran ini)"
                msg = {"model": model, "id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant",
                       "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
                       "usage": {"input_tokens": 0, "output_tokens": 0}}
                # session_id (snake) = id sumber -> Claude Watch menampilkan penanda "FORK dari" ke sesi sumber
                ent = {"parentUuid": parent, "type": "assistant", "message": msg, "uuid": u,
                       "timestamp": _iso(t["ts"], 1.0), "requestId": "req_" + uuid.uuid4().hex[:20],
                       "session_id": item["sid"]}
            lines.append({**ent, **base})
            parent = u
    title = item.get("title") or "sesi impor"
    lines.insert(0, {"type": "ai-title", "aiTitle": str(title)[:120], "sessionId": new_sid})
    os.makedirs(d, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for j in lines:
            f.write(json.dumps(j, ensure_ascii=False) + "\n")
    os.replace(tmp, out)
    return new_sid, cwd, True, {"kept": conv["kept"], "total": conv["total"], "reused": False}
