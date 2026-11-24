# -*- coding: utf-8 -*-
"""
B站旧缓存恢复工具
=================================================
作用：把 download 目录里已经存在、但没被 B 站登记进数据库的缓存，
      重新写回 offlineVideo.db，让「我的 → 离线缓存」重新显示出来。

原理：
  B站离线列表读的是 /data/data/tv.danmaku.bili/databases/offlineVideo.db
  的 video_info 表，不是磁盘目录。这个脚本解析磁盘上的 entry.json +
  index.json，还原成 video_info 的一行，再合并回数据库。

用法：
  python bili_restore.py            # 正常执行
  python bili_restore.py --dry-run  # 只看会插入什么，不动手机
  python bili_restore.py --list     # 只列出当前数据库里有哪些记录

前提：手机已 root，USB 调试已授权，platform-tools 可用。
"""
import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

PKG = "tv.danmaku.bili"
DB_REMOTE = "/data/data/tv.danmaku.bili/databases/offlineVideo.db"
DL_REMOTE = "/storage/emulated/0/Android/data/tv.danmaku.bili/download"
TAR_REMOTE = "/sdcard/bili_restore.tar"
BACKUP_REMOTE = "/sdcard/offlineVideo.backup.%s.db"

QN_NAME = {
    6: "240P 流畅", 16: "360P 流畅", 32: "480P 清晰", 64: "720P 高清",
    74: "720P 60帧", 80: "1080P 高清", 112: "1080P+ 高码率", 116: "1080P 60帧",
    120: "4K 超清", 125: "HDR 真彩", 126: "杜比视界", 127: "8K 超高清",
}
QN_DISP = {
    6: "240P", 16: "360P", 32: "480P", 64: "720P", 74: "720P", 80: "1080P",
    112: "1080P", 116: "1080P", 120: "4K", 125: "HDR", 126: "杜比", 127: "8K",
}
PLACEHOLDER = ("视频已缓存完成", "下载完成", "已缓存完成", "")


# ---------------------------------------------------------------- adb

def find_adb():
    """按顺序查找 adb：环境变量 ADB → PATH → 常见安装位置 → 脚本附近。"""
    exe = "adb.exe" if os.name == "nt" else "adb"
    cands = []

    if os.environ.get("ADB"):
        cands.append(os.environ["ADB"])

    for d in os.environ.get("PATH", "").split(os.pathsep):
        if d:
            cands.append(os.path.join(d, exe))

    home = os.path.expanduser("~")
    for base in (
        "C:/platform-tools",
        "C:/adb",
        os.path.join(os.environ.get("LOCALAPPDATA", home), "Android", "Sdk", "platform-tools"),
        os.path.join(home, "platform-tools"),
        os.path.join(home, "Downloads", "platform-tools"),
        "/usr/local/bin",
        "/opt/homebrew/bin",
    ):
        cands.append(os.path.join(base, exe))

    here = os.path.dirname(os.path.abspath(__file__))
    d = here
    for _ in range(5):                       # 脚本被放在 platform-tools 附近时也能找到
        cands.append(os.path.join(d, "platform-tools", exe))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent

    cands.append(exe)

    seen = set()
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        try:
            r = subprocess.run([c, "version"], capture_output=True, timeout=10)
            if r.returncode == 0:
                return c
        except Exception:
            continue
    return None


def sh(adb, cmd, su=False):
    full = ("su -c '%s'" % cmd) if su else cmd
    r = subprocess.run([adb, "shell", full], capture_output=True,
                       encoding="utf-8", errors="replace")
    return (r.stdout or "").strip(), (r.stderr or "").strip()


def pull(adb, remote, local):
    subprocess.run([adb, "pull", remote, local], capture_output=True)


def push(adb, local, remote):
    r = subprocess.run([adb, "push", local, remote], capture_output=True,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0


# ---------------------------------------------------------------- 数据转换

def av2bv(aid):
    TABLE = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
    XOR_CODE = 23442827791579
    MAX_AID = 1 << 51
    ENCODE_MAP = [8, 7, 0, 5, 1, 3, 2, 4, 6]
    b = ["0"] * 9
    tmp = (MAX_AID | aid) ^ XOR_CODE
    for i in range(9):
        b[ENCODE_MAP[i]] = TABLE[tmp % 58]
        tmp //= 58
    return "BV1" + "".join(b)


def build_row(entry_path):
    cdir = os.path.dirname(entry_path)
    aid_dir = os.path.basename(os.path.dirname(cdir))
    cdir_name = os.path.basename(cdir)

    with open(entry_path, encoding="utf-8") as f:
        e = json.load(f)
    pd = e.get("page_data") or {}

    aid = int(e.get("avid") or aid_dir)
    cid = int(pd.get("cid") or cdir_name.replace("c_", ""))

    idx = None
    for d in sorted(os.listdir(cdir)):
        p = os.path.join(cdir, d, "index.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                idx = json.load(f)
            break
    if idx is None:
        return None

    v = (idx.get("video") or [{}])[0]
    a = (idx.get("audio") or [{}])[0]
    qn = int(v.get("id") or e.get("video_quality") or 0)
    v_size, a_size = int(v.get("size") or 0), int(a.get("size") or 0)
    dur = int(e.get("total_time_milli") or 0)

    part = (pd.get("part") or "").strip()
    raw = (e.get("title") or "").strip()
    if raw in PLACEHOLDER:
        raw = part
    if not part:
        part = raw
    title = raw or part

    storage_path = "%s/%s/%s" % (DL_REMOTE, aid_dir, cdir_name)

    vi = {
        "videoId": qn, "audioId": int(a.get("id") or 0),
        "videoMd5": v.get("md5") or "", "audioMd5": a.get("md5") or "",
        "videoCodecid": int(v.get("codecid") or 0),
        "preferredAudioQuality": int(a.get("id") or 0),
        "realAudioQuality": int(a.get("id") or 0),
        "qn": qn, "fnval": 84948, "fourk": True, "codecType": 3,
        "voiceBalance": 1, "needTrial": False, "asrLanguage": "cmn",
    }

    return {
        "group_id": aid, "group_title": title, "aid": aid, "cid": cid,
        "season_id": int(e.get("season_id") or 0), "episode_id": 0, "biz_type": 1,
        "key": "av_%d_%d" % (aid, cid),
        "video_attr": 0, "video_id": aid,
        "archive_title": title, "page_id": cid, "page_title": part or title,
        "cover": e.get("cover") or "",
        "author": e.get("owner_name") or "",
        "author_mid": int(e.get("owner_id") or 0),
        "bvid": e.get("bvid") or av2bv(aid),
        "width": int(pd.get("width") or v.get("width") or 0),
        "height": int(pd.get("height") or v.get("height") or 0),
        "rotate": int(pd.get("rotate") or 0),
        "duration": dur,
        "subtitle_lan": "", "subtitle_lan_doc": "",
        "subtitle_font_size_type": 0, "subtitle_position": "",
        "danmaku_count": int(e.get("danmaku_count") or 0),
        "real_videoQuality": qn, "preferred_videoQuality": qn,
        "quality_display": QN_DISP.get(qn, str(qn)),
        "quality_name": QN_NAME.get(qn, str(qn)),
        "media_type": "DASH", "qn_path": str(qn),
        "video_info": json.dumps(vi, ensure_ascii=False, separators=(",", ":")),
        "state_value": 5, "error_code": 0,
        "total_size": (v_size + a_size) or int(e.get("total_bytes") or 0),
        "downloaded_size": (v_size + a_size) or int(e.get("total_bytes") or 0),
        "storage_path": storage_path,
        "create_time": int(e.get("time_create_stamp") or 0),
        "update_time": int(e.get("time_update_stamp") or 0),
        "videoIndex": 0, "pageIndex": int(pd.get("page") or 1),
        "season_type": 0, "from": pd.get("from") or "vupload",
        "app_version": 9200300,
        "extended": "", "auth_code": 0, "link": pd.get("link") or "",
        "sectionsLengthList": "%d,%d" % (v_size, a_size),
        "sectionsContentTime": "%d,%d" % (dur, dur),
        "sectionsDownloadedList": "%d,%d" % (v_size, a_size),
    }


# ---------------------------------------------------------------- 主流程

def main():
    dry = "--dry-run" in sys.argv
    adb = find_adb()
    if not adb:
        print("找不到 adb，请把 platform-tools 放在脚本同目录或改 find_adb()")
        return 1
    print("adb:", adb)

    out, _ = sh(adb, "getprop ro.product.model")
    pid, _ = sh(adb, "pidof " + PKG)
    print("设备:", out or "?", "| B站运行中:" , "是" if pid else "否")

    tmp = tempfile.mkdtemp(prefix="bilirestore_")

    print("\n[1/6] 备份数据库" + ("" if dry else " 并强停 B 站"))
    ts = time.strftime("%Y%m%d_%H%M%S")
    if dry:
        sh(adb, "cp -a %s %s; echo OK" % (DB_REMOTE, BACKUP_REMOTE % ts), su=True)
    else:
        sh(adb, "am force-stop %s; sleep 1; cp -a %s %s; echo OK"
           % (PKG, DB_REMOTE, BACKUP_REMOTE % ts), su=True)
    print("   备份 ->", BACKUP_REMOTE % ts)

    print("[2/6] 拉取数据库")
    work = os.path.join(tmp, "db")
    os.makedirs(work)
    for ext in ("", "-wal", "-shm"):
        sh(adb, "cp -f %s%s /sdcard/_r.db%s; chmod 666 /sdcard/_r.db%s"
           % (DB_REMOTE, ext, ext, ext), su=True)
        pull(adb, "/sdcard/_r.db" + ext, os.path.join(work, "offlineVideo.db" + ext))
    sh(adb, "rm -f /sdcard/_r.db*", su=True)
    dbp = os.path.join(work, "offlineVideo.db")
    if not os.path.exists(dbp):
        print("   拉取数据库失败")
        return 1
    print("   本地库:", os.path.getsize(dbp), "bytes")

    print("[3/6] 打包缓存元数据")
    sh(adb, 'cd %s && find . -name "*.json" | tar -cf %s -T - ; chmod 666 %s'
       % (DL_REMOTE, TAR_REMOTE, TAR_REMOTE), su=True)
    tar_local = os.path.join(tmp, "j.tar")
    pull(adb, TAR_REMOTE, tar_local)
    jsdir = os.path.join(tmp, "jsons")
    os.makedirs(jsdir, exist_ok=True)
    if os.path.exists(tar_local):
        subprocess.run(["tar", "-xf", tar_local, "-C", jsdir], capture_output=True)

    print("[4/6] 解析")
    rows, bad = [], 0
    for ep in sorted(glob.glob(os.path.join(jsdir, "**", "entry.json"), recursive=True)):
        try:
            r = build_row(ep)
            if r:
                rows.append(r)
            else:
                bad += 1
        except Exception:
            bad += 1
    print("   可插入 %d 条，跳过 %d 条" % (len(rows), bad))
    if not rows:
        print("   没有可解析的缓存，退出")
        return 1

    print("[5/6] 写入数据库")
    con = sqlite3.connect(dbp)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.commit()
    cur = con.cursor()
    before = cur.execute('SELECT COUNT(*) FROM "video_info"').fetchone()[0]
    if dry:
        print("   [dry-run] 当前 %d 条，将插入 %d 条，不实际写入" % (before, len(rows)))
        con.close()
        for r in rows[:5]:
            print("     ·", r["key"], "|", r["page_title"][:34], "|", r["total_size"])
        shutil.rmtree(tmp, ignore_errors=True)
        return 0

    added = 0
    for r in rows:
        cols = list(r.keys())
        sql = 'INSERT OR REPLACE INTO "video_info" (%s) VALUES (%s)' % (
            ",".join('"%s"' % c for c in cols), ",".join("?" * len(cols)))
        cur.execute(sql, [r[c] for c in cols])
        added += 1
    con.commit()
    con.execute("PRAGMA journal_mode=DELETE")
    con.commit()
    after = cur.execute('SELECT COUNT(*) FROM "video_info"').fetchone()[0]
    con.close()
    print("   %d 条 -> %d 条（写入 %d）" % (before, after, added))

    print("[6/6] 推回手机")
    newdb = os.path.join(tmp, "new.db")
    shutil.copy(dbp, newdb)
    sh(adb, "rm -f %s-wal %s-shm %s.lck" % (DB_REMOTE, DB_REMOTE, DB_REMOTE), su=True)
    ok = push(adb, newdb, "/sdcard/_new.db")
    if not ok:
        print("   推送失败")
        return 1
    # 属主必须与原库一致，否则 B 站读不到（不同机型 uid 不同，不能写死）
    owner, _ = sh(adb, "stat -c '%%u:%%g' %s" % DB_REMOTE, su=True)
    if ":" not in owner:
        uid, _ = sh(adb, "dumpsys package %s | grep -m1 userId= | tr -d ' ' | cut -d= -f2" % PKG)
        owner = ("%s:%s" % (uid, uid)) if uid.isdigit() else None
    if owner:
        chown = "chown %s %s;" % (owner, DB_REMOTE)
    else:
        chown = ""
        print("   警告：无法确定数据库属主，跳过 chown，B 站可能读不到库")

    sh(adb, "am force-stop %s; cp -f /sdcard/_new.db %s; %s chmod 600 %s; "
            "restorecon %s; rm -f /sdcard/_new.db"
       % (PKG, DB_REMOTE, chown, DB_REMOTE, DB_REMOTE), su=True)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n完成。共 %d 条缓存已登记，请打开 B站 → 我的 → 离线缓存 查看。" % after)
    print("原库备份保留在手机：%s" % (BACKUP_REMOTE % ts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
