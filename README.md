# 红红子lab (REDKOlab)

[![Release](https://github.com/ChimeRED/redkolab/actions/workflows/release.yml/badge.svg)](https://github.com/ChimeRED/redkolab/actions/workflows/release.yml)
[![最新版本](https://img.shields.io/github/v/release/ChimeRED/redkolab)](https://github.com/ChimeRED/redkolab/releases/latest)

一个 **Blender 4.0 ~ 最新版通用的单文件插件**，把三个高频操作变成一次点击：

| 功能 | 说明 |
| --- | --- |
| 🔧 删除所有材质 | 一键清空选中物体的全部材质槽（材质数据块本身保留在文件里） |
| ✅ 应用所有修改器 | 一键把所有修改器烘焙进网格 |
| 🗑 删除所有修改器 | 一键丢弃全部修改器（不烘焙） |

三种呼出方式：

1. **饼菜单** —— 默认快捷键 <kbd>`</kbd>（可改成任意按键）
2. **N 面板** —— 3D 视图 ▸ 侧栏（<kbd>N</kbd>）▸ `红红子lab` 标签页
3. 搜索菜单（<kbd>F3</kbd>）里直接搜「红红子lab」或「REDKO」

插件列表里显示的名字是 **红红子lab (REDKOlab)**，中英文都能搜到。

---

## 一、安装：一个 zip 通吃 4.0 ~ 最新版

只有一份安装包，从 **[Releases 页面](https://github.com/ChimeRED/redkolab/releases/latest)** 下载
`redkolab-<版本>.zip` —— 每个版本一条永久归档，附件里还有 `SHA256SUMS.txt` 可校验。

本地自己打也可以：`python tools/build_zips.py`，产物在 `dist/`（`dist/` 不入库，安装包一律由 Release 归档）。

| 你的 Blender | 怎么装 |
| --- | --- |
| **4.2 及以上**（4.2 / 4.3 / 4.4 / 4.5 / 5.x） | 偏好设置 ▸ **扩展 / Get Extensions** ▸ 右上角下拉 ▸ **Install from Disk**，选这个 zip；或者**直接把 zip 拖进 Blender 窗口** |
| **4.0 / 4.1** | 偏好设置 ▸ **插件 / Add-ons** ▸ 安装… ▸ **Install from Disk** |

> 同一个包在 4.2+ 的**插件页**也能装（会按传统插件方式加载，功能完全一样），
> 所以无论你习惯点哪个安装按钮都不会翻车。

装完在列表里勾选启用即可。**手动安装**：把压缩包里的 `redkolab/` 整个文件夹拷到

```
<Blender 配置目录>/scripts/addons/
```

Windows 一般是 `%APPDATA%\Blender Foundation\Blender\<版本>\scripts\addons\`。

### 为什么一个包能通吃

包里的结构只有一种，源码只有一份：

```
redkolab/
    __init__.py            <- 同时带 bl_info（传统插件元数据）
    blender_manifest.toml  <- 扩展清单（4.2+ 读这个）
    compat.py
    ops.py
    ui.py
```

4.2+ 的扩展安装器既能读根目录的清单、也能读单层目录里的清单；而 4.0/4.1 的传统安装器
只认「和模块同名的顶层目录」。把代码和清单一起放进 `redkolab/`，两种安装器就都满足了。

这是实测比出来的，不是猜的：

| 包内布局 | 4.0/4.1 插件页 | 4.2+ 插件页 | 4.2+ 扩展页 |
| --- | --- | --- | --- |
| 代码放根目录 + 目录副本 | 可以 | **报错** | 可以 |
| **只有目录（本包采用）** | 可以 | 可以 | 可以 |

> 根目录带 `__init__.py` 的包会被 4.2+ 的插件页安装直接拒绝
> （`ZIP packaged incorrectly; __init__.py should be in a directory`），
> 这是 Blender 对扩展包的固有行为，不是本插件的问题。

---

## 二、使用

**前提**：物体模式 + 选中至少一个物体。没有选中任何物体时，会自动作用在**活动物体**上。

### 饼菜单

在 3D 视图里按 <kbd>`</kbd>，四个方向：

```
            上：插件设置
左：删除所有材质      右：应用所有修改器
            下：删除所有修改器
```

### N 面板

3D 视图 ▸ 按 <kbd>N</kbd> ▸ `红红子lab` 标签页。除了三个按钮，还会实时显示
「选中 N 个物体 · 材质槽 X · 修改器 Y」，以及「呼出饼菜单」按钮（不方便按键时用）。

### 改快捷键

**偏好设置 ▸ 插件 ▸ 红红子lab**：

* 点 **「按下新按键…」**，然后直接按你想用的键 → 立刻生效（<kbd>Esc</kbd> 取消、<kbd>Backspace</kbd> 恢复默认的 <kbd>`</kbd>）
* 或者手动改：勾 `Ctrl` / `Shift` / `Alt` / `Cmd`，再填「按键标识」（`ACCENT_GRAVE` 就是 <kbd>`</kbd>、`SPACE` 是空格、`F` 是 F 键）
* 「启用饼菜单快捷键」关掉后，只能用 N 面板呼出

也可以在 **偏好设置 ▸ 键位映射** 里搜索「红红子lab」，用 Blender 自带的方式改键 —— 两种方式是同一个键位。

---

## 三、每个功能的具体行为（边界情况都处理了）

### 删除所有材质

* 材质槽**一起删掉**，不只是把材质设为空
* **材质数据块本身保留**，不会顺手删除其它物体还在用的材质
* 物体级材质槽（`link == 'OBJECT'`）和数据块级材质槽都会清
* 网格 / 曲线等所有带材质的数据类型都支持；对空物体、灯光、摄像机等不会报错

### 应用所有修改器

* **逐个应用**，保持修改器顺序
* 在视口里被**禁用**的修改器：先临时启用再应用，报告里会说明有几个是这种情况
* **多用户（共享）数据**：自动复制成单用户再应用，不影响共享同一份网格的其它物体
* **带形态键的网格**：Blender 不允许逐个应用，自动改用「转换为网格」一次性烘焙（会丢形态键，无法避免）
* **被隐藏的物体**：临时取消隐藏、做完还原原状态
* **不在当前视图层的物体**（所在集合被排除等）：跳过并在报告里点名
* **曲线/文字上的构建类修改器**：Blender 本身就拒绝应用（提示要转网格），插件会明确报错并**保持原样**，不会偷偷把曲线改成网格
* **链接自其它 .blend 的物体**：跳过并提示

### 删除所有修改器

* 直接丢弃，**不烘焙**，几何不变
* 纯数据操作，不依赖可见性

三个操作都支持 <kbd>Ctrl+Z</kbd> 撤销。

---

## 四、兼容性与实测

**支持范围：Blender 4.0 及以上**（含 4.x 全系与 5.x）。

所有版本差异都收敛在 [redkolab/compat.py](redkolab/compat.py)：

* 给 `bpy.ops` 注入物体上下文统一用 `bpy.context.temp_override()`
  （4.0 已移除 2.80~3.1 时代的「字典覆盖」写法，所以旧分支已按需求删除）
* 插件偏好设置的 `bl_idname` 用 `__package__`：传统安装是 `redkolab`，
  4.2+ 扩展安装是 `bl_ext.<仓库>.redkolab`，写死字符串必挂
* 属性一律用**类型注解**写法（`key_type: StringProperty(...)`），
  赋值写法在 4.0+ 上根本不会生成 RNA 属性

### 实测矩阵

同一份 `redkolab-1.1.0.zip`，真机无头测试：真的装包、真启用、真建场景、真跑功能并断言。

| Blender | 安装入口 | 装成的模块 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 4.0.2 | 插件页 Install from Disk | `redkolab` | 92 | ✔ |
| 4.2.23 LTS | 插件页 Install from Disk | `redkolab` | 92 | ✔ |
| 4.2.23 LTS | 扩展页 Install from Disk | `bl_ext.user_default.redkolab` | 90 | ✔ |
| 5.2.2 LTS | 插件页 Install from Disk | `redkolab` | 92 | ✔ |
| 5.2.2 LTS | 扩展页 Install from Disk | `bl_ext.user_default.redkolab` | 90 | ✔ |

> 断言数 92 / 90 的差别：扩展安装下 Blender 会接管元数据（不再暴露 `bl_info`），
> 测试会跳过 2 项元数据检查，功能断言一项不少。

扩展清单另外用官方校验器验过（4.2 与 5.2 都验）：

```bash
blender --factory-startup --command extension validate dist/redkolab-1.1.0.zip
# -> Success parsing TOML in "dist\redkolab-1.1.0.zip"
```

### 关于 2.80 ~ 3.x

按需求已不再支持（旧接口分支、旧安装格式都删掉了）。如果你以后要给 2.80~3.1 出包，
需要两处回退：

1. `compat.run_op()` 里补回 2.80~3.1 的字典覆盖分支（`temp_override` 是 3.2 才有的）
2. `__init__.py` 的 `bl_info["blender"]` 与清单的 `blender_version_min` 调回 `(2, 80, 0)`

---

## 五、目录结构

```
redkolab/
├─ redkolab/                    插件本体（这个目录就是插件，可直接拷进 scripts/addons/）
│  ├─ __init__.py               bl_info、注册/注销
│  ├─ blender_manifest.toml     4.2+ 扩展清单（版本号打包时自动同步）
│  ├─ compat.py                 跨版本兼容层（版本差异只在这里）
│  ├─ ops.py                    功能算符
│  └─ ui.py                     饼菜单、N 面板、偏好设置
├─ tools/
│  ├─ build_zips.py             打包（默认 nested 布局，可选其他布局做对比）
│  ├─ check_version.py          版本号一致性检查（发布守门）
│  ├─ inspect_zip.py            安装包结构自检（发布守门）
│  ├─ blender_test.py           Blender 内部真机测试（92 项断言，含边界情况）
│  ├─ run_tests.ps1             在指定 Blender 上跑测试（自动隔离配置目录）
│  ├─ release.ps1               一键发版：校验 → 升版本 → 打 tag → 推送
│  └─ probe.py                  版本差异探针（升级 Blender 前先跑它）
├─ .github/workflows/release.yml   自动构建 / 多版本真机测试 / 发布归档
└─ dist/                        本地打包产物（不入库，安装包归档在 Releases）
```

### 自己打包

```bash
python tools/build_zips.py              # 打正式包
python tools/build_zips.py --variant all  # 打所有候选布局，用于对比
```

### 自己跑测试

```powershell
powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1 -Blender "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
```

会自动判断安装方式（4.2+ 走扩展、4.0/4.1 走传统），并把 Blender 配置目录指到临时目录，
**不会污染你真实的 Blender 配置**。想单独验证某个安装入口：

```powershell
... -Mode extensions      # 强制走扩展页安装
... -Mode addon_install   # 强制走插件页安装
```

### 升级到更新的 Blender 时

```powershell
blender --background --factory-startup --python tools\probe.py
```

它会报告各版本底层行为的真实情况（`temp_override` 是否可用、注解式/赋值式属性哪种生效、
字典覆盖是否还能用、`keymap_items.new` 参数是否兼容），方便快速定位新版本的变化。

---

## 六、自动发布与归档

打一个 tag，就会自动完成「打包 → 自检 → 多版本真机测试 → 发布 → 归档」：

```powershell
powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump patch
```

这条命令依次做：检查工作区干净 → 升版本号（`bl_info` 与 `blender_manifest.toml` 一起改，永远同步）
→ 版本一致性检查 → 打包 → 安装包结构自检 → 提交 → 打 `v1.2.0` tag → 推送。
任何一步不过就直接中止，不会把坏包推上去。加 `-DryRun` 可以只看检查结果不推送。

推送后 [.github/workflows/release.yml](.github/workflows/release.yml) 自动执行：

| 阶段 | 内容 |
| --- | --- |
| 打包自检 | tag 与源码版本一致性、打包、结构自检（布局 / 元数据 / 属性写法 / 上下文覆盖）、生成 SHA256SUMS.txt |
| 真机测试 | 下载 Blender **4.2 / 4.5 / 5.2**（首次下载后走缓存），在隔离的配置目录里真装真跑 90+ 项断言；5.2 上再额外走一遍扩展页安装入口 |
| 发布归档 | 建立 Release，把 `redkolab-<版本>.zip` 与 `SHA256SUMS.txt` 作为附件永久归档 |

**任何一步失败都不会发布** —— 不会出现版本号对不上的包或装不上的包被归档。

只想构建和测试、不发版：仓库页 ▸ Actions ▸ Release ▸ Run workflow（手动触发不发 Release）。

---

## 七、已知限制

* 编辑模式下按钮是灰的（Blender 不允许在编辑模式改动材质/修改器数据），面板会提示切回物体模式
* 饼菜单的「按下新按键…」捕获需要真实窗口，无法在无头环境自动化测试，其余功能都有自动化覆盖
* 曲线的构建类修改器无法应用（Blender 的限制，见上文）
* 「应用所有修改器」遇到形态键会丢失形态键（转换烘焙的固有代价）
* 许可证：GPL-3.0-or-later（Blender 插件的通行许可）
