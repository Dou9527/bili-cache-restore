# bili-cache-restore

把手机上**磁盘里明明有、B 站离线列表里却看不到**的旧缓存，重新登记回数据库。

起因很常见：换手机、刷机、用清理软件清过 B 站数据之后，`download/` 目录里 6 个 G 的视频还在，但「我的 → 离线缓存」是空的。B 站自己的「清理存储空间」还显示占用 6.6GB，两处数字对不上，看着就来气。

---

## 为什么改 json 没用

B 站的离线列表**不扫描磁盘目录**，它只读自己私有目录下的 Room 数据库：

```
/data/data/tv.danmaku.bili/databases/offlineVideo.db
  └─ video_info 表   ← 列表的数据源，一行 = 一个已缓存分 P
```

磁盘上文件再完整，只要这张表里没有对应记录，列表里就是一个字都不会有。所以：

- ❌ 改 `entry.json`、补 `index.json`、改目录名 —— 全部无效
- ✅ 往 `video_info` 插入对应行 —— 立刻出现

本项目的全部工作，就是把磁盘上的 `entry.json` + `index.json` 还原成 `video_info` 的一行。

## 适用场景

- 换过手机，`Android/data/tv.danmaku.bili/download` 恢复了，`databases` 没恢复
- 用清理软件清过 B 站数据（文件还在，记录没了）
- 缓存是从别的设备/电脑拷进来的
- B 站升级或异常退出后列表突然变空，但文件还在

## 不适用

- 缓存文件本身不完整（只有 `danmaku` 没有 `video.m4s`）——脚本会登记成功，但点开是黑屏
- 手机没有 root —— 读不到 `/data/data/`，无解
- 想把缓存导出成 MP4 —— 那是另一个需求，本项目不做转码

## 前置条件

| 项 | 要求 |
|---|---|
| root | Magisk / KernelSU / APatch 任一，能 `su -c id` 拿到 uid=0 |
| adb | 电脑装有 adb，USB 调试已授权（`adb devices` 显示 `device`） |
| Python | 3.6+，只用标准库，无需 pip install |
| B 站 | 已安装并至少启动过一次（要保证数据库已创建） |

## 用法

```bash
git clone https://github.com/Dou9527/bili-cache-restore.git
cd bili-cache-restore

python bili_restore.py --list      # 只列出现有记录，不碰任何东西
python bili_restore.py --dry-run   # 完整走一遍流程，但不写库
python bili_restore.py             # 正式执行
```

`--dry-run` 适合第一次用：它会把「能识别多少条、第一条长什么样」打印出来，你确认标题、大小合理，再正式跑。

adb 查找顺序：环境变量 `ADB` → `PATH` → `C:/platform-tools`、`~/platform-tools`、`Android/Sdk/platform-tools` 等常见位置 → 脚本所在目录附近。都不满足时手动指定：

```bash
ADB=/path/to/adb python bili_restore.py
```

## 它做了什么

| 步骤 | 动作 |
|---|---|
| 1 | 强停 B 站，把数据库备份到 `/sdcard/offlineVideo.backup.<时间戳>.db` |
| 2 | 拉取 `offlineVideo.db` 及 `-wal` / `-shm` 到本地临时目录 |
| 3 | 把 `download/` 下所有 `*.json` 打包拉回本地 |
| 4 | 解析 `entry.json` + `index.json`，生成候选记录 |
| 5 | `PRAGMA wal_checkpoint(TRUNCATE)` 合并 WAL，`INSERT OR REPLACE` 写入 |
| 6 | 切成单文件库推回手机，恢复属主 / 权限 / SELinux 上下文 |

## 踩过的坑（想自己改代码的看这段）

这些都是实际调试中撞出来的，写在这里省得别人重踩：

**WAL 必须先合并。** B 站运行时数据全在 `offlineVideo.db-wal` 里，主库可能只有 4096 字节的 schema。直接改主库再推回去，等于把记录全丢了。必须先 `PRAGMA wal_checkpoint(TRUNCATE)`。

**属主不能写死。** 不同机型上 B 站的 uid 不同（`u0_a313`、`u0_a2xx` 都有可能）。写死会导致文件被 root 拥有，B 站读不到、直接闪退。脚本用 `stat -c '%u:%g'` 读原库属主再套用。

**必须 `restorecon`。** 否则 SELinux 上下文不对，同样读不到。

**用 `cp` 而不是 `adb push` 覆盖。** `cp -f` 保留 inode，避免某些机型上数据库权限重建失败。

**`storage_path` 不含 `download/` 前缀。** 它形如 `<aid>/c_<cid>`，播放器自己拼 `download/` + `storage_path` + `qn_path` + `/video.m4s`。

**标题会踩占位符。** 老版本 `entry.json` 的 `title` 字段常是「视频已缓存完成」，真标题在 `page_data.part`。不处理的话列表里全是同一句话。

**`sectionsLengthList` 是校验和。** 格式 `视频字节,音频字节`，两者之和必须等于 `total_size`。对不上说明解析错了，别硬写。

**`av2bv` 的映射表容易写错。** 正确写法：字母表前缀 `BV1`，`ENCODE_MAP = [8,7,0,5,1,3,2,4,6]`，正序填充。

## 安全与回滚

- 每次运行都会先备份，备份路径会打印在输出里
- 只对 `video_info` 做 `INSERT OR REPLACE`，不删除任何现有记录，不动其他表
- 出问题回滚：

```bash
adb shell su -c "am force-stop tv.danmaku.bili"
adb shell su -c "cp -f /sdcard/offlineVideo.backup.<时间戳>.db /data/data/tv.danmaku.bili/databases/offlineVideo.db"
adb shell su -c "chown <uid>:<uid> /data/data/tv.danmaku.bili/databases/offlineVideo.db"
adb shell su -c "restorecon /data/data/tv.danmaku.bili/databases/offlineVideo.db"
```

## 常见问题

**「可插入 0 条」**
`download/` 里没有 `entry.json`，或者目录层级被打乱了。正常层级是 `download/<aid>/c_<cid>/<qn>/index.json`。

**列表出现了，点开黑屏**
记录对了但文件缺。用 `--list` 对照，或去 B 站里手动删掉那一条。

**打开 B 站就闪退**
属主或 SELinux 上下文没恢复。按上面的回滚步骤还原，再检查 `/data/data/tv.danmaku.bili/databases/` 下的文件属主。

**会不会因为 B 站升级失效**
不会。脚本只读写 SQLite 表，不 hook 进程、不依赖混淆类名。只要表结构不变就一直能用。

## 免责声明

仅供恢复**你自己设备上本来就存在的缓存**。请勿用于下载、破解、二次分发受版权保护的内容。使用风险自负，操作前请确认已有备份。

## English

`bili_restore.py` re-registers orphaned offline cache files with Bilibili's `offlineVideo.db`. The offline list reads only the `video_info` table — editing JSON files on disk does nothing. Requires a rooted device and adb. Run `python bili_restore.py --dry-run` first. A timestamped backup of the database is created on `/sdcard` before any write.

## License

MIT
