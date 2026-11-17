# MicroBench 跨设备同步契约

> 两台设备通过坚果云共享同一份科研状态：译文、批注、阅读进度、论文本体。
>
> 这份文档写死两类东西：
> **① 改了也不会坏的**（随便改）
> **② 动了就完蛋的**（红线，动之前必须两台同时改 + 迁移数据）
>
> 改代码前先读这里。

---

## 1. 同步的是哪四样

| 通道 | 目录 | 内容 | 备注 |
|---|---|---|---|
| 译文快照 | `snapshots/` | 已翻好的页面 + 译文文本 + 每页 `text_hash` | 对方翻过就不用重翻、不烧额度 |
| 批注 | `annotations/` | 高亮 + 笔记 + **原文侧荧光笔矩形**（归一化坐标 x/y/w/h） | 按 id 合并，两边同时标注不互相覆盖 |
| 阅读进度 | `progress/` | 翻到第几页 + 缩放比例 | **按当前打开的论文**同步 |
| 论文本体 | `files/` | PDF 实体 + 清单（`relative_path` / `size` / `mtime` / `sha256`） | 增量；内容没变零传输；删除走墓碑 |

**云端路径（两台必须一致）**

```
<Nutstore 镜像目录>\我的坚果云\MicroBench同步
```

本地绝对路径各机器不同（`Nutstore\1`、`Nutstore\2`…），靠各自 `.env` 的 `ANNOTATIONS_DIR` 指过去。
`我的坚果云` 是坚果云自带的官方云端沙箱，**每台设备都有** —— 放在它下面，云端路径天然一致。

镜像目录 id 从 `%APPDATA%\Nutstore\config\UsersMap.json` 读：`"users": {"邮箱": "id"}`。

---

## 2. 寻址键 paper_id（同步的唯一依据）

```
paper_id = basename(去 .pdf, 截 70 字符) + "_" + djb2(完整 vault 相对路径) 的 base36

相对路径 = 相对 vault 根（BASE_DIR），正斜杠分隔
djb2     = h = 5381; h = ((h << 5) + h + charCode) | 0;  取 >>> 0 无符号
```

前端 `microbench/static/index.html` 和后端 `microbench/app.py:_paper_id_for()` 各算一份，
**必须永远一致**。落盘文件名 = paper_id 去掉非 `[A-Za-z0-9_-]` 字符。

**真值**（2026-10-23 两台设备实测互通过，已被测试锁死）：

| 相对路径 | paper_id | 同步目录里的文件名 |
|---|---|---|
| `01_Literature/04_Survey_综述/01_论文原文_PDF/2008-JACERS-综述-Magnetoelectric laminate composites An overview.pdf` | `2008-JACERS-综述-Magnetoelectric laminate composites An overview_pgghcp` | `2008-JACERS-综述-MagnetoelectriclaminatecompositesAnoverview_pgghcp.json` |
| `01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/2026_Sensors_&_Actuators_A._Physical_382_(2025)_116174.pdf` | `2026_Sensors_&_Actuators_A._Physical_382_(2025)_116174_1j6nqf7` | `2026_Sensors__Actuators_A_Physical_382_2025_116174_1j6nqf7.json` |
| `01_Literature/01_Device_TCAD_器件仿真/01_论文原文_PDF/2025_AgenticTCAD_A_LLM-based_Multi-.pdf` | `2025_AgenticTCAD_A_LLM-based_Multi-_1dawd47` | `2025_AgenticTCAD_A_LLM-based_Multi-_1dawd47.json` |

---

## 3. 四条红线 ⚠️

动这四样之前，**两台设备必须同时改 + 迁移已有数据**，否则同步静默断裂（不报错，只是"同步了但对不上"）：

1. **paper_id 算法** —— basename 截断长度 / djb2 种子 / base36 / 相对路径是否参与 hash
2. **`01_Literature` 下的目录名与 PDF 文件名** —— 相对路径参与 hash，差一个字就断
3. **四个通道目录名** —— `snapshots` / `annotations` / `progress` / `files`
4. **`.env` 里 `ANNOTATIONS_DIR` 的云端末段** —— `我的坚果云\MicroBench同步`

---

## 4. 哪些改动是安全的

**安全，随便改**：功能 / UI / 新特性 / 重构 / 加测试 / 翻译质量改进 / 加同步字段 / 改 schema / 改阅读体验

> ⚠ 依据要**按通道分开**说（2026-11-17 全面审查更正）：
>
> | 通道 | 合并方式 | 会不会互相踩坏 |
> |---|---|---|
> | 批注 | 按 id 合并 + `updated_at` 新者胜 + 删除墓碑 | 内容层不踩；但**整文件覆盖**会 → 所以有乐观 CAS（§7） |
> | 译文快照 | **按 page_num 合并**（不是整份替换） | 内容层不踩；但**整文件覆盖**会 → 同样有 CAS（§7） |
> | 阅读进度 | 整份替换（只有一台设备在写） | 不踩 |
> | PDF 本体 | 按 sha256 校验 + 增量 + 墓碑 | 不踩（内容不同 → 报冲突，不覆盖） |
>
> ⚠⚠ 原文这里写的是「快照是按 `paper_id` **整份替换**…新旧版本能共存，不会互相踩坏」——
> **实测不成立**：2026-10-27 改成按 page_num 合并；而且即便按页合并，
> 「读旧值 → 合并 → 整份写回」在**跨设备**时仍会踩（另一台设备在这窗口里写的页会被盖掉）。
> → 快照是整个同步层里**唯一会互相踩坏**的一路，靠下面的 CAS 兜住。

### 4.1 跨设备并发写：乐观 CAS（改共享文件的写路径前必读）

`annotations/`、`snapshots/`、`progress/`、`files/` 都在**坚果云同步根**里，
两台设备会**同时**写同一个文件。凡是「读旧值 → 改 → 整份写回」的写路径，
必须走乐观 CAS（`microbench/app.py` 的 `_cas_update_json`）：

1. **版本指纹取在读之前**（`(mtime_ns, size)`）——
   它要标识「我们即将读到的那一版」。
   取在读之后 → 另一台设备在「读」与「取指纹」之间写过一次 →
   拿到新指纹 → 写前比对**必然通过** → 用旧基线盖回去 → **对方的数据静默消失**；
2. 撞满 3 轮 → **放弃**并返回 **409**（fail closed）。
   强写 = 把「可能冲突」变成「确定丢数据」。客户端拿 409 重试即可
   （服务端每次都重新读、重新合并 → 重试不丢数据）；
3. **进程内的 `threading.Lock` 挡不住另一台设备** ——
   跨设备只能靠 CAS，别拿锁当安全感。

⚠ 联动删除（`files/` 的 `propagate`）必须记录**发起方**（`propagate_by`）：
否则发起端自己同步时也会走删除逻辑 → **把自己那份也删了**，且云端实体同时释放 → 不可恢复。

**危险**：第 3 节那四样。

---

## 5. 机器护栏（别让红线被悄悄碰掉）

| 测试 | 作用 |
|---|---|
| `tests/test_paper_id_contract.py` | **真用 node 跑** `static/index.html` 里那段 JS，三方对齐（前端算法 / 后端 `_paper_id_for()` / 上面的锁定值）；并锁同步目录里的实际文件名、四个通道名、`MicroBench同步`、`我的坚果云` |
| `tests/test_sync_files.py` | files 通道的增量与墓碑 |
| `tests/test_sync_cas_2026_11_17.py` | **跨设备并发写**：批注保存 / 批注删除 / 快照保存 / files 还原 / 联动删除（真造「另一台设备在读写窗口里写一次」）—— 判据见 §4.1 |
| `tests/test_security_guards.py` | 路径越权防护（**越权防护在这一份里**，不在 `test_sync_files.py`） |

> ⚠ 2026-11-17 更正：原文把「越权防护」挂在 `tests/test_sync_files.py` 名下，
> 但那个文件里**没有**越权防护用例 —— 护栏清单必须与文件里真实存在的用例对得上，
> 否则「文件还在」会被当成「防护还在」。

**破坏性验证**（改算法的人如果只想"优化一下"，测试会挡住）：

```powershell
# 把 static/index.html 里的 5381 改成 5382 → 契约测试立刻红
python -m pytest tests/test_paper_id_contract.py -v
```

前端算法被重构到无法自动提取时，测试也会失败并提示"请人工确认并更新本文件"——
**这是故意的**：宁可用例红，也不让契约悄悄漂移。

---

## 6. 部署清单（每台设备）

- [ ] `git pull` 到最新 main
- [ ] `set PYTHONUTF8=1` 后 `pip install -r requirements.txt`
      （中文 Windows 上 pip 默认按 GBK 解码 requirements.txt 里的中文注释 → UnicodeDecodeError）
- [ ] 坚果云客户端已装、已登录（同一账号）
- [ ] `.env` 里 `ANNOTATIONS_DIR` = `<自己的镜像目录>\我的坚果云\MicroBench同步`
- [ ] `01_Literature` 的目录结构与另一台**逐字一致**
- [ ] 共读的论文：PDF 本体走 `files/` 通道自动同步，但**首次需要对方先有**；
      也可以手动复制到相同的相对路径（文件名一字不差）

---

## 7. 自检

```powershell
# 同步根 / 各通道内容
curl http://127.0.0.1:5000/api/sync/status

# 契约测试（改了 paper_id 相关代码必跑）
python -m pytest tests/test_paper_id_contract.py -v

# 坚果云真的上传了吗
#   查 %APPDATA%\Nutstore\db1\nutstore.db 的 config 表 → traffic_history.totalUploadSize
#   > 0 才算真上传过（all_sandbox_list 是缓存列表，不可信）
```

---

## 8. 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 同步面板一直显示"没有新内容" | 对方还没同步，或两边云端路径不一致 | 比对 `ANNOTATIONS_DIR` 的云端末段 |
| 同步了但译文/批注认不出来 | `paper_id` 对不上（相对路径有出入） | 看同步目录里的文件名，跟第 2 节真值表比 |
| PDF 打不开，报 400「非法路径」 | vault 是不是用了 junction / 符号链接指到别处 | `_safe_path()` 用 `os.path.abspath`（**逻辑路径**）做包含性检查 → **junction 是允许的**，不用改用复制。⚠ 2026-11-17 更正：原文写的「会用 `resolve()` 校验越权」已过时（§4.4 改过） |
| 文件放进坚果云了但云端看不到 | 目录建在 `Nutstore\<id>\` 顶层 | 顶层新建文件夹不上云（`sync_errors reason=7`）→ 要放进「我的坚果云」沙箱 |
| 进度不跳到对方那页 | 同步时没打开那篇论文 | 进度按当前打开的论文同步 → **先打开那篇，再点同步** |
